# 在项目根目录执行：
# python app/manually_execute_script/sync_stock_adjust_factors.py
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import requests
from pymongo import UpdateMany


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.stock_history_common import (  # noqa: E402
    CN_TZ,
    market_for_code,
    normalize_code,
    open_database,
    parse_date,
    positive_int,
)
from app.manually_execute_script.sync_a_stock_daily_bars import (  # noqa: E402
    COLLECTION_NAME,
)


SOURCE_NAME = "sina.finance.qfq.js.local_anchor_ratio"
PRECLOSE_FALLBACK_SOURCE = "baostock.preclose.local_anchor_chain"
SINA_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"


@dataclass(frozen=True)
class AdjustmentEvent:
    effective_date: date
    factor: Decimal


@dataclass(frozen=True)
class FactorAnchor:
    anchor_date: date
    factor: Decimal
    method: str


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用新浪复权事件按本地历史因子比例延伸2026年逐日复权因子。"
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=date(2026, 1, 1),
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=date(2026, 12, 31),
    )
    parser.add_argument("--market", choices=("SH", "SZ", "BJ"), default=None)
    parser.add_argument("--only-code", default=None)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--workers", type=positive_int, default=12)
    parser.add_argument("--batch-size", type=positive_int, default=1000)
    parser.add_argument("--progress", type=positive_int, default=100)
    parser.add_argument(
        "--max-overlap-relative-error",
        type=float,
        default=0.005,
        help="2024至锚点重叠区允许的最大相对误差，默认0.5%。",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def sina_symbol(code: str, market: str | None = None) -> str:
    normalized = normalize_code(code)
    resolved_market = market or market_for_code(normalized)
    return f"{resolved_market.lower()}{normalized}"


def parse_sina_adjustment_events(text: str) -> list[AdjustmentEvent]:
    equals_index = text.find("=")
    if equals_index < 0:
        raise ValueError("新浪复权接口缺少赋值符号")
    try:
        payload, _end = json.JSONDecoder().raw_decode(text[equals_index + 1 :].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("新浪复权接口返回无效JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("新浪复权接口缺少data列表")

    events: list[AdjustmentEvent] = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ValueError("新浪复权事件不是对象")
        factor = Decimal(str(row.get("f", "")))
        if factor <= 0:
            raise ValueError(f"新浪复权因子必须大于0: {factor}")
        events.append(
            AdjustmentEvent(
                effective_date=date.fromisoformat(str(row.get("d", ""))),
                factor=factor,
            )
        )
    return sorted(events, key=lambda item: item.effective_date)


def fetch_sina_adjustment_events(
    code: str,
    market: str,
    *,
    max_attempts: int = 4,
) -> list[AdjustmentEvent]:
    symbol = sina_symbol(code, market)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                SINA_URL.format(symbol=symbol),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": (
                        "https://finance.sina.com.cn/realstock/company/"
                        f"{symbol}/nc.shtml"
                    ),
                },
                timeout=20,
            )
            response.raise_for_status()
            return parse_sina_adjustment_events(response.text)
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"新浪复权因子请求失败 code={code}: {last_error}")


def effective_sina_factor(
    events: Sequence[AdjustmentEvent],
    target_date: date,
) -> Decimal:
    if not events:
        raise ValueError("复权事件不能为空")
    effective = events[0].factor
    for event in events:
        if event.effective_date > target_date:
            break
        effective = event.factor
    return effective


def extend_adjustment_factor(
    anchor: FactorAnchor,
    target_date: date,
    events: Sequence[AdjustmentEvent],
) -> Decimal:
    return (
        anchor.factor
        * effective_sina_factor(events, anchor.anchor_date)
        / effective_sina_factor(events, target_date)
    )


def load_target_codes(
    collection: Any,
    *,
    start_date: date,
    end_date: date,
    market: str | None,
    only_code: str | None,
    limit: int | None,
) -> list[tuple[str, str]]:
    filters: dict[str, Any] = {
        "trade_date": {
            "$gte": start_date.isoformat(),
            "$lte": end_date.isoformat(),
        }
    }
    if only_code:
        filters["code"] = normalize_code(only_code)
    if market:
        filters["market"] = market
    codes = sorted(str(code) for code in collection.distinct("code", filters))
    if limit is not None:
        codes = codes[:limit]
    targets = []
    for code in codes:
        resolved_market = market_for_code(code)
        if resolved_market == "BJ" and not code.startswith("920"):
            continue
        targets.append((code, resolved_market))
    return targets


