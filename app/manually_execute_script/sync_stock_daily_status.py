# 在项目根目录执行：
# python app/manually_execute_script/sync_stock_daily_status.py --market ALL
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from curl_cffi import requests as curl_requests
from pymongo import UpdateMany


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.import_stock_daily_status_csv import (  # noqa: E402
    COLLECTION_NAME,
    create_indexes,
)
from app.manually_execute_script.stock_history_common import (  # noqa: E402
    BaoStockProxySession,
    StockTarget,
    ensure_baostock_login,
    market_for_code,
    normalize_code,
    open_database,
    optional_float,
    parse_date,
    positive_int,
    upsert_documents,
)
from app.manually_execute_script.sync_a_stock_daily_bars import (  # noqa: E402
    COLLECTION_NAME as DAILY_COLLECTION_NAME,
)


BAOSTOCK_SOURCE = "baostock.query_history_k_data_plus"
BSE_DERIVED_SOURCE = "bse.daily_bars+eastmoney.pct_chg"
BSE_ST_SOURCE = "cninfo.verified_bse_risk_warning_periods"
ST_GAP_FILL_SOURCE = "derived.nearest_known_st_state"
ST_NAME_FALLBACK_SOURCE = "local.security_name_st_prefix"
BAOSTOCK_FIELDS = "date,code,preclose,tradestatus,isST"
PRICE_TICK = Decimal("0.01")
SH_SZ_ST_LIMIT_CHANGE_DATE = date(2026, 7, 6)


@dataclass(frozen=True)
class BseStPeriod:
    start_date: date
    end_date: date | None
    source_url: str


# 巨潮资讯为法定信息披露平台。日期取公告正文中的“实施退市风险警示起始日”；
# 广道数字进入退市整理期后简称变为“广道退”，不再记作ST。
BSE_ST_PERIODS: dict[str, tuple[BseStPeriod, ...]] = {
    "920305": (
        BseStPeriod(
            start_date=date(2025, 5, 6),
            end_date=None,
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2025-04-29/"
                "1223423954.PDF"
            ),
        ),
    ),
    "920680": (
        BseStPeriod(
            start_date=date(2025, 5, 6),
            end_date=date(2025, 12, 11),
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2025-04-29/"
                "1223429057.PDF"
            ),
        ),
    ),
    "920090": (
        BseStPeriod(
            start_date=date(2026, 4, 24),
            end_date=None,
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2026-04-22/"
                "1225156941.PDF"
            ),
        ),
    ),
    "920023": (
        BseStPeriod(
            start_date=date(2026, 4, 30),
            end_date=None,
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2026-04-28/"
                "1225246489.PDF"
            ),
        ),
    ),
    "920575": (
        BseStPeriod(
            start_date=date(2026, 5, 6),
            end_date=None,
            source_url=(
                "https://static.cninfo.com.cn/finalpage/2026-04-29/"
                "1225267197.PDF"
            ),
        ),
    ),
}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="补齐沪深京逐日停牌、ST和涨跌停价状态。"
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=date(2015, 1, 1),
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=date(2026, 12, 31),
    )
    parser.add_argument(
        "--market",
        choices=("ALL", "HS", "SH", "SZ", "BJ"),
        default="ALL",
    )
    parser.add_argument("--only-code", default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--batch-size", type=positive_int, default=1000)
    parser.add_argument("--progress", type=positive_int, default=100)
    parser.add_argument("--max-retries", type=positive_int, default=4)
    parser.add_argument("--shard-count", type=positive_int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        "--baostock-proxy",
        dest="baostock_proxy",
        action="store_true",
        default=True,
    )
    proxy_group.add_argument(
        "--no-baostock-proxy",
        dest="baostock_proxy",
        action="store_false",
    )
    parser.add_argument("--proxy-socket-timeout", type=positive_int, default=90)
    parser.add_argument("--proxy-max-queries", type=positive_int, default=40)
    parser.add_argument("--proxy-lifetime-seconds", type=positive_int, default=150)
    parser.add_argument("--proxy-login-attempts", type=positive_int, default=20)
    parser.add_argument("--proxy-retry-delay", type=positive_int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--fill-st-gaps-only",
        action="store_true",
        help="仅补齐接口未返回日期的空ST状态，不重新请求行情接口。",
    )
    return parser


def infer_st_from_name(name: str | None) -> bool:
    normalized = "".join(str(name or "").upper().split())
    return normalized.startswith(("*ST", "ST", "S*ST", "SST"))


def resolve_missing_st_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    previous_state: tuple[bool, str] | None,
) -> list[dict[str, Any]]:
    following_states: list[tuple[bool, str] | None] = [None] * len(rows)
    following: tuple[bool, str] | None = None
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("is_st") is not None:
            following = (bool(row["is_st"]), str(row["trade_date"]))
        following_states[index] = following

    resolved: list[dict[str, Any]] = []
    current = previous_state
    for index, row in enumerate(rows):
        trade_date = str(row["trade_date"])
        if row.get("is_st") is not None:
            current = (bool(row["is_st"]), trade_date)
            continue
        anchor = current or following_states[index]
        if anchor is None:
            resolved.append(
                {
                    "trade_date": trade_date,
                    "is_st": infer_st_from_name(row.get("name")),
                    "source": ST_NAME_FALLBACK_SOURCE,
                    "anchor_date": None,
                }
            )
            continue
        resolved.append(
            {
                "trade_date": trade_date,
                "is_st": anchor[0],
                "source": ST_GAP_FILL_SOURCE,
                "anchor_date": anchor[1],
            }
        )
    return resolved


