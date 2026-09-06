"""Download and stage Tonghuashun forward-adjusted quant history.

The script reads its stock universe from the independently maintained quant
daily-history collection.  It never reads or modifies the online service
collections.  By default it is a dry run.  ``--apply`` writes only to the
seven hard-coded ``*_ths_forward_stage`` collections.  Production writes
require both ``--destination production`` and ``--confirm-production-write``.

Tonghuashun's public minute history currently reaches back only to roughly
August 2023.  The importer reports that source floor and never fills an older
gap with another provider.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar

import requests
from pymongo import ASCENDING, MongoClient, UpdateOne

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings  # noqa: E402
from app.manually_execute_script.stock_history_common import (  # noqa: E402
    CN_TZ,
    StockTarget,
    market_for_code,
    non_negative_int,
    parse_date,
    positive_int,
    today_cn,
)
from app.manually_execute_script.sync_ths_2026_shadow import (  # noqa: E402
    discover_ths_market_id,
)
from app.manually_execute_script.validate_stock_history_against_ths import (  # noqa: E402
    THS_DIRECT_KLINE_CACHE_URL,
    THS_DIRECT_KLINE_URL,
    discover_ths_direct_headers,
    fetch_ths,
    fetch_ths_direct_bars,
)


PRODUCTION_COLLECTIONS = {
    "daily": "stock_history_daily_bars",
    "120m": "stock_history_120m_bars",
    "60m": "stock_history_60m_bars",
    "30m": "stock_history_30m_bars",
    "15m": "stock_history_15m_bars",
    "5m": "stock_history_5m_bars",
    "1m": "stock_history_1m_bars",
}
STAGE_COLLECTIONS = {
    key: f"{name}_ths_forward_stage"
    for key, name in PRODUCTION_COLLECTIONS.items()
}
QUANT_UNIVERSE_COLLECTION = STAGE_COLLECTIONS["daily"]
ALLOWED_DESTINATIONS = frozenset(
    (*PRODUCTION_COLLECTIONS.values(), *STAGE_COLLECTIONS.values())
)
PERIOD_CONFIG = {
    "daily": {"time_period": "day_1", "interval": "1d"},
    "120m": {"time_period": "min_120", "interval": "120m"},
    "60m": {"time_period": "min_60", "interval": "60m"},
    "30m": {"time_period": "min_30", "interval": "30m"},
    "15m": {"time_period": "min_15", "interval": "15m"},
    "5m": {"time_period": "min_5", "interval": "5m"},
    "1m": {"time_period": "min_1", "interval": "1m"},
}
# The annual file remains available for independent 60-minute audits, but the
# acquisition path below uses the webpage's native single_kline period for
# every interval.  No interval is generated from another interval.
STATIC_LINE_TYPES = {"60m": "51"}
THS_ALL_DAILY_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/all.js"
THS_SOURCES = {
    "daily": "tonghuashun.single_kline.forward",
    "120m": "tonghuashun.single_kline.forward",
    "60m": "tonghuashun.single_kline.forward",
    "30m": "tonghuashun.single_kline.forward",
    "15m": "tonghuashun.single_kline.forward",
    "5m": "tonghuashun.single_kline.forward",
    "1m": "tonghuashun.single_kline.forward",
}
DEFAULT_DAILY_START = date(2023, 8, 14)
DEFAULT_MINUTE_START = date(2023, 8, 14)
PUBLIC_MINUTE_FLOOR_LATEST = {
    "120m": date(2023, 9, 30),
    "60m": date(2023, 9, 30),
    "30m": date(2023, 9, 30),
    "15m": date(2023, 9, 30),
    "5m": date(2025, 10, 31),
    "1m": date(2026, 6, 30),
}
NATIVE_SOURCE_FLOOR_LATEST = {
    "120m": date(2023, 9, 30),
    "60m": date(2023, 9, 30),
    "30m": date(2023, 9, 30),
    "15m": date(2023, 9, 30),
    "5m": date(2023, 9, 30),
    "1m": date(2026, 6, 30),
}
PUBLIC_BJ_MINUTE_FLOOR_LATEST = date(2024, 3, 31)


def session_times(start_minute: int, end_minute: int, step: int) -> tuple[str, ...]:
    return tuple(
        f"{minute // 60:02d}:{minute % 60:02d}:00"
        for minute in range(start_minute, end_minute + 1, step)
    )


EXPECTED_TIMES_BY_PERIOD = {
    "120m": ("11:30:00", "15:00:00"),
    "60m": ("10:30:00", "11:30:00", "14:00:00", "15:00:00"),
    "30m": (
        "10:00:00",
        "10:30:00",
        "11:00:00",
        "11:30:00",
        "13:30:00",
        "14:00:00",
        "14:30:00",
        "15:00:00",
    ),
    "15m": session_times(9 * 60 + 45, 11 * 60 + 30, 15)
    + session_times(13 * 60 + 15, 15 * 60, 15),
    "5m": session_times(9 * 60 + 35, 11 * 60 + 30, 5)
    + session_times(13 * 60 + 5, 15 * 60, 5),
    "1m": session_times(9 * 60 + 30, 11 * 60 + 30, 1)
    + session_times(13 * 60 + 1, 15 * 60, 1),
}
MAX_SAFE_WORKERS = 32

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class FetchResult:
    target: StockTarget
    status: str
    rows: dict[str, list[dict[str, Any]]]
    audit: dict[str, Any]
    error: str | None = None


def safe_worker_count(value: str) -> int:
    workers = positive_int(value)
    if workers > MAX_SAFE_WORKERS:
        raise argparse.ArgumentTypeError(
            f"workers不能超过{MAX_SAFE_WORKERS}，避免历史行情结果耗尽内存"
        )
    return workers


def iter_bounded_results(
    executor: ThreadPoolExecutor,
    items: Iterable[T],
    submit: Callable[[T], Future[R]],
    *,
    max_in_flight: int,
) -> Iterator[R]:
    """Yield results while retaining at most ``max_in_flight`` futures."""
    if max_in_flight < 1:
        raise ValueError("max_in_flight必须大于0")

    item_iterator = iter(items)
    pending: set[Future[R]] = set()
    for _ in range(max_in_flight):
        try:
            item = next(item_iterator)
        except StopIteration:
            break
        pending.add(submit(item))

    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            result = future.result()
            yield result
            del result
            try:
                item = next(item_iterator)
            except StopIteration:
                continue
            pending.add(submit(item))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "独立抓取并验收同花顺网页前复权日线、120/60/30/15/5/1分钟历史。"
        )
    )
    parser.add_argument("--daily-start-date", type=parse_date, default=DEFAULT_DAILY_START)
    parser.add_argument(
        "--minute-start-date", type=parse_date, default=DEFAULT_MINUTE_START
    )
    parser.add_argument("--end-date", type=parse_date, default=today_cn())
    parser.add_argument("--snapshot-date", type=parse_date, default=None)
    parser.add_argument("--only-code", default=None)
    parser.add_argument(
        "--market",
        choices=("HS", "SH", "SZ", "BJ"),
        default=None,
        help="HS表示仅沪深两市。",
    )
    parser.add_argument("--offset", type=non_negative_int, default=0)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--workers", type=safe_worker_count, default=2)
    parser.add_argument("--page-size", type=positive_int, default=400)
    parser.add_argument("--max-pages", type=positive_int, default=100)
    parser.add_argument("--window-retries", type=positive_int, default=12)
    parser.add_argument("--max-attempts", type=positive_int, default=30)
    parser.add_argument("--retry-delay", type=float, default=0.05)
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="以截止日最后一根K线为时间锚点，只分页抓取并验收本次缺口。",
    )
    parser.add_argument(
        "--periods",
        default="daily,120m,60m,30m,15m,5m,1m",
        help="逗号分隔，可选 daily,120m,60m,30m,15m,5m,1m。",
    )
    parser.add_argument(
        "--destination", choices=("stage", "production"), default="stage"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="只处理尚未在全部目标周期集合中完成暂存的股票。",
    )
    parser.add_argument(
        "--skip-codes-file",
        type=Path,
        default=None,
        help="跳过文件中每行一个、已完整验证并写入的6位股票代码。",
    )
    parser.add_argument(
        "--confirm-production-write",
        action="store_true",
        help="写正式集合时必须额外显式确认。",
    )
    parser.add_argument("--allow-intraday", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/reports/stock_history_ths_forward_sync.json"),
    )
    return parser


def parse_periods(value: str) -> tuple[str, ...]:
    periods = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    invalid = set(periods) - set(PERIOD_CONFIG)
    if not periods or invalid:
        raise ValueError(f"periods包含非法值: {sorted(invalid)}")
    return periods


def load_skip_codes(path: Path) -> frozenset[str]:
    codes: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        code = raw_line.strip()
        if not code:
            continue
        if re.fullmatch(r"\d{6}", code) is None:
            raise ValueError(f"skip-codes-file第{line_number}行不是6位股票代码: {code}")
        codes.add(code)
    return frozenset(codes)


def exclude_skipped_targets(
    targets: Iterable[StockTarget], skip_codes: frozenset[str]
) -> list[StockTarget]:
    return [target for target in targets if target.code not in skip_codes]


def resolve_snapshot_date(
    database: Any,
    *,
    collection_name: str,
    explicit: date | None,
) -> str:
    if explicit is not None:
        return explicit.isoformat()
    recent = list(
        database[collection_name].aggregate(
            [
                {"$match": {"adjust": "qfq"}},
                {"$group": {"_id": "$trade_date", "rows": {"$sum": 1}}},
                {"$sort": {"_id": -1}},
                {"$limit": 20},
            ],
            allowDiskUse=True,
        )
    )
    if not recent:
        raise RuntimeError(f"量化日线集合没有qfq股票快照: {collection_name}")
    peak = max(int(item["rows"]) for item in recent)
    threshold = peak * 0.98
    complete = [item for item in recent if int(item["rows"]) >= threshold]
    if not complete:
        raise RuntimeError("无法识别完整股票快照")
    return str(max(complete, key=lambda item: str(item["_id"]))["_id"])


def load_targets(
    database: Any,
    *,
    collection_name: str,
    snapshot_date: str,
    only_code: str | None,
    market_filter: str | None,
    offset: int,
    limit: int | None,
) -> list[StockTarget]:
    match: dict[str, Any] = {"adjust": "qfq", "trade_date": snapshot_date}
    if only_code:
        code = str(only_code).strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            raise ValueError("only-code必须是6位数字")
        match["code"] = code
    rows = database[collection_name].find(
        match,
        {"_id": 0, "code": 1, "name": 1},
        sort=[("code", ASCENDING)],
    )
    targets: list[StockTarget] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("code") or "").strip().zfill(6)
        if code in seen:
            continue
        try:
            market = market_for_code(code)
        except ValueError:
            continue
        if market_filter == "HS" and market not in {"SH", "SZ"}:
            continue
        if market_filter not in {None, "HS"} and market != market_filter:
            continue
        seen.add(code)
        targets.append(
            StockTarget(
                code=code,
                name=str(row.get("name") or "").strip() or None,
                market=market,
            )
        )
    stop = None if limit is None else offset + limit
    return targets[offset:stop]


def default_market_id(target: StockTarget) -> str | None:
    if target.market == "SH":
        return "17"
    if target.market == "SZ":
        return "33"
    return None


def reference_floor_not_after(
    daily_reference_dates: Iterable[str], cutoff: date
) -> date:
    """Move a source-floor guard to the stock's next actual trading day."""
    for value in daily_reference_dates:
        candidate = date.fromisoformat(value)
        if candidate >= cutoff:
            return candidate
    return cutoff