def load_factor_anchor(
    collection: Any,
    *,
    code: str,
    start_date: date,
) -> FactorAnchor:
    row = collection.find_one(
        {
            "code": code,
            "trade_date": {"$lt": start_date.isoformat()},
            "adj_factor": {"$ne": None},
        },
        {"_id": 0, "trade_date": 1, "adj_factor": 1},
        sort=[("trade_date", -1)],
    )
    if row is not None:
        return FactorAnchor(
            anchor_date=date.fromisoformat(str(row["trade_date"])),
            factor=Decimal(str(row["adj_factor"])),
            method="local_history_anchor",
        )

    first_row = collection.find_one(
        {"code": code, "trade_date": {"$gte": start_date.isoformat()}},
        {"_id": 0, "trade_date": 1},
        sort=[("trade_date", 1)],
    )
    if first_row is None:
        raise ValueError(f"目标区间没有日线: {code}")
    return FactorAnchor(
        anchor_date=date.fromisoformat(str(first_row["trade_date"])),
        factor=Decimal("1"),
        method="first_available_day_unit_anchor",
    )


def max_overlap_relative_error(
    collection: Any,
    *,
    code: str,
    anchor: FactorAnchor,
    events: Sequence[AdjustmentEvent],
    end_date: date,
) -> Decimal:
    if anchor.method != "local_history_anchor":
        return Decimal("0")
    if effective_sina_factor(events, anchor.anchor_date) == effective_sina_factor(
        events,
        end_date,
    ):
        return Decimal("0")
    overlap_start = date(2025, 1, 1)
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": overlap_start.isoformat(),
                "$lte": anchor.anchor_date.isoformat(),
            },
            "adj_factor": {"$ne": None},
        },
        {"_id": 0, "trade_date": 1, "adj_factor": 1},
    )
    maximum = Decimal("0")
    for row in rows:
        actual = Decimal(str(row["adj_factor"]))
        predicted = extend_adjustment_factor(
            anchor,
            date.fromisoformat(str(row["trade_date"])),
            events,
        )
        denominator = max(abs(actual), Decimal("0.00000001"))
        maximum = max(maximum, abs(predicted - actual) / denominator)
    return maximum


def target_factor_groups(
    collection: Any,
    *,
    code: str,
    start_date: date,
    end_date: date,
    anchor: FactorAnchor,
    events: Sequence[AdjustmentEvent],
) -> list[tuple[str, str, float]]:
    rows = collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            },
        },
        {"_id": 0, "trade_date": 1},
    ).sort("trade_date", 1)
    groups: list[tuple[str, str, float]] = []
    group_start: str | None = None
    group_end: str | None = None
    group_factor: Decimal | None = None
    for row in rows:
        trade_date = str(row["trade_date"])
        factor = extend_adjustment_factor(
            anchor,
            date.fromisoformat(trade_date),
            events,
        ).quantize(Decimal("0.00000001"))
        if group_factor is not None and factor != group_factor:
            assert group_start is not None and group_end is not None
            groups.append((group_start, group_end, float(group_factor)))
            group_start = trade_date
        elif group_start is None:
            group_start = trade_date
        group_end = trade_date
        group_factor = factor
    if group_factor is not None:
        assert group_start is not None and group_end is not None
        groups.append((group_start, group_end, float(group_factor)))
    return groups