def fill_missing_st_states(
    collection: Any,
    *,
    start_date: date,
    end_date: date,
    market: str,
    only_code: str | None,
    dry_run: bool,
) -> tuple[int, int]:
    markets = {
        "ALL": ["SH", "SZ", "BJ"],
        "HS": ["SH", "SZ"],
        "SH": ["SH"],
        "SZ": ["SZ"],
        "BJ": ["BJ"],
    }[market]
    date_filter = {
        "$gte": start_date.isoformat(),
        "$lte": end_date.isoformat(),
    }
    target_filter = {
        "market": {"$in": markets},
        "trade_date": date_filter,
        "is_st": None,
    }
    if only_code:
        target_filter["code"] = normalize_code(only_code)
    codes = sorted(str(code) for code in collection.distinct("code", target_filter))
    operations: list[UpdateMany] = []
    planned = 0
    now = datetime.now()
    for code in codes:
        rows = list(
            collection.find(
                {"code": code, "trade_date": date_filter},
                {"_id": 0, "trade_date": 1, "is_st": 1, "name": 1},
            ).sort("trade_date", 1)
        )
        previous = collection.find_one(
            {
                "code": code,
                "trade_date": {"$lt": start_date.isoformat()},
                "is_st": {"$ne": None},
            },
            {"_id": 0, "trade_date": 1, "is_st": 1},
            sort=[("trade_date", -1)],
        )
        previous_state = (
            (bool(previous["is_st"]), str(previous["trade_date"]))
            if previous is not None
            else None
        )
        resolutions = resolve_missing_st_rows(
            rows,
            previous_state=previous_state,
        )
        grouped_dates: dict[tuple[bool, str, str | None], list[str]] = defaultdict(list)
        for resolution in resolutions:
            key = (
                bool(resolution["is_st"]),
                str(resolution["source"]),
                resolution["anchor_date"],
            )
            grouped_dates[key].append(str(resolution["trade_date"]))
        for (is_st, source, anchor_date), trade_dates in grouped_dates.items():
            planned += len(trade_dates)
            operations.append(
                UpdateMany(
                    {
                        "code": code,
                        "trade_date": {"$in": trade_dates},
                        "is_st": None,
                    },
                    {
                        "$set": {
                            "is_st": is_st,
                            "st_source": source,
                            "st_anchor_date": anchor_date,
                            "updated_at": now,
                        }
                    },
                )
            )
    if dry_run or not operations:
        return planned, 0
    result = collection.bulk_write(operations, ordered=False)
    return planned, int(result.modified_count)


