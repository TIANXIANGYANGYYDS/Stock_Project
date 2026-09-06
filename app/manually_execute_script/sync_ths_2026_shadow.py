"""Build a Tonghuashun-validated 2026 increment in isolated shadow tables.

The production history collections are read-only in this script.  Data is
written only when ``--apply`` is supplied, and then only to the three
``*_ths_shadow`` collections declared below.  Each direct 15-minute window is
accepted only when its date coverage and OHLC aggregation match independent
Tonghuashun ``actual`` windows.  Volume and amount remain audit fields because
Tonghuashun assigns auctions and rounding differently between periods.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests
from pymongo import ASCENDING, MongoClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.manually_execute_script.stock_history_common import (
    CN_TZ,
    StockTarget,
    market_for_code,
    non_negative_int,
    parse_date,
    positive_int,
    today_cn,
)
from app.manually_execute_script.validate_stock_history_against_ths import (
    DAILY_COLLECTION,
    EXPECTED_15_TIMES,
    EXPECTED_60_TIMES,
    FIELDS,
    MINUTE_15_COLLECTION,
    MINUTE_60_COLLECTION,
    aggregate_15m,
    compare,
    discover_ths_direct_headers,
    fetch_ths_direct_bars,
    validate_intraday_structure,
)


SHADOW_DAILY_COLLECTION = "stock_history_daily_bars_ths_shadow"
SHADOW_15M_COLLECTION = "stock_history_15m_bars_ths_shadow"
SHADOW_60M_COLLECTION = "stock_history_60m_bars_ths_shadow"
PRODUCTION_COLLECTIONS = (
    DAILY_COLLECTION,
    MINUTE_15_COLLECTION,
    MINUTE_60_COLLECTION,
)
SHADOW_COLLECTIONS = (
    SHADOW_DAILY_COLLECTION,
    SHADOW_15M_COLLECTION,
    SHADOW_60M_COLLECTION,
)
THS_SOURCE = "tonghuashun.single_kline.actual"
THS_STOCK_PAGE_URL = "https://stockpage.10jqka.com.cn/{code}/"
THS_MARKET_ID_PATTERN = re.compile(r'data-market-id="(\d+)"')


@dataclass(frozen=True)
class CodeFetchResult:
    code: str
    status: str
    documents: dict[str, list[dict[str, Any]]]
    audit: dict[str, Any]
    error: str | None = None


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "将同花顺actual 2026增量写入独立影子集合；默认只读演练，"
            "不会修改三张正式历史表。"
        )
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=None,
        help="默认从正式日线全局最大日期的下一天开始。",
    )
    parser.add_argument("--end-date", type=parse_date, default=today_cn())
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--only-code", default=None)
    selection.add_argument(
        "--retry-report",
        type=Path,
        default=None,
        help="只重试另一份本脚本报告中的fetch_failed/validation_failed代码。",
    )
    parser.add_argument("--offset", type=non_negative_int, default=0)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--workers", type=positive_int, default=4)
    parser.add_argument("--max-attempts", type=positive_int, default=30)
    parser.add_argument("--retry-delay", type=float, default=0.05)
    parser.add_argument("--progress-codes", type=positive_int, default=25)
    parser.add_argument(
        "--window-trading-days",
        type=positive_int,
        default=None,
        help="请求窗口交易日数；默认按日期范围工作日数再加一个基准日。",
    )
    parser.add_argument(
        "--allow-intraday",
        action="store_true",
        help="允许在北京时间16:00前抓取当天数据；默认拒绝未收盘数据。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="只向三个*_ths_shadow集合插入缺失记录；不提供则完全不写MongoDB。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".local/reports/stock_history_ths_shadow_2026.json"),
    )
    return parser


def calculate_window_trading_days(start_date: date, end_date: date) -> int:
    weekdays = sum(
        (start_date + timedelta(days=offset)).weekday() < 5
        for offset in range((end_date - start_date).days + 1)
    )
    return max(2, weekdays + 1)


def _target_rows(
    rows: Iterable[dict[str, Any]], *, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    start = start_date.isoformat()
    end = end_date.isoformat()
    return [
        row
        for row in rows
        if start <= str(row["key"])[:10] <= end
    ]


def derive_daily_from_15m(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[str(row["key"])[:10]].append(row)

    documents: list[dict[str, Any]] = []
    incomplete_dates: list[str] = []
    for trade_date, members in sorted(by_date.items()):
        ordered = sorted(members, key=lambda item: str(item["key"]))
        times = [str(item["key"])[11:19] for item in ordered]
        if times != list(EXPECTED_15_TIMES):
            incomplete_dates.append(trade_date)
            continue
        documents.append(
            {
                "key": f"{trade_date}T00:00:00+08:00",
                "open": float(ordered[0]["open"]),
                "high": max(float(item["high"]) for item in ordered),
                "low": min(float(item["low"]) for item in ordered),
                "close": float(ordered[-1]["close"]),
                "volume": sum(float(item["volume"]) for item in ordered),
                "amount": sum(float(item["amount"]) for item in ordered),
            }
        )
    return documents, {
        "trade_dates": len(by_date),
        "complete_dates": len(documents),
        "incomplete_dates": incomplete_dates,
    }


def comparison_is_exact(result: Mapping[str, Any]) -> bool:
    common = int(result["common_rows"])
    return (
        int(result["official_rows"]) == common
        and int(result["current_rows"]) == common
        and int(result["missing_official_keys"]) == 0
        and int(result["extra_current_keys"]) == 0
        and int(result["ohlc_exact_rows"]) == common
        and result["volume_abs_diff_max"] in (None, 0, 0.0)
        and result["amount_abs_diff_max"] in (None, 0, 0.0)
    )


def comparison_has_exact_keys_and_ohlc(result: Mapping[str, Any]) -> bool:
    common = int(result["common_rows"])
    return (
        int(result["official_rows"]) == common
        and int(result["current_rows"]) == common
        and int(result["missing_official_keys"]) == 0
        and int(result["extra_current_keys"]) == 0
        and int(result["ohlc_exact_rows"]) == common
    )


def _market_id(target: StockTarget) -> str:
    if target.market == "SH":
        return "17"
    if target.market == "SZ":
        return "33"
    raise ValueError(f"同花顺影子同步只支持沪深市场: {target.market}")


def extract_ths_market_id(html: str) -> str:
    market_ids = set(THS_MARKET_ID_PATTERN.findall(html))
    if len(market_ids) != 1:
        raise ValueError(
            f"同花顺股票页market id不唯一: {sorted(market_ids)}"
        )
    return market_ids.pop()


def discover_ths_market_id(
    session: requests.Session,
    *,
    target: StockTarget,
    headers: Mapping[str, str],
) -> tuple[str, dict[str, Any]]:
    page_url = THS_STOCK_PAGE_URL.format(code=target.code)
    response = session.get(
        page_url,
        headers={
            "User-Agent": headers.get("User-Agent", "Mozilla/5.0"),
            "Referer": page_url,
        },
        timeout=30,
    )
    response.raise_for_status()
    market_id = extract_ths_market_id(response.text)
    return market_id, {
        "source": "stock_page_data_market_id",
        "page_url": page_url,
        "market_id": market_id,
    }


def _fetch_period(
    session: requests.Session,
    *,
    headers: dict[str, str],
    target: StockTarget,
    market_id: str,
    time_period: str,
    count: int,
    max_attempts: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, audit = fetch_ths_direct_bars(
        session,
        headers=headers,
        code=target.code,
        market=market_id,
        time_period=time_period,
        end_time_ms=0,
        count=count,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
    )
    if rows is None:
        raise RuntimeError(audit.get("error") or f"{time_period}窗口不可用")
    return rows, audit


def _bar_document(
    target: StockTarget,
    row: Mapping[str, Any],
    *,
    interval: str,
    source: str,
    validation_status: str,
    reference_market_id: str,
) -> dict[str, Any]:
    timestamp = str(row["key"])
    document = {
        "code": target.code,
        "name": target.name,
        "market": target.market,
        "trade_date": timestamp[:10],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]),
        "amount": float(row["amount"]),
        "volume_unit": "share",
        "adjust": "",
        "interval": interval,
        "source": source,
        "reference_provider": THS_SOURCE,
        "reference_market_id": reference_market_id,
        "validation_status": validation_status,
    }
    if interval == "1d":
        document["trade_date_int"] = int(timestamp[:10].replace("-", ""))
    else:
        document["timestamp"] = timestamp
    return document


def fetch_validated_code(
    target: StockTarget,
    *,
    headers: dict[str, str],
    start_date: date,
    end_date: date,
    window_trading_days: int,
    max_attempts: int,
    retry_delay: float,
) -> CodeFetchResult:
    session = requests.Session()
    session.cookies.clear()
    target_headers = {
        **headers,
        "Referer": f"https://stockpage.10jqka.com.cn/{target.code}/",
    }
    try:
        market_id = _market_id(target)
        market_audit: dict[str, Any] = {
            "source": "exchange_default",
            "default_market_id": market_id,
            "market_id": market_id,
            "fallback_used": False,
        }
        try:
            direct_15, audit_15 = _fetch_period(
                session,
                headers=target_headers,
                target=target,
                market_id=market_id,
                time_period="min_15",
                count=window_trading_days * 16,
                max_attempts=min(3, max_attempts),
                retry_delay=retry_delay,
            )
        except RuntimeError as initial_error:
            market_id, page_market_audit = discover_ths_market_id(
                session,
                target=target,
                headers=target_headers,
            )
            market_audit = {
                **page_market_audit,
                "default_market_id": _market_id(target),
                "fallback_used": True,
                "initial_fetch_error": str(initial_error),
            }
            direct_15, audit_15 = _fetch_period(
                session,
                headers=target_headers,
                target=target,
                market_id=market_id,
                time_period="min_15",
                count=window_trading_days * 16,
                max_attempts=max_attempts,
                retry_delay=retry_delay,
            )
        direct_60, audit_60 = _fetch_period(
            session,
            headers=target_headers,
            target=target,
            market_id=market_id,
            time_period="min_60",
            count=window_trading_days * 4,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )
        direct_daily, audit_daily = _fetch_period(
            session,
            headers=target_headers,
            target=target,
            market_id=market_id,
            time_period="day_1",
            count=window_trading_days,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
        )

        target_15 = _target_rows(
            direct_15, start_date=start_date, end_date=end_date
        )
        target_60 = _target_rows(
            direct_60, start_date=start_date, end_date=end_date
        )
        target_daily = _target_rows(
            direct_daily, start_date=start_date, end_date=end_date
        )
        if not target_daily and not target_60 and not target_15:
            return CodeFetchResult(
                code=target.code,
                status="no_trading_rows",
                documents={"daily": [], "15m": [], "60m": []},
                audit={
                    "market_resolution": market_audit,
                    "fetch": {
                        "15m": audit_15,
                        "60m": audit_60,
                        "daily": audit_daily,
                    }
                },
            )

        structure_15 = validate_intraday_structure(
            target_15, EXPECTED_15_TIMES
        )
        structure_60 = validate_intraday_structure(
            target_60, EXPECTED_60_TIMES
        )
        derived_daily, daily_aggregation = derive_daily_from_15m(target_15)
        derived_60, aggregation_60 = aggregate_15m(
            target_15, target_minutes=60
        )
        daily_comparison = compare(target_daily, derived_daily)
        minute_60_comparison = compare(target_60, derived_60)
        coverage = {
            "15m": sorted({str(row["key"])[:10] for row in target_15}),
            "60m": sorted({str(row["key"])[:10] for row in target_60}),
            "daily": sorted({str(row["key"])[:10] for row in target_daily}),
        }
        valid = (
            structure_15["bad_trade_dates"] == 0
            and structure_60["bad_trade_dates"] == 0
            and not daily_aggregation["incomplete_dates"]
            and aggregation_60["incomplete_groups"] == 0
            and coverage["15m"] == coverage["60m"] == coverage["daily"]
            and comparison_has_exact_keys_and_ohlc(minute_60_comparison)
        )
        audit = {
            "market_resolution": market_audit,
            "fetch": {
                "15m": audit_15,
                "60m": audit_60,
                "daily": audit_daily,
            },
            "target_rows": {
                "15m": len(target_15),
                "60m": len(target_60),
                "daily": len(target_daily),
            },
            "15m_structure": structure_15,
            "60m_structure": structure_60,
            "trade_date_coverage": coverage,
            "daily_aggregation": daily_aggregation,
            "60m_aggregation": aggregation_60,
            "daily_cross_period": daily_comparison,
            "60m_cross_period": minute_60_comparison,
        }
        if not valid:
            return CodeFetchResult(
                code=target.code,
                status="validation_failed",
                documents={"daily": [], "15m": [], "60m": []},
                audit=audit,
                error="同花顺三周期日期覆盖或15m→60m OHLC不一致",
            )

        documents = {
            "daily": [
                _bar_document(
                    target,
                    row,
                    interval="1d",
                    source=THS_SOURCE,
                    validation_status="ths_direct_actual_date_coverage_exact",
                    reference_market_id=market_id,
                )
                for row in target_daily
            ],
            "15m": [
                _bar_document(
                    target,
                    row,
                    interval="15m",
                    source=THS_SOURCE,
                    validation_status="ths_direct_actual_cross_60m_ohlc_exact",
                    reference_market_id=market_id,
                )
                for row in target_15
            ],
            "60m": [
                _bar_document(
                    target,
                    row,
                    interval="60m",
                    source=THS_SOURCE,
                    validation_status="ths_direct_actual_cross_15m_ohlc_exact",
                    reference_market_id=market_id,
                )
                for row in target_60
            ],
        }
        return CodeFetchResult(
            code=target.code,
            status="validated",
            documents=documents,
            audit=audit,
        )
    except Exception as exc:
        return CodeFetchResult(
            code=target.code,
            status="fetch_failed",
            documents={"daily": [], "15m": [], "60m": []},
            audit={},
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        session.close()


def latest_trade_date(collection: Any, match: Mapping[str, Any] | None = None) -> str:
    row = collection.find_one(
        dict(match or {}),
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    if not row:
        raise RuntimeError(f"集合{collection.name}没有可用trade_date")
    return str(row["trade_date"])


def production_signature(database: Any) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "count": database[name].estimated_document_count(),
            "max_trade_date": latest_trade_date(database[name]),
        }
        for name in PRODUCTION_COLLECTIONS
    }


def load_targets_from_quant_history(
    database: Any,
    *,
    snapshot_date: str,
    only_code: str | None,
    selected_codes: list[str] | None,
    offset: int,
    limit: int | None,
) -> list[StockTarget]:
    match: dict[str, Any] = {
        "trade_date": snapshot_date,
        "market": {"$in": ["SH", "SZ"]},
    }
    if only_code:
        match["code"] = str(only_code).zfill(6)
    elif selected_codes:
        match["code"] = {"$in": selected_codes}
    rows = database[DAILY_COLLECTION].find(
        match,
        {"_id": 0, "code": 1, "name": 1, "market": 1},
        sort=[("code", ASCENDING)],
    )
    targets_by_code: dict[str, StockTarget] = {}
    for row in rows:
        code = str(row["code"]).zfill(6)
        market = str(row.get("market") or market_for_code(code))
        targets_by_code[code] = StockTarget(
            code=code,
            name=str(row.get("name") or "").strip() or None,
            market=market,
        )
    targets = sorted(targets_by_code.values(), key=lambda item: item.code)
    stop = None if limit is None else offset + limit
    return targets[offset:stop]


def load_retry_codes(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    failures = payload.get("failures") if isinstance(payload, dict) else None
    if not isinstance(failures, list):
        raise ValueError("retry-report缺少failures数组")
    codes = sorted(
        {
            str(item["code"]).zfill(6)
            for item in failures
            if isinstance(item, dict)
            and item.get("status") in {"fetch_failed", "validation_failed"}
            and item.get("code") is not None
        }
    )
    if not codes:
        raise ValueError("retry-report中没有可重试代码")
    return codes


def create_shadow_indexes(database: Any) -> None:
    for name, key_field in (
        (SHADOW_DAILY_COLLECTION, "trade_date"),
        (SHADOW_15M_COLLECTION, "timestamp"),
        (SHADOW_60M_COLLECTION, "timestamp"),
    ):
        if name not in SHADOW_COLLECTIONS or not name.endswith("_ths_shadow"):
            raise RuntimeError(f"禁止创建非影子集合索引: {name}")
        collection = database[name]
        collection.create_index(
            [("code", ASCENDING), (key_field, ASCENDING)],
            unique=True,
            name=f"uniq_{name}_{key_field}",
        )
        collection.create_index(
            [("trade_date", ASCENDING), ("code", ASCENDING)],
            name=f"idx_{name}_trade_date_code",
        )


def _same_bar(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(float(left[field]) == float(right[field]) for field in FIELDS)


def insert_shadow_documents(
    collection: Any,
    documents: list[dict[str, Any]],
    *,
    key_field: str,
    now: datetime,
) -> dict[str, int]:
    if collection.name not in SHADOW_COLLECTIONS:
        raise RuntimeError(f"禁止写入非影子集合: {collection.name}")
    if not documents:
        return {
            "planned": 0,
            "inserted": 0,
            "existing": 0,
            "conflicts": 0,
            "metadata_backfilled": 0,
            "metadata_conflicts": 0,
        }
    code = str(documents[0]["code"])
    keys = [str(document[key_field]) for document in documents]
    existing_rows = collection.find(
        {"code": code, key_field: {"$in": keys}},
        {
            "_id": 0,
            key_field: 1,
            "reference_market_id": 1,
            **{field: 1 for field in FIELDS},
        },
    )
    existing = {str(row[key_field]): row for row in existing_rows}
    inserts: list[dict[str, Any]] = []
    existing_count = 0
    conflicts = 0
    metadata_backfill_keys: list[str] = []
    metadata_conflicts = 0
    for document in documents:
        key = str(document[key_field])
        previous = existing.get(key)
        if previous is not None:
            if _same_bar(previous, document):
                existing_count += 1
                previous_market_id = previous.get("reference_market_id")
                expected_market_id = document["reference_market_id"]
                if previous_market_id is None:
                    metadata_backfill_keys.append(key)
                elif str(previous_market_id) != str(expected_market_id):
                    metadata_conflicts += 1
            else:
                conflicts += 1
            continue
        inserts.append({**document, "created_at": now, "fetched_at": now})
    if inserts:
        collection.insert_many(inserts, ordered=False)
    metadata_backfilled = 0
    if metadata_backfill_keys:
        result = collection.update_many(
            {
                "code": code,
                key_field: {"$in": metadata_backfill_keys},
                "reference_market_id": {"$exists": False},
            },
            {"$set": {"reference_market_id": documents[0]["reference_market_id"]}},
        )
        metadata_backfilled = int(result.modified_count)
    return {
        "planned": len(documents),
        "inserted": len(inserts),
        "existing": existing_count,
        "conflicts": conflicts,
        "metadata_backfilled": metadata_backfilled,
        "metadata_conflicts": metadata_conflicts,
    }


def load_production_range(
    collection: Any,
    *,
    code: str,
    start_date: date,
    end_date: date,
    key_field: str,
) -> list[dict[str, Any]]:
    projection = {"_id": 0, key_field: 1, **{field: 1 for field in FIELDS}}
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            },
        },
        projection,
    )
    return [{**row, "key": str(row[key_field])} for row in rows]


def merge_comparison_totals(
    totals: dict[str, int], result: Mapping[str, Any]
) -> None:
    for field in (
        "official_rows",
        "current_rows",
        "common_rows",
        "missing_official_keys",
        "extra_current_keys",
        "extra_current_zero_volume_flat_candidates",
        "ohlc_exact_rows",
    ):
        totals[field] = totals.get(field, 0) + int(result[field])


def run() -> None:
    args = build_argument_parser().parse_args()
    raise RuntimeError(
        "已停用：该脚本的actual口径不符合当前同花顺前复权标准；"
        "请使用 sync_ths_forward_history.py。"
    )
    settings = Settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10_000)
    database = client[settings.mongo_db_name]
    direct_session = requests.Session()
    try:
        before = production_signature(database)
        start_date = args.start_date or (
            date.fromisoformat(before[DAILY_COLLECTION]["max_trade_date"])
            + timedelta(days=1)
        )
        end_date = args.end_date
        if start_date > end_date:
            raise RuntimeError("start-date不能晚于end-date")
        now_cn = datetime.now(CN_TZ)
        if end_date > now_cn.date():
            raise RuntimeError("end-date不能晚于北京时间今天")
        if (
            end_date == now_cn.date()
            and now_cn.hour < 16
            and not args.allow_intraday
        ):
            raise RuntimeError("北京时间16:00前禁止抓取当天未收盘数据")

        snapshot_date = latest_trade_date(
            database[DAILY_COLLECTION],
            {"trade_date": {"$lt": start_date.isoformat()}},
        )
        retry_codes = (
            load_retry_codes(args.retry_report) if args.retry_report else None
        )
        targets = load_targets_from_quant_history(
            database,
            snapshot_date=snapshot_date,
            only_code=args.only_code,
            selected_codes=retry_codes,
            offset=args.offset,
            limit=args.limit,
        )
        if not targets:
            raise RuntimeError("量化日线快照中没有符合条件的沪深股票")
        window_days = args.window_trading_days or calculate_window_trading_days(
            start_date, end_date
        )
        if window_days > 25:
            raise RuntimeError("同花顺单窗口最多400根15分钟线，请缩短日期范围")

        headers, credential_metadata = discover_ths_direct_headers(
            direct_session, code=targets[0].code
        )
        direct_session.close()
        direct_session = requests.Session()
        if args.apply:
            create_shadow_indexes(database)

        report: dict[str, Any] = {
            "read_production_only": True,
            "apply": bool(args.apply),
            "production_collections": list(PRODUCTION_COLLECTIONS),
            "shadow_collections": list(SHADOW_COLLECTIONS),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "snapshot_date": snapshot_date,
            "target_count": len(targets),
            "target_selection": {
                "only_code": args.only_code,
                "retry_report": str(args.retry_report) if args.retry_report else None,
            },
            "window_trading_days": window_days,
            "credential_discovery": credential_metadata,
            "production_before": before,
            "status_counts": {},
            "document_counts": {"daily": 0, "15m": 0, "60m": 0},
            "shadow_writes": {
                "daily": {
                    "planned": 0, "inserted": 0, "existing": 0,
                    "conflicts": 0, "metadata_backfilled": 0,
                    "metadata_conflicts": 0,
                },
                "15m": {
                    "planned": 0, "inserted": 0, "existing": 0,
                    "conflicts": 0, "metadata_backfilled": 0,
                    "metadata_conflicts": 0,
                },
                "60m": {
                    "planned": 0, "inserted": 0, "existing": 0,
                    "conflicts": 0, "metadata_backfilled": 0,
                    "metadata_conflicts": 0,
                },
            },
            "production_comparison": {"daily": {}, "15m": {}, "60m": {}},
            "failures": [],
            "no_trading_codes": [],
            "validation_examples": [],
        }
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    fetch_validated_code,
                    target,
                    headers=headers,
                    start_date=start_date,
                    end_date=end_date,
                    window_trading_days=window_days,
                    max_attempts=args.max_attempts,
                    retry_delay=args.retry_delay,
                ): target
                for target in targets
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                status_counts = report["status_counts"]
                status_counts[result.status] = status_counts.get(result.status, 0) + 1
                if result.error:
                    report["failures"].append(
                        {
                            "code": result.code,
                            "status": result.status,
                            "error": result.error,
                            "audit": result.audit,
                        }
                    )
                if result.status == "no_trading_rows":
                    report["no_trading_codes"].append(result.code)
                if result.status == "validated":
                    for interval, collection_name, key_field in (
                        ("daily", DAILY_COLLECTION, "trade_date"),
                        ("15m", MINUTE_15_COLLECTION, "timestamp"),
                        ("60m", MINUTE_60_COLLECTION, "timestamp"),
                    ):
                        documents = result.documents[interval]
                        report["document_counts"][interval] += len(documents)
                        current = load_production_range(
                            database[collection_name],
                            code=result.code,
                            start_date=start_date,
                            end_date=end_date,
                            key_field=key_field,
                        )
                        reference = [
                            {**document, "key": str(document[key_field])}
                            for document in documents
                        ]
                        comparison = compare(reference, current)
                        merge_comparison_totals(
                            report["production_comparison"][interval], comparison
                        )
                    if len(report["validation_examples"]) < 9:
                        report["validation_examples"].append(
                            {"code": result.code, "audit": result.audit}
                        )
                    if args.apply:
                        now = datetime.now(CN_TZ)
                        for interval, shadow_name, key_field in (
                            ("daily", SHADOW_DAILY_COLLECTION, "trade_date"),
                            ("15m", SHADOW_15M_COLLECTION, "timestamp"),
                            ("60m", SHADOW_60M_COLLECTION, "timestamp"),
                        ):
                            stats = insert_shadow_documents(
                                database[shadow_name],
                                result.documents[interval],
                                key_field=key_field,
                                now=now,
                            )
                            for field, value in stats.items():
                                report["shadow_writes"][interval][field] += value
                if completed % args.progress_codes == 0 or completed == len(targets):
                    print(
                        f"ths_shadow_progress={completed}/{len(targets)} "
                        f"status={report['status_counts']} "
                        f"documents={report['document_counts']}",
                        flush=True,
                    )

        after = production_signature(database)
        report["production_after"] = after
        report["production_unchanged"] = before == after
        report["seconds"] = round(time.monotonic() - started, 3)
        if not report["production_unchanged"]:
            raise RuntimeError("正式历史集合签名发生变化，停止验收")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps({key: report[key] for key in (
            "apply", "target_count", "status_counts", "document_counts",
            "shadow_writes", "production_comparison", "production_unchanged",
            "seconds",
        )}, ensure_ascii=False, indent=2))
        print(f"report={args.output}")
    finally:
        direct_session.close()
        client.close()


if __name__ == "__main__":
    run()