def discover_runtime_headers(
    *, code: str, max_attempts: int, retry_delay: float
) -> tuple[dict[str, str], dict[str, Any]]:
    last_error = "unknown"
    for attempt in range(1, max_attempts + 1):
        discovery_session = requests.Session()
        try:
            headers, audit = discover_ths_direct_headers(
                discovery_session, code=code
            )
            return headers, {**audit, "discovery_attempts": attempt}
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_attempts and retry_delay > 0:
                time.sleep(retry_delay)
        finally:
            discovery_session.close()
    raise RuntimeError(f"同花顺动态前端凭据发现失败: {last_error}")


def fetch_range(
    *,
    headers: dict[str, str],
    target: StockTarget,
    market_id: str,
    period: str,
    start_date: date,
    end_date: date,
    page_size: int,
    max_pages: int,
    window_retries: int,
    max_attempts: int,
    retry_delay: float,
    allow_partial: bool,
    partial_not_after: date | None,
    source_floor_not_after: date | None = None,
    expected_trade_dates: set[str] | None = None,
    incremental: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = PERIOD_CONFIG[period]
    end_time_ms = (
        int(
            datetime.combine(
                end_date,
                (
                    datetime.min.time()
                    if period == "daily"
                    else datetime.strptime("15:00:00", "%H:%M:%S").time()
                ),
                tzinfo=CN_TZ,
            ).timestamp()
            * 1000
        )
        if incremental
        else 0
    )
    previous_oldest: int | None = None
    by_timestamp: dict[int, dict[str, Any]] = {}
    page_audits: list[dict[str, Any]] = []
    source_floor_reached = False

    for page_number in range(1, max_pages + 1):
        large_history_request = page_number == 1 and end_time_ms == 0
        request_count = 3200 if large_history_request else page_size
        last_window_error = "unknown"
        rows = None
        oldest = 0
        oldest_date = start_date
        for window_retry in range(1, window_retries + 1):
            endpoint = (
                THS_DIRECT_KLINE_CACHE_URL
                if window_retry % 2 == 1
                else THS_DIRECT_KLINE_URL
            )
            anchor_date = (
                datetime.fromtimestamp(end_time_ms / 1000, tz=CN_TZ).date()
                if end_time_ms
                else None
            )
            page_allows_partial = bool(
                allow_partial
                and partial_not_after is not None
                and (
                    (
                        anchor_date is not None
                        and anchor_date <= partial_not_after + timedelta(days=90)
                    )
                    or (
                        anchor_date is None
                        and partial_not_after >= end_date
                    )
                )
            )
            window_session = requests.Session()
            window_session.cookies.clear()
            try:
                candidate, page_audit = fetch_ths_direct_bars(
                    window_session,
                    headers=headers,
                    code=target.code,
                    market=market_id,
                    time_period=str(config["time_period"]),
                    end_time_ms=end_time_ms,
                    adjust_type="forward",
                    endpoint=endpoint,
                    count=request_count,
                    allow_partial=page_allows_partial and not large_history_request,
                    allow_oversized=large_history_request or incremental,
                    minimum_rows=1 if period == "daily" else 2,
                    partial_confirmations=3,
                    max_attempts=1 if large_history_request else min(max_attempts, 3),
                    retry_delay=retry_delay,
                )
            finally:
                window_session.close()
            page_audits.append(
                {
                    "page": page_number,
                    "window_retry": window_retry,
                    "requested_end_time_ms": end_time_ms,
                    "endpoint": endpoint,
                    "allow_partial": page_allows_partial,
                    "allow_oversized": large_history_request or incremental,
                    "request_count": request_count,
                    **page_audit,
                }
            )
            if candidate is None:
                last_window_error = str(page_audit.get("error") or "window failed")
                continue
            candidate_oldest = int(candidate[0]["timestamp_ms"])
            candidate_oldest_date = date.fromisoformat(
                str(candidate[0]["key"])[:10]
            )
            if previous_oldest is not None and candidate_oldest >= previous_oldest:
                last_window_error = "历史分页没有向前推进"
                continue
            if (
                (
                    large_history_request
                    or len(candidate) < page_size
                )
                and partial_not_after is not None
                and candidate_oldest_date > partial_not_after
            ):
                last_window_error = (
                    "提前返回可疑的部分窗口: "
                    f"oldest={candidate_oldest_date}, "
                    f"expected_not_after={partial_not_after}"
                )
                continue
            if rows is None or candidate_oldest < oldest:
                rows = candidate
                oldest = candidate_oldest
                oldest_date = candidate_oldest_date
            if not large_history_request:
                break
        if rows is None:
            previous_oldest_date = (
                datetime.fromtimestamp(previous_oldest / 1000, tz=CN_TZ).date()
                if previous_oldest is not None
                else None
            )
            if (
                period == "daily"
                and by_timestamp
                and last_window_error == "历史分页没有向前推进"
                and expected_trade_dates is not None
            ):
                actual_dates = {
                    str(row["key"])[:10]
                    for row in by_timestamp.values()
                    if start_date.isoformat()
                    <= str(row["key"])[:10]
                    <= end_date.isoformat()
                }
                expected_dates = {
                    value
                    for value in expected_trade_dates
                    if start_date.isoformat() <= value <= end_date.isoformat()
                }
                if actual_dates == expected_dates:
                    source_floor_reached = True
                    break
            if (
                by_timestamp
                and previous_oldest_date is not None
                and source_floor_not_after is not None
                and previous_oldest_date <= source_floor_not_after
            ):
                source_floor_reached = True
                break
            raise RuntimeError(
                f"同花顺{period}第{page_number}页失败, "
                f"end_time_ms={end_time_ms}, error={last_window_error}"
            )
        for row in rows:
            by_timestamp[int(row["timestamp_ms"])] = row

        if oldest_date <= start_date:
            break
        if len(rows) < page_size:
            source_floor_reached = True
            break
        previous_oldest = oldest
        end_time_ms = oldest
    else:
        raise RuntimeError(f"同花顺{period}超过max-pages仍未到达开始日期")

    if by_timestamp and source_floor_not_after is not None:
        oldest_source_date = date.fromisoformat(
            str(min(by_timestamp.items())[1]["key"])[:10]
        )
        if start_date <= source_floor_not_after < oldest_source_date:
            raise RuntimeError(
                f"同花顺{period}历史没有到达已确认的原生下界: "
                f"oldest={oldest_source_date}, "
                f"expected_not_after={source_floor_not_after}"
            )

    selected = [
        row
        for _, row in sorted(by_timestamp.items())
        if start_date.isoformat() <= str(row["key"])[:10] <= end_date.isoformat()
    ]
    return selected, {
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "rows": len(selected),
        "coverage_start": str(selected[0]["key"]) if selected else None,
        "coverage_end": str(selected[-1]["key"]) if selected else None,
        "oldest_source_row": (
            str(min(by_timestamp.items())[1]["key"]) if by_timestamp else None
        ),
        "source_floor_reached": source_floor_reached,
        "pages": page_audits,
    }


def fetch_ths_all_daily_dates(
    *,
    target: StockTarget,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[str], dict[str, Any]]:
    url = THS_ALL_DAILY_URL.format(code=target.code)
    last_error = "unknown"
    required_candidates = min(3, max_attempts)
    successful_attempts = 0
    candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    attempts_used = 0
    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        request_session = requests.Session()
        try:
            response = request_session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": f"https://stockpage.10jqka.com.cn/{target.code}/",
                },
                timeout=30,
            )
            response.raise_for_status()
            match = re.search(r"\((\{.*\})\)\s*$", response.text)
            if match is None:
                raise ValueError("同花顺日线all.js不是可解析的JS包装JSON")
            payload = json.loads(match.group(1))
            start = str(payload["start"])
            raw_dates = [
                value for value in str(payload.get("dates") or "").split(",") if value
            ]
            if len(start) != 8 or not raw_dates:
                raise ValueError("同花顺日线all.js缺少起始日期或日期序列")
            dates: list[str] = []
            offset = 0
            sort_year = list(payload.get("sortYear") or [])
            if not sort_year:
                raise ValueError("同花顺all.js缺少sortYear")
            for index, year_item in enumerate(sort_year):
                year, declared_count = int(year_item[0]), int(year_item[1])
                count = (
                    len(raw_dates) - offset
                    if index == len(sort_year) - 1
                    else declared_count
                )
                for month_day in raw_dates[offset : offset + count]:
                    if len(month_day) != 4 or not month_day.isdigit():
                        raise ValueError(
                            f"同花顺all.js日期格式异常: {month_day!r}"
                        )
                    value = f"{year:04d}-{month_day[:2]}-{month_day[2:]}"
                    date.fromisoformat(value)
                    dates.append(value)
                offset += count
            if offset != len(raw_dates):
                raise ValueError("同花顺all.js年度计数与日期序列不一致")
            if dates != sorted(dates) or len(dates) != len(set(dates)):
                raise ValueError("同花顺all.js日期未严格递增或存在重复")
            signature = tuple(dates)
            candidate = candidates.setdefault(
                signature,
                {
                    "dates": dates,
                    "occurrences": 0,
                    "payload_start": start,
                    "payload_total": payload.get("total"),
                },
            )
            candidate["occurrences"] += 1
            successful_attempts += 1
            if successful_attempts >= required_candidates:
                break
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if retry_delay > 0:
                time.sleep(retry_delay)
        finally:
            request_session.close()

    if candidates:
        selected = max(
            candidates.values(),
            key=lambda item: (len(item["dates"]), int(item["occurrences"])),
        )
        return list(selected["dates"]), {
            "url": url,
            "line_type": "01",
            "adjust_type": "forward",
            "payload_start": selected["payload_start"],
            "payload_total": selected["payload_total"],
            "date_rows": len(selected["dates"]),
            "attempts": attempts_used,
            "successful_candidates": successful_attempts,
            "candidate_variants": [
                {
                    "date_rows": len(item["dates"]),
                    "coverage_start": item["dates"][0],
                    "coverage_end": item["dates"][-1],
                    "occurrences": item["occurrences"],
                }
                for item in candidates.values()
            ],
            "selection_rule": "maximum_date_rows_then_occurrences",
            }
    raise RuntimeError(f"同花顺前复权all.js日期请求失败: {last_error}")