def sh_sz_limit_rate(code: str, trade_date: date, is_st: bool) -> Decimal:
    normalized = normalize_code(code)
    if normalized.startswith(("300", "301", "688", "689")):
        return Decimal("0.20")
    if is_st and trade_date < SH_SZ_ST_LIMIT_CHANGE_DATE:
        return Decimal("0.05")
    return Decimal("0.10")


def sh_sz_price_limits(
    preclose: Decimal,
    *,
    code: str,
    trade_date: date,
    is_st: bool,
) -> tuple[float, float]:
    rate = sh_sz_limit_rate(code, trade_date, is_st)
    limit_up = (preclose * (Decimal("1") + rate)).quantize(
        PRICE_TICK,
        rounding=ROUND_HALF_UP,
    )
    limit_down = (preclose * (Decimal("1") - rate)).quantize(
        PRICE_TICK,
        rounding=ROUND_HALF_UP,
    )
    return float(limit_up), float(limit_down)


def bse_price_limits(preclose: Decimal) -> tuple[float, float]:
    limit_amount = (preclose * Decimal("0.30")).quantize(
        PRICE_TICK,
        rounding=ROUND_DOWN,
    )
    return float(preclose + limit_amount), float(preclose - limit_amount)


def reference_price_from_pct(close: float, pct_chg: float) -> Decimal:
    denominator = Decimal("1") + Decimal(str(pct_chg)) / Decimal("100")
    if denominator <= 0:
        raise ValueError(f"涨跌幅无法还原前收盘价: {pct_chg}")
    return (Decimal(str(close)) / denominator).quantize(
        PRICE_TICK,
        rounding=ROUND_HALF_UP,
    )


def bse_st_state(code: str, trade_date: date) -> tuple[bool, str | None]:
    for period in BSE_ST_PERIODS.get(normalize_code(code), ()):
        if trade_date < period.start_date:
            continue
        if period.end_date is None or trade_date < period.end_date:
            return True, period.source_url
    return False, None


def _has_initial_no_limit(
    *,
    first_trade_date: date | None,
    listed_trade_number: int,
) -> bool:
    return first_trade_date is not None and listed_trade_number <= 5


def baostock_status_document(
    target: StockTarget,
    row: Mapping[str, str],
    *,
    first_trade_date: date | None,
    listed_trade_number: int,
) -> dict[str, Any]:
    trade_date = date.fromisoformat(str(row["date"]))
    suspended = str(row.get("tradestatus", "")).strip() != "1"
    is_st = str(row.get("isST", "")).strip() == "1"
    preclose_value = optional_float(row.get("preclose"))
    document: dict[str, Any] = {
        "code": target.code,
        "name": target.name,
        "market": target.market,
        "trade_date": trade_date.isoformat(),
        "trade_date_int": int(trade_date.strftime("%Y%m%d")),
        "is_suspended": suspended,
        "is_st": is_st,
        "preclose": preclose_value,
        "suspend_source": BAOSTOCK_SOURCE,
        "st_source": BAOSTOCK_SOURCE,
        "source": BAOSTOCK_SOURCE,
    }
    if trade_date.year < 2026:
        return document

    no_limit = _has_initial_no_limit(
        first_trade_date=first_trade_date,
        listed_trade_number=listed_trade_number,
    )
    if suspended or no_limit or preclose_value is None or preclose_value <= 0:
        document.update(
            {
                "limit_up": None,
                "limit_down": None,
                "has_price_limit": False,
                "price_limit_source": BAOSTOCK_SOURCE,
            }
        )
        return document

    limit_up, limit_down = sh_sz_price_limits(
        Decimal(str(preclose_value)),
        code=target.code,
        trade_date=trade_date,
        is_st=is_st,
    )
    document.update(
        {
            "limit_up": limit_up,
            "limit_down": limit_down,
            "has_price_limit": True,
            "price_limit_source": BAOSTOCK_SOURCE,
        }
    )
    return document


