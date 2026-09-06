"""Read-only audit of quant history collections against Tonghuashun.

Only these MongoDB collections are read:

* ``stock_history_daily_bars``
* ``stock_history_60m_bars``
* ``stock_history_15m_bars``

The audit never inserts, updates, deletes, or creates indexes.  Tonghuashun
v6 line type ``00`` is the unadjusted daily reference, ``40`` is the
unadjusted 30-minute reference, and ``50`` is the unadjusted 60-minute
reference.  Since the public annual files do not expose a complete 15-minute
series, stored 15-minute bars are additionally aggregated to 30 and 60
minutes before comparison.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from pymongo import MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings


THS_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/{line_type}/{year}.js"
THS_STOCK_PAGE_URL = "https://stockpage.10jqka.com.cn/{code}/"
THS_DIRECT_KLINE_URL = (
    "https://quota-h.10jqka.com.cn/"
    "fuyao/common_hq_aggr/quote/v1/single_kline"
)
THS_DIRECT_KLINE_CACHE_URL = (
    "https://quota-h.10jqka.com.cn/"
    "fuyao/common_hq_aggr_cache/quote/v1/single_kline"
)
THS_DIRECT_KLINE_URLS = frozenset(
    {THS_DIRECT_KLINE_URL, THS_DIRECT_KLINE_CACHE_URL}
)
CHINA_TZ = timezone(timedelta(hours=8))
FIELDS = ("open", "high", "low", "close", "volume", "amount")
OHLC_FIELDS = FIELDS[:4]

DAILY_COLLECTION = "stock_history_daily_bars"
MINUTE_60_COLLECTION = "stock_history_60m_bars"
MINUTE_15_COLLECTION = "stock_history_15m_bars"
ALLOWED_COLLECTIONS = frozenset(
    {DAILY_COLLECTION, MINUTE_60_COLLECTION, MINUTE_15_COLLECTION}
)

DEFAULT_CODES = (
    "000001",
    "000333",
    "002594",
    "300059",
    "300750",
    "600000",
    "600519",
    "601318",
    "688981",
)
DEFAULT_DAILY_YEARS = tuple(range(2015, 2027))
DEFAULT_MINUTE_YEARS = tuple(range(2020, 2027))

EXPECTED_15_TIMES = (
    "09:45:00",
    "10:00:00",
    "10:15:00",
    "10:30:00",
    "10:45:00",
    "11:00:00",
    "11:15:00",
    "11:30:00",
    "13:15:00",
    "13:30:00",
    "13:45:00",
    "14:00:00",
    "14:15:00",
    "14:30:00",
    "14:45:00",
    "15:00:00",
)
EXPECTED_30_TIMES = (
    "10:00:00",
    "10:30:00",
    "11:00:00",
    "11:30:00",
    "13:30:00",
    "14:00:00",
    "14:30:00",
    "15:00:00",
)
EXPECTED_60_TIMES = ("10:30:00", "11:30:00", "14:00:00", "15:00:00")

TARGET_GROUPS = {
    30: tuple(
        tuple(EXPECTED_15_TIMES[index : index + 2])
        for index in range(0, len(EXPECTED_15_TIMES), 2)
    ),
    60: tuple(
        tuple(EXPECTED_15_TIMES[index : index + 4])
        for index in range(0, len(EXPECTED_15_TIMES), 4)
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读验收历史日线、60分钟和15分钟量化行情（同花顺口径）"
    )
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES))
    parser.add_argument(
        "--daily-years", default=",".join(map(str, DEFAULT_DAILY_YEARS))
    )
    parser.add_argument(
        "--minute-years", default=",".join(map(str, DEFAULT_MINUTE_YEARS))
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/reports/stock_history_ths_validation.json"),
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.2,
        help="同花顺年度文件请求间隔秒数，避免触发临时限流",
    )
    parser.add_argument(
        "--direct-15m",
        action="store_true",
        help="额外用当前同花顺actual直连接口抽验原始15分钟线",
    )
    parser.add_argument(
        "--direct-15m-count",
        type=int,
        default=400,
        help="每只股票每年直连抽验的15分钟线数量",
    )
    parser.add_argument(
        "--direct-15m-max-attempts",
        type=int,
        default=20,
        help="错误时间窗口的最大重试次数",
    )
    return parser.parse_args()


def parse_codes(value: str) -> list[str]:
    codes = sorted(
        {str(item).strip().zfill(6) for item in value.split(",") if item.strip()}
    )
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("codes必须是逗号分隔的6位数字")
    return codes


def parse_years(value: str) -> list[int]:
    years = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not years or any(year < 1990 or year > 2100 for year in years):
        raise ValueError("years包含非法年份")
    return years


def parse_ths_payload(
    text: str, *, code: str, year: int
) -> list[dict[str, Any]]:
    match = re.search(r"\((\{.*\})\)\s*$", text)
    if match is None:
        raise ValueError("同花顺响应不是可解析的JS包装JSON")
    payload = json.loads(match.group(1))
    rows: list[dict[str, Any]] = []
    for raw in str(payload.get("data") or "").split(";"):
        if not raw:
            continue
        fields = raw.split(",")
        if len(fields) < 7:
            raise ValueError(f"同花顺字段数异常: {raw[:80]}")
        stamp = fields[0]
        if not stamp.startswith(str(year)):
            raise ValueError(
                f"同花顺返回错误年份: 请求{year}, 实际时间{stamp!r}"
            )
        if len(stamp) == 8:
            key = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        elif len(stamp) == 12:
            key = (
                f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                f"T{stamp[8:10]}:{stamp[10:12]}:00+08:00"
            )
        else:
            raise ValueError(f"同花顺时间格式异常: {stamp!r}")
        rows.append(
            {
                "code": code,
                "year": year,
                "key": key,
                "open": float(fields[1]),
                "high": float(fields[2]),
                "low": float(fields[3]),
                "close": float(fields[4]),
                "volume": float(fields[5]),
                "amount": float(fields[6]),
            }
        )
    return rows


def extract_webpack_url(html: str) -> str:
    match = re.search(
        r'((?:https?:)?//[^"\']+/_next/static/chunks/webpack-[^"\']+\.js)',
        html,
    )
    if match is None:
        raise ValueError("同花顺页面未暴露webpack运行时地址")
    url = match.group(1)
    return f"https:{url}" if url.startswith("//") else url


def extract_webpack_chunk_urls(runtime: str, webpack_url: str) -> list[str]:
    try:
        start = runtime.index("r.u=")
        end = runtime.index(",r.miniCssF", start)
    except ValueError as exc:
        raise ValueError("同花顺webpack运行时缺少动态分包映射") from exc
    maps = re.findall(r"\{[^{}]+\}", runtime[start:end])
    if len(maps) != 2:
        raise ValueError("同花顺webpack动态分包映射格式变化")
    prefixes = dict(re.findall(r'(\d+):"([^"]+)"', maps[0]))
    hashes = dict(re.findall(r'(\d+):"([^"]+)"', maps[1]))
    if not hashes:
        raise ValueError("同花顺webpack动态分包哈希为空")
    root = webpack_url.split("static/chunks/webpack-", 1)[0]
    return [
        urljoin(
            root,
            f"static/chunks/{prefixes.get(chunk_id, chunk_id)}.{digest}.js",
        )
        for chunk_id, digest in hashes.items()
    ]


def extract_ths_frontend_credentials(text: str) -> tuple[str, str] | None:
    match = re.search(
        r'\{id:"(hxkline-[^"]+)",token:"(eyJ[^"]+)"\}', text
    )
    return match.groups() if match is not None else None


def parse_ths_direct_payload(
    payload: dict[str, Any], *, code: str
) -> list[dict[str, Any]]:
    if payload.get("status_code") != 0:
        raise ValueError(
            f"同花顺直连接口错误: {payload.get('status_code')} "
            f"{payload.get('status_msg')}"
        )
    quote_data = (payload.get("data") or {}).get("quote_data") or []
    quote = next(
        (item for item in quote_data if str(item.get("code")) == code), None
    )
    if quote is None:
        return []
    fields = [str(value) for value in quote.get("data_fields") or []]
    required = {"1", "7", "8", "9", "11", "13", "19"}
    if not required.issubset(fields):
        raise ValueError(f"同花顺15分钟字段缺失: {fields}")
    rows: list[dict[str, Any]] = []
    for raw in quote.get("value") or []:
        values = dict(zip(fields, raw))
        def optional_number(field: str) -> float | None:
            value = values[field]
            return None if value in (None, "") else float(value)

        timestamp_ms = int(values["1"])
        key = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=CHINA_TZ
        ).isoformat(timespec="seconds")
        rows.append(
            {
                "code": code,
                "key": key,
                "timestamp_ms": timestamp_ms,
                "open": float(values["7"]),
                "high": float(values["8"]),
                "low": float(values["9"]),
                "close": float(values["11"]),
                "volume": optional_number("13"),
                "amount": optional_number("19"),
            }
        )
    return rows


def discover_ths_direct_headers(
    session: requests.Session, *, code: str
) -> tuple[dict[str, str], dict[str, Any]]:
    page_url = THS_STOCK_PAGE_URL.format(code=code)
    user_agent = "Mozilla/5.0"

    def get_text(url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = session.get(
                    url,
                    headers={"User-Agent": user_agent, "Referer": page_url},
                    timeout=30,
                )
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise ValueError(f"同花顺前端资源请求失败: {last_error}")

    html = get_text(page_url)
    webpack_url = extract_webpack_url(html)
    runtime = get_text(webpack_url)
    credential_source = None
    credentials = None
    chunk_urls = extract_webpack_chunk_urls(runtime, webpack_url)
    for chunk_url in chunk_urls:
        chunk = get_text(chunk_url)
        credentials = extract_ths_frontend_credentials(chunk)
        if credentials is not None:
            credential_source = chunk_url
            break
    if credentials is None or credential_source is None:
        raise ValueError("同花顺当前前端分包未找到公开行情凭据")
    source_id, token = credentials
    session.cookies.clear()
    headers = {
        "User-Agent": user_agent,
        "Referer": page_url,
        "Origin": "https://stockpage.10jqka.com.cn",
        "Content-Type": "application/json",
        "X-Fuyao-Auth": token,
        "Source-Id": source_id,
        "Platform": "hxkline",
        "X-Auth-Type": "ths",
        "X-Auth-Version": "1.0",
        "X-Auth-ProgId": "7047",
        "X-Auth-AppName": "AINVEST",
    }
    return headers, {
        "page_url": page_url,
        "webpack_url": webpack_url,
        "credential_source": credential_source,
        "source_id": source_id,
        "token_extracted_at_runtime": True,
        "cookie_free": not bool(session.cookies),
        "sw8_required": False,
        "chunks_scanned": len(chunk_urls),
    }


def fetch_ths_direct_bars(
    session: Any,
    *,
    headers: dict[str, str],
    code: str,
    market: str,
    time_period: str,
    end_time_ms: int,
    adjust_type: str = "actual",
    endpoint: str = THS_DIRECT_KLINE_URL,
    count: int = 400,
    allow_partial: bool = False,
    allow_oversized: bool = False,
    minimum_rows: int = 1,
    partial_confirmations: int = 1,
    max_attempts: int = 20,
    retry_delay: float = 0.05,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    if time_period not in {
        "min_1",
        "min_5",
        "min_15",
        "min_30",
        "min_60",
        "min_120",
        "day_1",
    }:
        raise ValueError(f"不支持的同花顺周期: {time_period}")
    if adjust_type not in {"actual", "forward", "backward"}:
        raise ValueError(f"不支持的同花顺复权类型: {adjust_type}")
    if endpoint not in THS_DIRECT_KLINE_URLS:
        raise ValueError(f"不支持的同花顺K线端点: {endpoint}")
    if (
        count <= 1
        or minimum_rows <= 0
        or partial_confirmations <= 0
        or max_attempts <= 0
    ):
        raise ValueError(
            "同花顺窗口条数必须大于1，"
            "最小返回条数和重试次数必须为正数"
        )
    observed: dict[str, int] = {}
    rejected_windows = 0
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        body = {
            "code_list": [{"codes": [code], "market": market}],
            "trade_class": "intraday",
            "time_period": time_period,
            "trade_date": -1,
            "begin_time": -count,
            "end_time": end_time_ms,
            "adjust_type": adjust_type,
            "gpid": 1,
        }
        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=body,
                timeout=30,
            )
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            rows = parse_ths_direct_payload(response.json(), code=code)
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts and retry_delay > 0:
                time.sleep(retry_delay)
            continue

        timestamps = [int(row["timestamp_ms"]) for row in rows]
        signature = (
            f"rows={len(rows)},first={timestamps[0] if timestamps else None},"
            f"last={timestamps[-1] if timestamps else None}"
        )
        observed[signature] = observed.get(signature, 0) + 1
        valid_count = (
            len(rows) >= minimum_rows
            if allow_oversized
            else (
                minimum_rows <= len(rows) <= count
                and (allow_partial or len(rows) == count)
            )
        )
        valid = (
            valid_count
            and timestamps == sorted(timestamps)
            and len(timestamps) == len(set(timestamps))
            and (end_time_ms == 0 or timestamps[-1] == end_time_ms)
        )
        partial_confirmed = (
            allow_oversized
            or len(rows) == count
            or observed[signature] >= partial_confirmations
        )
        if valid and partial_confirmed:
            return rows, {
                "attempts": attempt,
                "rejected_windows": rejected_windows,
                "observed_windows": observed,
            }
        rejected_windows += 1
        last_error = (
            f"同花顺{time_period} HTTP 200但返回了错误时间窗口"
        )
        if attempt < max_attempts and retry_delay > 0:
            time.sleep(retry_delay)
    return None, {
        "attempts": max_attempts,
        "rejected_windows": rejected_windows,
        "observed_windows": observed,
        "error": last_error or f"同花顺{time_period}窗口为空",
    }


def fetch_ths_direct_15m(
    session: Any,
    *,
    headers: dict[str, str],
    code: str,
    market: str,
    end_time_ms: int,
    adjust_type: str = "actual",
    endpoint: str = THS_DIRECT_KLINE_URL,
    count: int = 400,
    allow_partial: bool = False,
    allow_oversized: bool = False,
    minimum_rows: int = 1,
    partial_confirmations: int = 1,
    max_attempts: int = 20,
    retry_delay: float = 0.05,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    return fetch_ths_direct_bars(
        session,
        headers=headers,
        code=code,
        market=market,
        time_period="min_15",
        end_time_ms=end_time_ms,
        adjust_type=adjust_type,
        endpoint=endpoint,
        count=count,
        allow_partial=allow_partial,
        allow_oversized=allow_oversized,
        minimum_rows=minimum_rows,
        partial_confirmations=partial_confirmations,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )


def fetch_ths(
    session: requests.Session,
    *,
    code: str,
    line_type: str,
    year: int,
) -> tuple[int, list[dict[str, Any]] | None, str | None]:
    url = THS_URL.format(code=code, line_type=line_type, year=year)
    response = None
    last_parse_error: str | None = None
    for attempt in range(3):
        try:
            response = session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": f"https://stockpage.10jqka.com.cn/{code}/",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == 2:
                return 0, None, f"{type(exc).__name__}: {exc}"
            time.sleep(2**attempt)
            continue
        if response.status_code == 200:
            try:
                return response.status_code, parse_ths_payload(
                    response.text, code=code, year=year
                ), None
            except Exception as exc:
                last_parse_error = f"{type(exc).__name__}: {exc}"
                if attempt == 2:
                    return response.status_code, None, last_parse_error
                time.sleep(2**attempt)
                continue
        if response.status_code == 404:
            return response.status_code, None, None
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            return (
                response.status_code,
                None,
                f"HTTPError: 同花顺返回{response.status_code}",
            )
        time.sleep(2**attempt)
    if response is None:
        return 0, None, "RequestError: 未取得同花顺响应"
    return response.status_code, None, last_parse_error or "同花顺响应不可解析"


def load_rows(
    collection: Any,
    *,
    collection_name: str,
    code: str,
    year: int,
) -> list[dict[str, Any]]:
    if collection_name not in ALLOWED_COLLECTIONS:
        raise ValueError(f"禁止读取非量化历史集合: {collection_name}")
    projection = {"_id": 0, "trade_date": 1, "source": 1}
    projection.update({field: 1 for field in FIELDS})
    if collection_name == DAILY_COLLECTION:
        key_field = "trade_date"
    else:
        key_field = "timestamp"
        projection["timestamp"] = 1
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": f"{year}-01-01",
                "$lt": f"{year + 1}-01-01",
            },
        },
        projection,
    )
    return [{**row, "key": str(row[key_field])} for row in rows]


def validate_intraday_structure(
    rows: Iterable[dict[str, Any]], expected_times: tuple[str, ...]
) -> dict[str, int]:
    by_date: dict[str, list[str]] = {}
    for row in rows:
        key = str(row["key"])
        by_date.setdefault(key[:10], []).append(key[11:19])
    bad_dates = sum(
        sorted(times) != list(expected_times) for times in by_date.values()
    )
    return {"trade_dates": len(by_date), "bad_trade_dates": bad_dates}


def aggregate_15m(
    rows: Iterable[dict[str, Any]], *, target_minutes: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    groups = TARGET_GROUPS[target_minutes]
    by_date_time: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        key = str(row["key"])
        by_date_time.setdefault(key[:10], {})[key[11:19]] = row

    aggregated: list[dict[str, Any]] = []
    incomplete_groups = 0
    for trade_date, by_time in sorted(by_date_time.items()):
        for group in groups:
            members = [by_time.get(value) for value in group]
            if any(member is None for member in members):
                incomplete_groups += 1
                continue
            complete = [member for member in members if member is not None]
            aggregated.append(
                {
                    "key": f"{trade_date}T{group[-1]}+08:00",
                    "open": float(complete[0]["open"]),
                    "high": max(float(row["high"]) for row in complete),
                    "low": min(float(row["low"]) for row in complete),
                    "close": float(complete[-1]["close"]),
                    "volume": sum(float(row["volume"]) for row in complete),
                    "amount": sum(float(row["amount"]) for row in complete),
                }
            )
    return aggregated, {
        "trade_dates": len(by_date_time),
        "expected_groups": len(by_date_time) * len(groups),
        "complete_groups": len(aggregated),
        "incomplete_groups": incomplete_groups,
    }


def compare(
    official: Iterable[dict[str, Any]], current: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    official_by_key = {str(row["key"]): row for row in official}
    current_by_key = {str(row["key"]): row for row in current}
    common = sorted(set(official_by_key) & set(current_by_key))
    missing = sorted(set(official_by_key) - set(current_by_key))
    extra = sorted(set(current_by_key) - set(official_by_key))
    zero_volume_flat_candidates = [
        key
        for key in extra
        if float(current_by_key[key].get("volume", 0)) == 0
        and float(current_by_key[key].get("amount", 0)) == 0
        and len(
            {
                float(current_by_key[key][field]) for field in OHLC_FIELDS
            }
        )
        == 1
    ]
    ohlc_diffs: list[float] = []
    volume_diffs: list[float] = []
    amount_diffs: list[float] = []
    mismatch_examples: list[dict[str, Any]] = []
    field_exact = {field: 0 for field in OHLC_FIELDS}
    exact_ohlc = 0
    within_one_cent = 0
    for key in common:
        left = official_by_key[key]
        right = current_by_key[key]
        diffs = [
            abs(float(right[field]) - float(left[field]))
            for field in OHLC_FIELDS
        ]
        ohlc_diffs.append(max(diffs))
        volume_diffs.append(float(right["volume"]) - float(left["volume"]))
        amount_diffs.append(float(right["amount"]) - float(left["amount"]))
        exact_ohlc += max(diffs) == 0
        within_one_cent += max(diffs) <= 0.0100000001
        for field, diff in zip(OHLC_FIELDS, diffs):
            field_exact[field] += diff == 0
        if max(diffs) > 0:
            mismatch_examples.append(
                {
                    "key": key,
                    "max_abs_diff": max(diffs),
                    "official_ohlc": {
                        field: float(left[field]) for field in OHLC_FIELDS
                    },
                    "current_ohlc": {
                        field: float(right[field]) for field in OHLC_FIELDS
                    },
                }
            )

    def abs_median(values: list[float]) -> float | None:
        return statistics.median(abs(value) for value in values) if values else None

    return {
        "official_rows": len(official_by_key),
        "current_rows": len(current_by_key),
        "common_rows": len(common),
        "missing_official_keys": len(missing),
        "extra_current_keys": len(extra),
        "extra_current_zero_volume_flat_candidates": len(
            zero_volume_flat_candidates
        ),
        "extra_current_traded_or_nonflat_keys": len(extra)
        - len(zero_volume_flat_candidates),
        "ohlc_exact_rows": exact_ohlc,
        "ohlc_exact_rate": exact_ohlc / len(common) if common else None,
        "ohlc_within_one_cent_rows": within_one_cent,
        "ohlc_within_one_cent_rate": (
            within_one_cent / len(common) if common else None
        ),
        "field_exact_rows": field_exact,
        "field_exact_rates": {
            field: count / len(common) if common else None
            for field, count in field_exact.items()
        },
        "ohlc_abs_diff_p50": statistics.median(ohlc_diffs) if ohlc_diffs else None,
        "ohlc_abs_diff_max": max(ohlc_diffs) if ohlc_diffs else None,
        "volume_abs_diff_p50": abs_median(volume_diffs),
        "volume_abs_diff_max": max(
            (abs(value) for value in volume_diffs), default=None
        ),
        "amount_abs_diff_p50": abs_median(amount_diffs),
        "amount_abs_diff_max": max(
            (abs(value) for value in amount_diffs), default=None
        ),
        "official_only_examples": missing[:5],
        "current_only_examples": extra[:5],
        "zero_volume_flat_candidate_examples": zero_volume_flat_candidates[:5],
        "worst_ohlc_examples": sorted(
            mismatch_examples,
            key=lambda item: (-item["max_abs_diff"], item["key"]),
        )[:5],
    }


def split_current_by_reference_keys(
    official: Iterable[dict[str, Any]], current: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate comparable rows from rows the sampled reference did not return.

    The direct Tonghuashun endpoint can return a valid, ordered 400-row window
    while omitting a complete trading day inside that window.  Such rows are a
    reference coverage gap, not evidence that the database contains bad bars.
    """
    official_keys = {str(row["key"]) for row in official}
    comparable: list[dict[str, Any]] = []
    unjudged: list[dict[str, Any]] = []
    for row in current:
        target = comparable if str(row["key"]) in official_keys else unjudged
        target.append(row)
    return comparable, unjudged


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    checks = sorted({str(item["check"]) for item in results})
    for check in checks:
        items = [item for item in results if item["check"] == check]
        comparisons = [item["comparison"] for item in items if "comparison" in item]
        common = sum(item["common_rows"] for item in comparisons)
        exact = sum(item["ohlc_exact_rows"] for item in comparisons)
        field_exact = {
            field: sum(
                int(item.get("field_exact_rows", {}).get(field, 0))
                for item in comparisons
            )
            for field in OHLC_FIELDS
        }
        within_one_cent = sum(
            int(item.get("ohlc_within_one_cent_rows", 0))
            for item in comparisons
        )
        summary[check] = {
            "requested_files": len(items),
            "official_files_available": len(comparisons),
            "official_files_unavailable": sum(
                item.get("ths_rows") is None
                and "ths_rows" in item
                and "error" not in item
                for item in items
            ),
            "upstream_errors": sum("error" in item for item in items),
            "official_rows": sum(item["official_rows"] for item in comparisons),
            "current_rows_in_available_files": sum(
                item["current_rows"] for item in comparisons
            ),
            "common_rows": common,
            "missing_official_keys": sum(
                item["missing_official_keys"] for item in comparisons
            ),
            "extra_current_keys": sum(
                item["extra_current_keys"] for item in comparisons
            ),
            "unjudged_current_rows_absent_from_direct_reference": sum(
                int(item.get("unjudged_current_rows_absent_from_direct_reference", 0))
                for item in items
            ),
            "ohlc_exact_rows": exact,
            "ohlc_exact_rate": exact / common if common else None,
            "ohlc_within_one_cent_rows": within_one_cent,
            "ohlc_within_one_cent_rate": (
                within_one_cent / common if common else None
            ),
            "field_exact_rows": field_exact,
            "field_exact_rates": {
                field: count / common if common else None
                for field, count in field_exact.items()
            },
        }
    cross_comparisons = [
        item["tonghuashun_15m_to_static_60m"]["comparison"]
        for item in results
        if "comparison" in item.get("tonghuashun_15m_to_static_60m", {})
    ]
    if cross_comparisons:
        common = sum(item["common_rows"] for item in cross_comparisons)
        exact = sum(item["ohlc_exact_rows"] for item in cross_comparisons)
        summary["tonghuashun_15m_to_static_60m_self_check"] = {
            "available_windows": len(cross_comparisons),
            "static_60m_rows": sum(
                item["official_rows"] for item in cross_comparisons
            ),
            "direct_15m_aggregated_rows": sum(
                item["current_rows"] for item in cross_comparisons
            ),
            "common_rows": common,
            "missing_static_60m_keys": sum(
                item["missing_official_keys"] for item in cross_comparisons
            ),
            "extra_direct_aggregated_keys": sum(
                item["extra_current_keys"] for item in cross_comparisons
            ),
            "ohlc_exact_rows": exact,
            "ohlc_exact_rate": exact / common if common else None,
        }
    return summary