def preclose_factor_groups(
    daily_collection: Any,
    status_collection: Any,
    *,
    code: str,
    start_date: date,
    end_date: date,
    anchor: FactorAnchor,
) -> list[tuple[str, str, float]]:
    anchor_row = daily_collection.find_one(
        {"code": code, "trade_date": anchor.anchor_date.isoformat()},
        {"_id": 0, "close": 1},
    )
    if anchor_row is None or anchor_row.get("close") is None:
        raise ValueError(f"复权锚点缺少原始收盘价: {code} {anchor.anchor_date}")
    previous_close = Decimal(str(anchor_row["close"]))
    current_factor = anchor.factor
    rows = daily_collection.find(
        {
            "code": code,
            "trade_date": {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            },
        },
        {"_id": 0, "trade_date": 1, "close": 1},
    ).sort("trade_date", 1)

    groups: list[tuple[str, str, float]] = []
    group_start: str | None = None
    group_end: str | None = None
    group_factor: Decimal | None = None
    for row in rows:
        trade_date = str(row["trade_date"])
        status = status_collection.find_one(
            {"code": code, "trade_date": trade_date},
            {"_id": 0, "preclose": 1},
        )
        if status is None or status.get("preclose") in (None, 0, ""):
            raise ValueError(f"回退计算缺少前收盘价: {code} {trade_date}")
        preclose = Decimal(str(status["preclose"]))
        is_synthetic_anchor_day = (
            anchor.method == "first_available_day_unit_anchor"
            and trade_date == anchor.anchor_date.isoformat()
        )
        if not is_synthetic_anchor_day:
            ratio = previous_close / preclose
            if abs(ratio - Decimal("1")) > Decimal("0.000001"):
                current_factor *= ratio
        factor = current_factor.quantize(Decimal("0.00000001"))
        if group_factor is not None and factor != group_factor:
            assert group_start is not None and group_end is not None
            groups.append((group_start, group_end, float(group_factor)))
            group_start = trade_date
        elif group_start is None:
            group_start = trade_date
        group_end = trade_date
        group_factor = factor
        previous_close = Decimal(str(row["close"]))
    if group_factor is not None:
        assert group_start is not None and group_end is not None
        groups.append((group_start, group_end, float(group_factor)))
    return groups


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date 不能晚于 end-date")
    if args.max_overlap_relative_error < 0:
        parser.error("max-overlap-relative-error 不能小于0")

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    status_collection = database["stock_daily_trading_status"]
    targets = load_target_codes(
        collection,
        start_date=args.start_date,
        end_date=args.end_date,
        market=args.market,
        only_code=args.only_code,
        limit=args.limit,
    )
    if not targets:
        client.close()
        raise RuntimeError("目标区间没有需要补因子的日线")

    operations: list[UpdateMany] = []
    updated = 0
    completed = 0
    failures: list[str] = []
    overlap_failures: list[str] = []
    preclose_fallbacks = 0
    started = time.monotonic()

    def flush() -> None:
        nonlocal updated
        if not operations:
            return
        if args.dry_run:
            operations.clear()
            return
        result = collection.bulk_write(operations, ordered=False)
        updated += int(result.modified_count)
        operations.clear()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(fetch_sina_adjustment_events, code, market): (
                    code,
                    market,
                )
                for code, market in targets
            }
            for future in as_completed(futures):
                code, _market = futures[future]
                try:
                    events = future.result()
                    anchor = load_factor_anchor(
                        collection,
                        code=code,
                        start_date=args.start_date,
                    )
                    overlap_error = (
                        max_overlap_relative_error(
                            collection,
                            code=code,
                            anchor=anchor,
                            events=events,
                            end_date=args.end_date,
                        )
                        if events
                        else Decimal("0")
                    )
                    use_preclose_fallback = not events or overlap_error > Decimal(
                        str(args.max_overlap_relative_error)
                    )
                    if use_preclose_fallback:
                        if events:
                            overlap_failures.append(
                                f"{code}:{float(overlap_error):.8f}"
                            )
                        groups = preclose_factor_groups(
                            collection,
                            status_collection,
                            code=code,
                            start_date=args.start_date,
                            end_date=args.end_date,
                            anchor=anchor,
                        )
                        factor_source = PRECLOSE_FALLBACK_SOURCE
                        preclose_fallbacks += 1
                    else:
                        groups = target_factor_groups(
                            collection,
                            code=code,
                            start_date=args.start_date,
                            end_date=args.end_date,
                            anchor=anchor,
                            events=events,
                        )
                        factor_source = SOURCE_NAME
                    now = datetime.now(CN_TZ)
                    for group_start, group_end, factor in groups:
                        operations.append(
                            UpdateMany(
                                {
                                    "code": code,
                                    "trade_date": {
                                        "$gte": group_start,
                                        "$lte": group_end,
                                    },
                                },
                                {
                                    "$set": {
                                        "adj_factor": factor,
                                        "adj_factor_source": factor_source,
                                        "adj_factor_anchor_date": (
                                            anchor.anchor_date.isoformat()
                                        ),
                                        "adj_factor_anchor_method": anchor.method,
                                        "adj_factor_overlap_relative_error": float(
                                            overlap_error
                                        ),
                                        "updated_at": now,
                                    }
                                },
                            )
                        )
                    if len(operations) >= args.batch_size:
                        flush()
                except Exception as exc:
                    failures.append(f"{code}: {type(exc).__name__}: {exc}")
                finally:
                    completed += 1
                    if completed % args.progress == 0 or completed == len(targets):
                        print(
                            f"adj_factor_progress={completed}/{len(targets)} "
                            f"updated={updated} queued={len(operations)} "
                            f"overlap_failed={len(overlap_failures)} "
                            f"preclose_fallbacks={preclose_fallbacks} "
                            f"failed={len(failures)} "
                            f"seconds={time.monotonic() - started:.2f}",
                            flush=True,
                        )
        flush()
    finally:
        client.close()

    print(
        f"adj_factor_finished targets={len(targets)} updated={updated} "
        f"overlap_failed={len(overlap_failures)} failed={len(failures)} "
        f"preclose_fallbacks={preclose_fallbacks} "
        f"dry_run={args.dry_run} collection={COLLECTION_NAME}",
        flush=True,
    )
    if overlap_failures:
        print(
            "adj_factor_overlap_failures=" + ";".join(overlap_failures[:50]),
            flush=True,
        )
    if failures:
        raise RuntimeError("复权因子同步失败: " + "; ".join(failures[:20]))


if __name__ == "__main__":
    run()