def iter_baostock_status_documents(
    baostock: Any,
    target: StockTarget,
    *,
    start_date: date,
    end_date: date,
    first_trade_date: date | None,
) -> Iterable[dict[str, Any]]:
    prefix = "sh" if target.market == "SH" else "sz"
    result = baostock.query_history_k_data_plus(
        f"{prefix}.{target.code}",
        BAOSTOCK_FIELDS,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        frequency="d",
        adjustflag="3",
    )
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock状态请求失败: {result.error_msg}")

    fields = BAOSTOCK_FIELDS.split(",")
    listed_trade_number = 0 if first_trade_date and first_trade_date >= start_date else 999
    while result.next():
        row = dict(zip(fields, result.get_row_data()))
        row_date = date.fromisoformat(row["date"])
        if (
            first_trade_date is not None
            and row_date >= first_trade_date
            and row.get("tradestatus") == "1"
            and listed_trade_number < 999
        ):
            listed_trade_number += 1
        yield baostock_status_document(
            target,
            row,
            first_trade_date=first_trade_date,
            listed_trade_number=listed_trade_number,
        )

    import baostock.common.context as baostock_context

    active_socket = getattr(baostock_context, "default_socket", None)
    socket_failure = getattr(active_socket, "failure", None)
    if socket_failure is not None:
        raise RuntimeError(f"BaoStock状态分页连接失败: {socket_failure}")
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock状态游标失败: {result.error_msg}")


def load_hs_targets(
    status_collection: Any,
    daily_collection: Any,
    *,
    start_date: date,
    end_date: date,
    market: str,
    only_code: str | None,
    offset: int,
    limit: int | None,
    shard_count: int,
    shard_index: int,
) -> list[StockTarget]:
    filters: dict[str, Any] = {
        "trade_date": {
            "$gte": start_date.isoformat(),
            "$lte": end_date.isoformat(),
        },
        "market": {"$in": ["SH", "SZ"]},
    }
    if market in {"SH", "SZ"}:
        filters["market"] = market
    if only_code:
        filters["code"] = normalize_code(only_code)
    codes = set(status_collection.distinct("code", filters))
    codes.update(daily_collection.distinct("code", filters))
    ordered = sorted(str(code) for code in codes)
    stop = None if limit is None else offset + limit
    ordered = ordered[offset:stop]
    ordered = [
        code for index, code in enumerate(ordered) if index % shard_count == shard_index
    ]

    targets: list[StockTarget] = []
    for code in ordered:
        row = daily_collection.find_one(
            {"code": code, "name": {"$nin": [None, ""]}},
            {"_id": 0, "name": 1},
            sort=[("trade_date", -1)],
        )
        targets.append(
            StockTarget(
                code=code,
                name=str(row["name"]).strip() if row and row.get("name") else None,
                market=market_for_code(code),
            )
        )
    return targets


def first_trade_date(daily_collection: Any, code: str) -> date | None:
    row = daily_collection.find_one(
        {"code": code},
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", 1)],
    )
    return date.fromisoformat(str(row["trade_date"])) if row else None