def make_item(
    *, check: str, code: str, year: int, status: int
) -> dict[str, Any]:
    return {"check": check, "code": code, "year": year, "ths_http_status": status}


def main() -> None:
    args = parse_args()
    codes = parse_codes(args.codes)
    daily_years = parse_years(args.daily_years)
    minute_years = parse_years(args.minute_years)
    settings = Settings()
    if args.direct_15m_count <= 1 or args.direct_15m_max_attempts <= 0:
        raise ValueError("direct-15m-count必须大于1，最大重试次数必须为正数")
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10_000)
    database = client[settings.mongo_db_name]
    session = requests.Session()
    direct_session = requests.Session() if args.direct_15m else None
    direct_headers: dict[str, str] | None = None
    direct_metadata: dict[str, Any] | None = None
    direct_init_error: str | None = None
    if direct_session is not None:
        try:
            direct_headers, direct_metadata = discover_ths_direct_headers(
                direct_session, code=codes[0]
            )
        except Exception as exc:
            direct_init_error = f"{type(exc).__name__}: {exc}"
    results: list[dict[str, Any]] = []
    fetch_cache: dict[tuple[str, str, int], tuple[int, Any, Any]] = {}

    def cached_fetch(code: str, line_type: str, year: int) -> tuple[int, Any, Any]:
        key = (code, line_type, year)
        if key not in fetch_cache:
            fetch_cache[key] = fetch_ths(
                session, code=code, line_type=line_type, year=year
            )
            if args.request_delay > 0:
                time.sleep(args.request_delay)
        return fetch_cache[key]

    try:
        for code in codes:
            for year in daily_years:
                status, official, error = cached_fetch(code, "00", year)
                current = load_rows(
                    database[DAILY_COLLECTION],
                    collection_name=DAILY_COLLECTION,
                    code=code,
                    year=year,
                )
                item = make_item(
                    check="daily_raw", code=code, year=year, status=status
                )
                if error:
                    item["error"] = error
                elif official is None:
                    item.update(
                        {
                            "ths_rows": None,
                            "current_rows": len(current),
                            "note": "同花顺公开v6年度文件不可用，不能据此判定正误",
                        }
                    )
                else:
                    item["comparison"] = compare(official, current)
                results.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)

            for year in minute_years:
                raw_60 = load_rows(
                    database[MINUTE_60_COLLECTION],
                    collection_name=MINUTE_60_COLLECTION,
                    code=code,
                    year=year,
                )
                status, official_60, error = cached_fetch(code, "50", year)
                item = make_item(
                    check="60m_raw", code=code, year=year, status=status
                )
                item["current_structure"] = validate_intraday_structure(
                    raw_60, EXPECTED_60_TIMES
                )
                if error:
                    item["error"] = error
                elif official_60 is None:
                    item.update(
                        {
                            "ths_rows": None,
                            "current_rows": len(raw_60),
                            "note": "同花顺公开v6年度文件不可用，不能据此判定正误",
                        }
                    )
                else:
                    item["official_structure"] = validate_intraday_structure(
                        official_60, EXPECTED_60_TIMES
                    )
                    item["comparison"] = compare(official_60, raw_60)
                results.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)

                raw_15 = load_rows(
                    database[MINUTE_15_COLLECTION],
                    collection_name=MINUTE_15_COLLECTION,
                    code=code,
                    year=year,
                )
                raw_15_structure = validate_intraday_structure(
                    raw_15, EXPECTED_15_TIMES
                )
                if args.direct_15m:
                    direct_item = make_item(
                        check="15m_direct_actual",
                        code=code,
                        year=year,
                        status=0,
                    )
                    direct_item["current_structure"] = raw_15_structure
                    if year < 2023:
                        direct_item.update(
                            {
                                "ths_rows": None,
                                "current_rows": len(raw_15),
                                "note": (
                                    "同花顺当前公开15分钟源约从2023-08开始，"
                                    "不能据此验收更早年份"
                                ),
                            }
                        )
                    elif direct_init_error is not None:
                        direct_item.update(
                            {"ths_rows": None, "error": direct_init_error}
                        )
                    elif not raw_15:
                        direct_item.update(
                            {
                                "ths_rows": None,
                                "current_rows": 0,
                                "note": "当前库本年没有15分钟记录，无法选择抽验锚点",
                            }
                        )
                    else:
                        anchor_key = max(str(row["key"]) for row in raw_15)
                        end_time_ms = int(
                            datetime.fromisoformat(anchor_key).timestamp() * 1000
                        )
                        official_direct, direct_audit = fetch_ths_direct_15m(
                            direct_session,
                            headers=direct_headers or {},
                            code=code,
                            market="17" if code.startswith("6") else "33",
                            end_time_ms=end_time_ms,
                            count=args.direct_15m_count,
                            max_attempts=args.direct_15m_max_attempts,
                        )
                        direct_item["request"] = {
                            "time_period": "min_15",
                            "adjust_type": "actual",
                            "begin_time": -args.direct_15m_count,
                            "end_time": end_time_ms,
                            "cookie_free": True,
                            "browser_runtime": False,
                        }
                        direct_item["fetch_audit"] = direct_audit
                        if official_direct is None:
                            direct_item.update(
                                {
                                    "ths_rows": None,
                                    "error": direct_audit.get("error"),
                                }
                            )
                        else:
                            official_year = [
                                row
                                for row in official_direct
                                if str(row["key"]).startswith(f"{year}-")
                            ]
                            if not official_year:
                                direct_item.update(
                                    {
                                        "ths_rows": None,
                                        "error": "同花顺正确窗口不含请求年份",
                                    }
                                )
                            else:
                                first_key = min(
                                    str(row["key"]) for row in official_year
                                )
                                last_key = max(
                                    str(row["key"]) for row in official_year
                                )
                                current_window = [
                                    row
                                    for row in raw_15
                                    if first_key <= str(row["key"]) <= last_key
                                ]
                                comparable_current, unjudged_current = (
                                    split_current_by_reference_keys(
                                        official_year, current_window
                                    )
                                )
                                direct_item["ths_http_status"] = 200
                                direct_item["official_window"] = {
                                    "first": first_key,
                                    "last": last_key,
                                    "rows": len(official_year),
                                }
                                direct_item["official_structure"] = (
                                    validate_intraday_structure(
                                        official_year, EXPECTED_15_TIMES
                                    )
                                )
                                direct_item[
                                    "unjudged_current_rows_absent_from_direct_reference"
                                ] = len(unjudged_current)
                                direct_item[
                                    "unjudged_current_examples"
                                ] = sorted(
                                    str(row["key"]) for row in unjudged_current
                                )[:5]
                                direct_item["reference_gap_note"] = (
                                    "这些库内时间戳未被本次同花顺直连抽样窗口返回，"
                                    "仅记为参考源覆盖缺口，不判定为库内多余或错误数据"
                                )
                                direct_item["comparison"] = compare(
                                    official_year, comparable_current
                                )
                                direct_60, direct_aggregation = aggregate_15m(
                                    official_year, target_minutes=60
                                )
                                cross_check: dict[str, Any] = {
                                    "aggregation": direct_aggregation,
                                    "note": (
                                        "同花顺actual 15分钟自身聚合后，与同花顺"
                                        "v6未复权60分钟交叉验证"
                                    ),
                                }
                                if error:
                                    cross_check["error"] = error
                                elif official_60 is None:
                                    cross_check["note"] += "；60分钟年度文件不可用"
                                else:
                                    direct_60_first = min(
                                        str(row["key"]) for row in direct_60
                                    )
                                    direct_60_last = max(
                                        str(row["key"]) for row in direct_60
                                    )
                                    static_60_window = [
                                        row
                                        for row in official_60
                                        if direct_60_first
                                        <= str(row["key"])
                                        <= direct_60_last
                                    ]
                                    cross_check["comparison"] = compare(
                                        static_60_window, direct_60
                                    )
                                direct_item[
                                    "tonghuashun_15m_to_static_60m"
                                ] = cross_check
                    results.append(direct_item)
                    print(
                        json.dumps(direct_item, ensure_ascii=False), flush=True
                    )
                for target, line_type, expected_times in (
                    (30, "40", EXPECTED_30_TIMES),
                    (60, "50", EXPECTED_60_TIMES),
                ):
                    aggregated, aggregation = aggregate_15m(
                        raw_15, target_minutes=target
                    )
                    status, official, error = cached_fetch(code, line_type, year)
                    item = make_item(
                        check=f"15m_to_{target}m_raw",
                        code=code,
                        year=year,
                        status=status,
                    )
                    item["source_15m_structure"] = raw_15_structure
                    item["aggregation"] = aggregation
                    if error:
                        item["error"] = error
                    elif official is None:
                        item.update(
                            {
                                "ths_rows": None,
                                "current_rows": len(aggregated),
                                "note": "同花顺公开v6年度文件不可用，不能据此判定正误",
                            }
                        )
                    else:
                        item["official_structure"] = validate_intraday_structure(
                            official, expected_times
                        )
                        item["comparison"] = compare(official, aggregated)
                    results.append(item)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
    finally:
        client.close()
        session.close()
        if direct_session is not None:
            direct_session.close()

    report = {
        "read_only": True,
        "collections_read": sorted(ALLOWED_COLLECTIONS),
        "tonghuashun_line_types": {
            "00": "unadjusted daily",
            "40": "unadjusted 30-minute",
            "50": "unadjusted 60-minute",
        },
        "tonghuashun_direct_15m": direct_metadata,
        "codes": codes,
        "daily_years": daily_years,
        "minute_years": minute_years,
        "summary": summarize(results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report={args.output}")


if __name__ == "__main__":
    main()