def fetch_static_range(
    *,
    target: StockTarget,
    period: str,
    start_date: date,
    end_date: date,
    expected_trade_dates: set[str] | None,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    line_type = STATIC_LINE_TYPES[period]
    rows_by_key: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    source_started = False
    for year in range(start_date.year, end_date.year + 1):
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            file_session = requests.Session()
            file_session.cookies.clear()
            try:
                status, rows, error = fetch_ths(
                    file_session,
                    code=target.code,
                    line_type=line_type,
                    year=year,
                )
            finally:
                file_session.close()
            if error is None:
                file_rows = rows or []
                file_dates = {str(row["key"])[:10] for row in file_rows}
                expected_year_dates = {
                    value
                    for value in (expected_trade_dates or set())
                    if value.startswith(f"{year}-")
                }
                if file_dates:
                    if source_started:
                        missing_dates = expected_year_dates - file_dates
                    else:
                        first_file_date = min(file_dates)
                        missing_dates = {
                            value
                            for value in expected_year_dates
                            if value >= first_file_date and value not in file_dates
                        }
                    if missing_dates:
                        last_error = (
                            "同花顺60m年度文件缺失日线已确认的交易日: "
                            f"{sorted(missing_dates)[:5]}"
                        )
                        if retry_delay > 0:
                            time.sleep(retry_delay)
                        continue
                elif year >= 2024 and expected_year_dates:
                    last_error = (
                        "同花顺60m年度文件为空，"
                        "但日线存在交易日"
                    )
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue
                files.append(
                    {
                        "year": year,
                        "http_status": status,
                        "rows": len(rows or []),
                        "attempts": attempt,
                    }
                )
                if rows:
                    for row in rows:
                        rows_by_key[str(row["key"])] = row
                    source_started = True
                break
            last_error = error
            if retry_delay > 0:
                time.sleep(retry_delay)
        else:
            raise RuntimeError(
                f"同花顺{period}年度文件失败: "
                f"code={target.code}, year={year}, error={last_error}"
            )

    selected = [
        row
        for key, row in sorted(rows_by_key.items())
        if start_date.isoformat() <= key[:10] <= end_date.isoformat()
    ]
    return selected, {
        "line_type": line_type,
        "adjust_type": "forward",
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "rows": len(selected),
        "coverage_start": str(selected[0]["key"]) if selected else None,
        "coverage_end": str(selected[-1]["key"]) if selected else None,
        "files": files,
    }


def validate_rows(period: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = list(rows)
    keys = [str(row["key"]) for row in ordered]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError(f"{period}时间戳不严格递增或存在重复")

    expected_times = EXPECTED_TIMES_BY_PERIOD.get(period)

    by_date: dict[str, list[str]] = {}
    for row in ordered:
        values = [float(row[field]) for field in ("open", "high", "low", "close")]
        volume = float(row["volume"])
        amount = float(row["amount"])
        if not all(math.isfinite(value) for value in (*values, volume, amount)):
            raise ValueError(f"{period}存在非有限行情值: {row['key']}")
        open_, high, low, close = values
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError(f"{period} OHLC关系非法: {row['key']}")
        if volume < 0 or amount < 0:
            raise ValueError(f"{period}成交量或成交额为负: {row['key']}")
        if expected_times is not None:
            key = str(row["key"])
            by_date.setdefault(key[:10], []).append(key[11:19])

    bad_dates: list[str] = []
    if expected_times is not None:
        bad_dates = [
            trade_date
            for trade_date, times in by_date.items()
            if sorted(times) != list(expected_times)
        ]
        if bad_dates:
            raise ValueError(f"{period}每日K线结构异常: {bad_dates[:5]}")
    return {
        "rows": len(ordered),
        "trade_dates": len(by_date) if expected_times is not None else len(keys),
        "bad_trade_dates": len(bad_dates),
    }


def remove_no_trade_placeholders(
    rows: list[dict[str, Any]],
    *,
    daily_trade_dates: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for row in rows:
        if str(row["key"])[:10] in daily_trade_dates:
            kept.append(row)
        else:
            removed.append(row)
    invalid = [
        row
        for row in removed
        if float(row["volume"]) != 0
        or float(row["amount"]) != 0
        or len(
            {
                float(row[field])
                for field in ("open", "high", "low", "close")
            }
        )
        != 1
    ]
    if invalid:
        raise ValueError(
            "分钟线在同花顺日线不存在的日期有成交或非平价记录: "
            f"{[str(row['key']) for row in invalid[:5]]}"
        )
    return kept, {
        "removed_rows": len(removed),
        "removed_trade_dates": len(
            {str(row["key"])[:10] for row in removed}
        ),
        "rule": "daily_absent_and_zero_volume_zero_amount_flat_ohlc",
    }


def remove_partial_left_boundary(
    period: str,
    rows: list[dict[str, Any]],
    *,
    requested_start: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop only a proven left-truncated session at the requested boundary."""
    expected_times = EXPECTED_TIMES_BY_PERIOD.get(period)
    audit: dict[str, Any] = {
        "removed_rows": 0,
        "removed_trade_date": None,
        "rule": "requested_start_strict_session_suffix",
    }
    if not rows or expected_times is None:
        return rows, audit

    first_date = str(rows[0]["key"])[:10]
    if first_date != requested_start.isoformat():
        return rows, audit
    first_times = [
        str(row["key"])[11:19]
        for row in rows
        if str(row["key"])[:10] == first_date
    ]
    if first_times == list(expected_times):
        return rows, audit
    if (
        not first_times
        or len(first_times) != len(set(first_times))
        or first_times != list(expected_times[-len(first_times) :])
    ):
        return rows, audit

    kept = [row for row in rows if str(row["key"])[:10] != first_date]
    audit.update(
        {
            "removed_rows": len(rows) - len(kept),
            "removed_trade_date": first_date,
            "first_available_time": first_times[0],
            "last_available_time": first_times[-1],
        }
    )
    return kept, audit


def find_missing_native_trade_dates(
    rows: Iterable[Mapping[str, Any]],
    *,
    daily_reference_dates: Iterable[str],
) -> list[str]:
    actual_dates = sorted({str(row["key"])[:10] for row in rows})
    if not actual_dates:
        return []
    expected_dates = {
        value
        for value in daily_reference_dates
        if actual_dates[0] <= value <= actual_dates[-1]
    }
    return sorted(expected_dates - set(actual_dates))


def recover_missing_native_dates(
    *,
    headers: dict[str, str],
    target: StockTarget,
    market_id: str,
    period: str,
    rows: list[dict[str, Any]],
    missing_dates: list[str],
    window_retries: int,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_times = EXPECTED_TIMES_BY_PERIOD[period]
    if not missing_dates:
        return rows, {"requested_dates": [], "recovered_rows": 0, "requests": []}
    if len(missing_dates) > 20:
        raise ValueError(f"同花顺{period}完整交易日缺口过多: {missing_dates[:20]}")

    recovered: list[dict[str, Any]] = []
    request_audits: list[dict[str, Any]] = []
    endpoint_attempts = max(2, min(window_retries, 12))
    for trade_date in missing_dates:
        end_time_ms = int(
            datetime.fromisoformat(f"{trade_date}T15:00:00+08:00").timestamp()
            * 1000
        )
        target_rows = None
        attempts: list[dict[str, Any]] = []
        for attempt in range(1, endpoint_attempts + 1):
            endpoint = (
                THS_DIRECT_KLINE_CACHE_URL
                if attempt % 2 == 1
                else THS_DIRECT_KLINE_URL
            )
            recovery_session = requests.Session()
            recovery_session.cookies.clear()
            try:
                window, audit = fetch_ths_direct_bars(
                    recovery_session,
                    headers=headers,
                    code=target.code,
                    market=market_id,
                    time_period=str(PERIOD_CONFIG[period]["time_period"]),
                    end_time_ms=end_time_ms,
                    adjust_type="forward",
                    endpoint=endpoint,
                    count=len(expected_times),
                    minimum_rows=len(expected_times),
                    max_attempts=min(max_attempts, 3),
                    retry_delay=retry_delay,
                )
            finally:
                recovery_session.close()
            attempts.append({"attempt": attempt, "endpoint": endpoint, **audit})
            if window is None:
                continue
            candidate = [
                row for row in window if str(row["key"])[:10] == trade_date
            ]
            if [str(row["key"])[11:19] for row in candidate] == list(
                expected_times
            ):
                target_rows = candidate
                break
        request_audits.append(
            {"trade_date": trade_date, "attempts": attempts, "recovered": bool(target_rows)}
        )
        if target_rows is None:
            raise RuntimeError(
                f"同花顺{period}原生缺失交易日定点恢复失败: {trade_date}"
            )
        recovered.extend(target_rows)

    merged = {
        int(row["timestamp_ms"]): row
        for row in (*rows, *recovered)
    }
    return [row for _, row in sorted(merged.items())], {
        "requested_dates": missing_dates,
        "recovered_rows": len(recovered),
        "requests": request_audits,
    }


def validate_native_candidate(
    period: str,
    rows: list[dict[str, Any]],
    *,
    daily_reference_dates: list[str],
    daily_start_date: date,
    end_date: date,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    if not rows:
        raise ValueError(f"同花顺{period}在请求范围内没有数据")

    placeholder_audit = None
    if period == "daily":
        expected_dates = [
            value
            for value in daily_reference_dates
            if daily_start_date.isoformat() <= value <= end_date.isoformat()
        ]
        actual_dates = [str(row["key"])[:10] for row in rows]
        if actual_dates != expected_dates:
            raise ValueError(
                "同花顺直连日线与all.js日期覆盖不一致: "
                f"expected={len(expected_dates)}, actual={len(actual_dates)}"
            )
        calendar_audit = {
            "coverage_start": actual_dates[0] if actual_dates else None,
            "coverage_end": actual_dates[-1] if actual_dates else None,
            "trade_dates": len(actual_dates),
            "missing_dates": 0,
            "extra_dates": 0,
        }
    else:
        rows, placeholder_audit = remove_no_trade_placeholders(
            rows,
            daily_trade_dates={
                value
                for value in daily_reference_dates
                if value <= end_date.isoformat()
            },
        )
        actual_dates = sorted({str(row["key"])[:10] for row in rows})
        if not actual_dates:
            raise ValueError(f"同花顺{period}清理无交易占位后没有数据")
        expected_dates = [
            value
            for value in daily_reference_dates
            if actual_dates[0] <= value <= actual_dates[-1]
        ]
        if actual_dates != expected_dates:
            missing_dates = sorted(set(expected_dates) - set(actual_dates))
            extra_dates = sorted(set(actual_dates) - set(expected_dates))
            raise ValueError(
                f"同花顺{period}与官方日线交易日期覆盖不一致: "
                f"missing={missing_dates[:5]}, extra={extra_dates[:5]}"
            )
        calendar_audit = {
            "coverage_start": actual_dates[0],
            "coverage_end": actual_dates[-1],
            "trade_dates": len(actual_dates),
            "missing_dates": 0,
            "extra_dates": 0,
        }

    return rows, {
        "calendar": calendar_audit,
        "structure": validate_rows(period, rows),
    }, placeholder_audit


def fetch_target(
    target: StockTarget,
    *,
    base_headers: dict[str, str],
    periods: tuple[str, ...],
    daily_start_date: date,
    minute_start_date: date,
    end_date: date,
    page_size: int,
    max_pages: int,
    window_retries: int,
    max_attempts: int,
    retry_delay: float,
    incremental: bool,
) -> FetchResult:
    session = requests.Session()
    session.cookies.clear()
    headers = {
        **base_headers,
        "Referer": f"https://stockpage.10jqka.com.cn/{target.code}/",
    }
    try:
        exchange_default_market_id = default_market_id(target)
        market_id, market_audit = discover_ths_market_id(
            session, target=target, headers=headers
        )
        market_audit["exchange_default_market_id"] = exchange_default_market_id
        market_audit["matches_exchange_default"] = (
            exchange_default_market_id == market_id
        )

        daily_reference_dates, daily_reference_audit = fetch_ths_all_daily_dates(
            target=target,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )
        listing_date = date.fromisoformat(daily_reference_dates[0])
        rows_by_period: dict[str, list[dict[str, Any]]] = {}
        fetch_audit: dict[str, Any] = {}
        structure_audit: dict[str, Any] = {}
        placeholder_audit: dict[str, Any] = {}
        period_order = tuple(PERIOD_CONFIG)
        ordered_periods = tuple(sorted(periods, key=period_order.index))
        for period in ordered_periods:
            start_date = daily_start_date if period == "daily" else minute_start_date
            if period == "daily":
                partial_not_after = max(daily_start_date, listing_date) + timedelta(
                    days=15
                )
            else:
                public_floor_latest = PUBLIC_MINUTE_FLOOR_LATEST[period]
                native_source_floor_latest = NATIVE_SOURCE_FLOOR_LATEST[period]
                if target.market == "BJ":
                    public_floor_latest = max(
                        public_floor_latest,
                        PUBLIC_BJ_MINUTE_FLOOR_LATEST,
                    )
                    native_source_floor_latest = max(
                        native_source_floor_latest,
                        PUBLIC_BJ_MINUTE_FLOOR_LATEST,
                    )
                partial_cutoff = max(
                    public_floor_latest,
                    minute_start_date + timedelta(days=45),
                    listing_date + timedelta(days=45),
                )
                source_floor_cutoff = max(
                    native_source_floor_latest,
                    minute_start_date + timedelta(days=45),
                    listing_date + timedelta(days=45),
                )
                partial_not_after = reference_floor_not_after(
                    daily_reference_dates, partial_cutoff
                )
                source_floor_not_after = reference_floor_not_after(
                    daily_reference_dates, source_floor_cutoff
                )

            rejected_candidates: list[str] = []
            candidate_attempts = min(window_retries, 12)
            for candidate_attempt in range(1, candidate_attempts + 1):
                try:
                    rows, audit = fetch_range(
                        headers=headers,
                        target=target,
                        market_id=market_id,
                        period=period,
                        start_date=start_date,
                        end_date=end_date,
                        page_size=page_size,
                        max_pages=max_pages,
                        window_retries=window_retries,
                        max_attempts=max_attempts,
                        retry_delay=retry_delay,
                        allow_partial=True,
                        partial_not_after=partial_not_after,
                        source_floor_not_after=(
                            None if period == "daily" else source_floor_not_after
                        ),
                        expected_trade_dates=(
                            set(daily_reference_dates)
                            if period == "daily"
                            else None
                        ),
                        incremental=incremental,
                    )
                    gap_audit = None
                    boundary_audit = None
                    if period != "daily":
                        missing_dates = find_missing_native_trade_dates(
                            rows,
                            daily_reference_dates=daily_reference_dates,
                        )
                        rows, gap_audit = recover_missing_native_dates(
                            headers=headers,
                            target=target,
                            market_id=market_id,
                            period=period,
                            rows=rows,
                            missing_dates=missing_dates,
                            window_retries=window_retries,
                            max_attempts=max_attempts,
                            retry_delay=retry_delay,
                        )
                        rows, boundary_audit = remove_partial_left_boundary(
                            period,
                            rows,
                            requested_start=start_date,
                        )
                    rows, candidate_audit, removed_audit = validate_native_candidate(
                        period,
                        rows,
                        daily_reference_dates=daily_reference_dates,
                        daily_start_date=daily_start_date,
                        end_date=end_date,
                    )
                except (RuntimeError, ValueError) as exc:
                    rejected_candidates.append(f"{type(exc).__name__}: {exc}")
                    continue
                audit["native_candidate_selection"] = {
                    "attempts": candidate_attempt,
                    "rejected_candidates": rejected_candidates,
                }
                if period == "daily":
                    audit["date_reference"] = daily_reference_audit
                else:
                    audit["daily_calendar_check"] = candidate_audit["calendar"]
                    audit["native_gap_recovery"] = gap_audit
                    audit["source_boundary_cleanup"] = boundary_audit
                    placeholder_audit[period] = removed_audit
                rows_by_period[period] = rows
                fetch_audit[period] = audit
                structure_audit[period] = candidate_audit["structure"]
                break
            else:
                raise ValueError(
                    f"同花顺{period}原生候选在重试后仍未通过独立验收: "
                    f"{rejected_candidates[-3:]}"
                )

        return FetchResult(
            target=target,
            status="validated",
            rows=rows_by_period,
            audit={
                "market": market_audit,
                "daily_date_reference": daily_reference_audit,
                "fetch": fetch_audit,
                "structure": structure_audit,
                "minute_no_trade_placeholders": placeholder_audit,
                "cross_period_generation": False,
            },
        )
    except Exception as exc:
        return FetchResult(
            target=target,
            status="failed",
            rows={},
            audit={},
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        session.close()


def create_indexes(database: Any, collections: Mapping[str, str]) -> None:
    for period, name in collections.items():
        if name not in ALLOWED_DESTINATIONS:
            raise RuntimeError(f"禁止创建非量化目标集合: {name}")
        key_field = "trade_date" if period == "daily" else "timestamp"
        collection = database[name]
        collection.create_index(
            [("code", ASCENDING), (key_field, ASCENDING)],
            unique=True,
            name=f"uniq_{name}_{key_field}",
        )
        collection.create_index(
            [("trade_date", ASCENDING), ("code", ASCENDING), (key_field, ASCENDING)],
            name=f"idx_{name}_trade_date_code_{key_field}",
        )


def keep_incomplete_targets(
    database: Any,
    collections: Mapping[str, str],
    periods: tuple[str, ...],
    targets: list[StockTarget],
) -> list[StockTarget]:
    completed_by_period = [
        set(database[collections[period]].distinct("code"))
        for period in periods
    ]
    completed = set.intersection(*completed_by_period) if completed_by_period else set()
    return [target for target in targets if target.code not in completed]


def make_document(
    target: StockTarget,
    *,
    period: str,
    row: Mapping[str, Any],
    market_id: str,
    fetched_at: datetime,
) -> dict[str, Any]:
    key = str(row["key"])
    document: dict[str, Any] = {
        "code": target.code,
        "name": target.name,
        "market": target.market,
        "trade_date": key[:10],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "amount": float(row["amount"]),
        "volume_unit": "share",
        "adjust": "qfq",
        "adjust_type": "forward",
        "interval": str(PERIOD_CONFIG[period]["interval"]),
        "source": THS_SOURCES[period],
        "source_market_id": market_id,
        "validation_status": "ths_forward_native_structure_validated",
        "fetched_at": fetched_at,
        "updated_at": fetched_at,
    }
    if period == "daily":
        document["trade_date_int"] = int(key[:10].replace("-", ""))
    else:
        document["timestamp"] = key
    return document


def write_result(
    database: Any,
    collections: Mapping[str, str],
    result: FetchResult,
) -> dict[str, int]:
    market_id = str(result.audit["market"]["market_id"])
    now = datetime.now(CN_TZ)
    written: dict[str, int] = {}
    for period, rows in result.rows.items():
        collection_name = collections[period]
        if collection_name not in ALLOWED_DESTINATIONS:
            raise RuntimeError(f"禁止写入非量化目标集合: {collection_name}")
        key_field = "trade_date" if period == "daily" else "timestamp"
        documents: list[dict[str, Any]] = []
        for row in rows:
            documents.append(
                make_document(
                    result.target,
                    period=period,
                    row=row,
                    market_id=market_id,
                    fetched_at=now,
                )
            )
        if not documents:
            written[period] = 0
            continue
        collection = database[collection_name]
        code_exists = collection.count_documents(
            {"code": result.target.code}, limit=1
        )
        if not code_exists:
            for document in documents:
                document["created_at"] = now
            collection.insert_many(documents, ordered=False)
            written[period] = len(documents)
            continue
        operations = []
        for document in documents:
            operations.append(
                UpdateOne(
                    {"code": result.target.code, key_field: document[key_field]},
                    {
                        "$set": document,
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            )
        collection.bulk_write(operations, ordered=False)
        written[period] = len(operations)
    return written


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    periods = parse_periods(args.periods)
    if args.daily_start_date > args.end_date:
        parser.error("daily-start-date不能晚于end-date")
    if args.minute_start_date > args.end_date and any(
        period != "daily" for period in periods
    ):
        parser.error("minute-start-date不能晚于end-date")
    now_cn = datetime.now(CN_TZ)
    if args.end_date > now_cn.date():
        parser.error("end-date不能晚于北京时间今天")
    if args.end_date == now_cn.date() and now_cn.hour < 16 and not args.allow_intraday:
        parser.error("北京时间16:00前禁止抓取当天未收盘数据")
    if args.destination == "production" and not args.confirm_production_write:
        parser.error("写正式集合必须同时提供--confirm-production-write")

    settings = Settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10_000)
    client.admin.command("ping")
    database = client[settings.mongo_db_name]
    collections = (
        STAGE_COLLECTIONS if args.destination == "stage" else PRODUCTION_COLLECTIONS
    )
    snapshot_date = resolve_snapshot_date(
        database,
        collection_name=QUANT_UNIVERSE_COLLECTION,
        explicit=args.snapshot_date,
    )
    targets = load_targets(
        database,
        collection_name=QUANT_UNIVERSE_COLLECTION,
        snapshot_date=snapshot_date,
        only_code=args.only_code,
        market_filter=args.market,
        offset=args.offset,
        limit=args.limit,
    )
    selected_target_count = len(targets)
    skip_codes = (
        load_skip_codes(args.skip_codes_file) if args.skip_codes_file is not None else frozenset()
    )
    if skip_codes:
        targets = exclude_skipped_targets(targets, skip_codes)
        print(
            f"ths_forward_skip_filter before={selected_target_count} "
            f"after={len(targets)} skipped={selected_target_count - len(targets)} "
            f"file={args.skip_codes_file}",
            flush=True,
        )
    if not targets:
        client.close()
        raise RuntimeError("完整qfq快照中没有符合条件的股票")
    if args.missing_only:
        before_missing_filter = len(targets)
        targets = keep_incomplete_targets(database, collections, periods, targets)
        print(
            f"ths_forward_missing_filter before={before_missing_filter} "
            f"after={len(targets)}",
            flush=True,
        )
        if not targets:
            client.close()
            print("ths_forward_finished targets=0 all_selected_targets_complete=1")
            return

    headers, credential_audit = discover_runtime_headers(
        code=targets[0].code,
        max_attempts=min(args.max_attempts, 12),
        retry_delay=max(args.retry_delay, 0.5),
    )
    if args.apply:
        create_indexes(database, {period: collections[period] for period in periods})

    report: dict[str, Any] = {
        "standard": {
            "provider": "tonghuashun",
            "adjust": "qfq",
            "adjust_type": "forward",
            "browser_runtime": False,
            "cookies_required": False,
        },
        "apply": bool(args.apply),
        "destination": args.destination,
        "collections": {period: collections[period] for period in periods},
        "universe_collection": QUANT_UNIVERSE_COLLECTION,
        "snapshot_date": snapshot_date,
        "selected_target_count": selected_target_count,
        "target_count": len(targets),
        "skip_codes_file": (
            str(args.skip_codes_file) if args.skip_codes_file is not None else None
        ),
        "skipped_target_count": selected_target_count - len(targets),
        "market_filter": args.market,
        "missing_only": bool(args.missing_only),
        "incremental": bool(args.incremental),
        "periods": list(periods),
        "daily_start_date": args.daily_start_date.isoformat(),
        "minute_start_date": args.minute_start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "credential_discovery": credential_audit,
        "status_counts": {},
        "row_counts": Counter(),
        "writes": Counter(),
        "results": [],
    }
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            def submit_target(target: StockTarget) -> Future[FetchResult]:
                return executor.submit(
                    fetch_target,
                    target,
                    base_headers=headers,
                    periods=periods,
                    daily_start_date=args.daily_start_date,
                    minute_start_date=args.minute_start_date,
                    end_date=args.end_date,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    window_retries=args.window_retries,
                    max_attempts=args.max_attempts,
                    retry_delay=args.retry_delay,
                    incremental=args.incremental,
                )

            results = iter_bounded_results(
                executor,
                targets,
                submit_target,
                max_in_flight=args.workers,
            )
            for index, result in enumerate(results, start=1):
                report["status_counts"][result.status] = (
                    int(report["status_counts"].get(result.status, 0)) + 1
                )
                item: dict[str, Any] = {
                    "code": result.target.code,
                    "market": result.target.market,
                    "status": result.status,
                    "audit": result.audit,
                }
                if result.error:
                    item["error"] = result.error
                if result.status == "validated":
                    for period, rows in result.rows.items():
                        report["row_counts"][period] += len(rows)
                    if args.apply:
                        writes = write_result(database, collections, result)
                        item["writes"] = writes
                        for period, count in writes.items():
                            report["writes"][period] += count
                report["results"].append(item)
                print(
                    f"ths_forward_progress={index}/{len(targets)} "
                    f"code={result.target.code} status={result.status} "
                    f"rows={{{', '.join(f'{key}:{len(value)}' for key, value in result.rows.items())}}} "
                    f"error={result.error or '-'}",
                    flush=True,
                )
                del result
    finally:
        client.close()

    report["row_counts"] = dict(report["row_counts"])
    report["writes"] = dict(report["writes"])
    report["elapsed_seconds"] = time.monotonic() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status_counts": report["status_counts"],
                "row_counts": report["row_counts"],
                "writes": report["writes"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if report["status_counts"].get("failed"):
        raise RuntimeError("同花顺前复权同步存在失败股票，请按报告重试")


if __name__ == "__main__":
    main()