def sync_hs_status(
    database: Any,
    args: argparse.Namespace,
) -> None:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("缺少baostock，请安装requirements.txt") from exc

    status_collection = database[COLLECTION_NAME]
    daily_collection = database[DAILY_COLLECTION_NAME]
    targets = load_hs_targets(
        status_collection,
        daily_collection,
        start_date=args.start_date,
        end_date=args.end_date,
        market=args.market,
        only_code=args.only_code,
        offset=args.offset,
        limit=args.limit,
        shard_count=args.shard_count,
        shard_index=args.shard_index,
    )
    if not targets:
        print("status_hs_finished targets=0 rows=0 affected=0 failed=0")
        return

    proxy_session = (
        BaoStockProxySession(
            bs,
            socket_timeout=args.proxy_socket_timeout,
            max_queries_per_proxy=args.proxy_max_queries,
            max_lifetime_seconds=args.proxy_lifetime_seconds,
            login_attempts=args.proxy_login_attempts,
            retry_delay_seconds=args.proxy_retry_delay,
        )
        if args.baostock_proxy
        else None
    )
    if proxy_session is None:
        ensure_baostock_login(bs)

    total_rows = 0
    total_affected = 0
    failures: list[str] = []
    try:
        for index, target in enumerate(targets, start=1):
            started = time.monotonic()
            for attempt in range(1, args.max_retries + 1):
                try:
                    if proxy_session is not None:
                        proxy_session.ensure_login()
                    documents = list(
                        iter_baostock_status_documents(
                            bs,
                            target,
                            start_date=args.start_date,
                            end_date=args.end_date,
                            first_trade_date=first_trade_date(
                                daily_collection,
                                target.code,
                            ),
                        )
                    )
                    if args.dry_run:
                        rows = len(documents)
                        affected = 0
                    else:
                        stats = upsert_documents(
                            status_collection,
                            documents,
                            key_fields=("code", "trade_date"),
                            batch_size=args.batch_size,
                        )
                        rows = stats.rows
                        affected = stats.affected
                    if proxy_session is not None:
                        proxy_session.note_query()
                    total_rows += rows
                    total_affected += affected
                    if index % args.progress == 0 or index == len(targets):
                        print(
                            f"status_hs_progress={index}/{len(targets)} "
                            f"code={target.code} total_rows={total_rows} "
                            f"total_affected={total_affected} failed={len(failures)} "
                            f"seconds={time.monotonic() - started:.2f}",
                            flush=True,
                        )
                    break
                except Exception as exc:
                    if attempt < args.max_retries:
                        if proxy_session is not None:
                            proxy_session.rotate(exc)
                        else:
                            try:
                                bs.logout()
                            except Exception:
                                pass
                            ensure_baostock_login(bs)
                        time.sleep(min(2**attempt, 8))
                    else:
                        failures.append(
                            f"{target.code}: {type(exc).__name__}: {exc}"
                        )
                        print(
                            f"status_hs_failed code={target.code} error={exc}",
                            flush=True,
                        )
    finally:
        if proxy_session is not None:
            proxy_session.close()
        else:
            try:
                bs.logout()
            except Exception:
                pass

    print(
        f"status_hs_finished targets={len(targets)} rows={total_rows} "
        f"affected={total_affected} failed={len(failures)}",
        flush=True,
    )
    if failures:
        raise RuntimeError("沪深状态同步失败: " + "; ".join(failures[:20]))


def fetch_current_bse_codes() -> set[str]:
    url = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
    session = curl_requests.Session(impersonate="chrome124")
    page = 0
    codes: set[str] = set()
    try:
        while True:
            response = session.post(
                url,
                data={
                    "page": str(page),
                    "typejb": "T",
                    "xxfcbj[]": "2",
                    "xxzqdm": "",
                    "sortfield": "xxzqdm",
                    "sorttype": "asc",
                },
                headers={"Referer": "https://www.bse.cn/nq/listedcompany.html"},
                timeout=30,
            )
            response.raise_for_status()
            text = response.text
            payload = json.loads(text[text.find("[") : text.rfind(")")])
            if not isinstance(payload, list) or not payload:
                raise RuntimeError("北交所股票列表返回格式异常")
            result = payload[0]
            for row in result.get("content", []):
                code = str(row.get("xxzqdm", "")).strip()
                if code:
                    codes.add(normalize_code(code))
            total_pages = int(result.get("totalPages", 0))
            page += 1
            if page >= total_pages:
                break
    finally:
        session.close()
    return codes


def bse_status_document(
    *,
    code: str,
    name: str | None,
    trade_date: date,
    raw_row: Mapping[str, Any] | None,
    detail_row: Mapping[str, Any] | None,
    is_first_trade_day: bool,
    fallback_preclose: Decimal | None = None,
) -> dict[str, Any]:
    is_st, st_source_url = bse_st_state(code, trade_date)
    suspended = raw_row is None
    document: dict[str, Any] = {
        "code": code,
        "name": name,
        "market": "BJ",
        "trade_date": trade_date.isoformat(),
        "trade_date_int": int(trade_date.strftime("%Y%m%d")),
        "is_suspended": suspended,
        "is_st": is_st,
        "suspend_source": BSE_DERIVED_SOURCE,
        "st_source": BSE_ST_SOURCE,
        "st_source_url": st_source_url,
        "source": BSE_DERIVED_SOURCE,
    }
    if suspended or is_first_trade_day:
        document.update(
            {
                "preclose": None,
                "limit_up": None,
                "limit_down": None,
                "has_price_limit": False,
                "price_limit_source": BSE_DERIVED_SOURCE,
            }
        )
        return document

    if detail_row is not None and detail_row.get("pct_chg") is not None:
        preclose = reference_price_from_pct(
            float(raw_row["close"]),
            float(detail_row["pct_chg"]),
        )
    elif fallback_preclose is not None:
        preclose = fallback_preclose
    else:
        raise ValueError(f"北交所行情缺少前收盘价: {code} {trade_date}")
    limit_up, limit_down = bse_price_limits(preclose)
    document.update(
        {
            "preclose": float(preclose),
            "limit_up": limit_up,
            "limit_down": limit_down,
            "has_price_limit": True,
            "price_limit_source": BSE_DERIVED_SOURCE,
        }
    )
    return document


def set_bse_historical_st(
    collection: Any,
    *,
    start_date: date,
    end_date: date,
    dry_run: bool,
) -> int:
    now = datetime.now()
    operations: list[UpdateMany] = [
        UpdateMany(
            {
                "market": "BJ",
                "trade_date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat(),
                },
            },
            {
                "$set": {
                    "is_st": False,
                    "st_source": BSE_ST_SOURCE,
                    "st_source_url": None,
                    "updated_at": now,
                }
            },
        )
    ]
    for code, periods in BSE_ST_PERIODS.items():
        for period in periods:
            period_end = period.end_date or end_date
            lower = max(start_date, period.start_date)
            upper = min(end_date, period_end)
            if lower >= upper and period.end_date is not None:
                continue
            date_filter: dict[str, str] = {"$gte": lower.isoformat()}
            if period.end_date is not None:
                date_filter["$lt"] = period.end_date.isoformat()
            else:
                date_filter["$lte"] = end_date.isoformat()
            operations.append(
                UpdateMany(
                    {"code": code, "trade_date": date_filter},
                    {
                        "$set": {
                            "is_st": True,
                            "st_source": BSE_ST_SOURCE,
                            "st_source_url": period.source_url,
                            "updated_at": now,
                        }
                    },
                )
            )
    if dry_run:
        return 0
    result = collection.bulk_write(operations, ordered=True)
    return int(result.modified_count)


def sync_bse_status(database: Any, args: argparse.Namespace) -> None:
    collection = database[COLLECTION_NAME]
    daily_collection = database[DAILY_COLLECTION_NAME]
    historical_modified = set_bse_historical_st(
        collection,
        start_date=args.start_date,
        end_date=args.end_date,
        dry_run=args.dry_run,
    )

    target_start = max(args.start_date, date(2026, 1, 1))
    if target_start > args.end_date:
        print(
            f"status_bse_finished historical_modified={historical_modified} "
            "rows=0 affected=0"
        )
        return
    sessions = sorted(
        str(value)
        for value in daily_collection.distinct(
            "trade_date",
            {
                "market": {"$in": ["SH", "SZ"]},
                "trade_date": {
                    "$gte": target_start.isoformat(),
                    "$lte": args.end_date.isoformat(),
                },
            },
        )
    )
    if not sessions:
        print(
            f"status_bse_finished historical_modified={historical_modified} "
            "rows=0 affected=0"
        )
        return

    raw_rows = list(
        daily_collection.find(
            {
                "market": "BJ",
                "trade_date": {"$gte": target_start.isoformat(), "$lte": sessions[-1]},
            },
            {"_id": 0, "code": 1, "name": 1, "trade_date": 1, "close": 1},
        ).sort([("code", 1), ("trade_date", 1)])
    )
    details = {
        (str(row["code"]), str(row["trade_date"])): row
        for row in database["stock_daily_detail"].find(
            {
                "adjust": "qfq",
                "trade_date": {"$gte": target_start.isoformat(), "$lte": sessions[-1]},
            },
            {"_id": 0, "code": 1, "trade_date": 1, "pct_chg": 1},
        )
        if market_for_code(str(row["code"])) == "BJ"
    }
    rows_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        rows_by_code[str(row["code"])].append(row)
    codes = sorted(code for code in rows_by_code if code.startswith("920"))
    if args.only_code:
        requested = normalize_code(args.only_code)
        codes = [code for code in codes if code == requested]
    if args.limit is not None:
        codes = codes[args.offset : args.offset + args.limit]
    else:
        codes = codes[args.offset :]
    codes = [
        code for index, code in enumerate(codes) if index % args.shard_count == args.shard_index
    ]
    current_codes = fetch_current_bse_codes()

    total_rows = 0
    total_affected = 0
    for index, code in enumerate(codes, start=1):
        code_rows = rows_by_code[code]
        raw_by_date = {str(row["trade_date"]): row for row in code_rows}
        earliest = first_trade_date(daily_collection, code)
        if earliest is None:
            continue
        last_active_date = sessions[-1] if code in current_codes else str(
            code_rows[-1]["trade_date"]
        )
        name = str(code_rows[-1].get("name") or "").strip() or None
        documents = []
        previous_row = daily_collection.find_one(
            {"code": code, "trade_date": {"$lt": target_start.isoformat()}},
            {"_id": 0, "close": 1},
            sort=[("trade_date", -1)],
        )
        previous_close = (
            Decimal(str(previous_row["close"]))
            if previous_row and previous_row.get("close") is not None
            else None
        )
        for session_date in sessions:
            parsed_date = date.fromisoformat(session_date)
            if parsed_date < earliest or session_date > last_active_date:
                continue
            documents.append(
                bse_status_document(
                    code=code,
                    name=name,
                    trade_date=parsed_date,
                    raw_row=raw_by_date.get(session_date),
                    detail_row=details.get((code, session_date)),
                    is_first_trade_day=parsed_date == earliest,
                    fallback_preclose=previous_close,
                )
            )
            raw_row = raw_by_date.get(session_date)
            if raw_row is not None:
                previous_close = Decimal(str(raw_row["close"]))
        if args.dry_run:
            rows = len(documents)
            affected = 0
        else:
            stats = upsert_documents(
                collection,
                documents,
                key_fields=("code", "trade_date"),
                batch_size=args.batch_size,
            )
            rows = stats.rows
            affected = stats.affected
        total_rows += rows
        total_affected += affected
        print(
            f"status_bse_progress={index}/{len(codes)} code={code} "
            f"rows={rows} affected={affected}",
            flush=True,
        )

    print(
        f"status_bse_finished targets={len(codes)} "
        f"historical_modified={historical_modified} rows={total_rows} "
        f"affected={total_affected}",
        flush=True,
    )


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date 不能晚于 end-date")
    if args.offset < 0:
        parser.error("offset 不能小于0")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        parser.error("shard-index 必须在[0, shard-count)范围内")
    if args.only_code:
        requested_market = market_for_code(args.only_code)
        if args.market in {"BJ"} and requested_market != "BJ":
            parser.error("only-code与market不一致")
        if args.market in {"HS", "SH", "SZ"} and requested_market == "BJ":
            parser.error("only-code与market不一致")

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    create_indexes(collection)
    try:
        if args.fill_st_gaps_only:
            planned, modified = fill_missing_st_states(
                collection,
                start_date=args.start_date,
                end_date=args.end_date,
                market=args.market,
                only_code=args.only_code,
                dry_run=args.dry_run,
            )
            print(
                f"status_st_gap_fill_finished planned={planned} "
                f"modified={modified} dry_run={args.dry_run}",
                flush=True,
            )
            return
        if args.market in {"ALL", "HS", "SH", "SZ"}:
            sync_hs_status(database, args)
        if args.market in {"ALL", "BJ"}:
            sync_bse_status(database, args)
    finally:
        client.close()


if __name__ == "__main__":
    run()
