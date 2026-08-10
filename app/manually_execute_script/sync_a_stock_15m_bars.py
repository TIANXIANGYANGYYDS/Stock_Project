# 在项目根目录执行：
# python app/manually_execute_script/sync_a_stock_15m_bars.py --limit 3
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, DESCENDING


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.stock_history_common import (  # noqa: E402
    BaoStockProxySession,
    EastMoneyKlineClient,
    SinaMinuteClient,
    StockTarget,
    baostock_bar_timestamp,
    ensure_baostock_login,
    fill_missing_target_names,
    five_years_before,
    insert_missing_documents,
    keep_targets_without_data,
    load_targets,
    market_for_code,
    non_negative_int,
    open_database,
    optional_float,
    parse_date,
    positive_int,
    required_float,
    sina_bar_timestamp,
    today_cn,
    upsert_documents,
)


COLLECTION_NAME = "stock_history_15m_bars"
BAOSTOCK_FIELDS = "date,time,code,open,high,low,close,volume,amount"


def build_argument_parser() -> argparse.ArgumentParser:
    end_date = today_cn()
    parser = argparse.ArgumentParser(
        description=(
            "同步当前在市 A 股 15 分钟线；沪深保存近五年，北交所保存新浪当前可提供的全部历史。"
        )
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=five_years_before(end_date),
        help="沪深开始日期，默认北京时间今天向前五年；北交所不应用此下限。",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=end_date,
        help="三市场统一截止日期，默认北京时间今天。",
    )
    parser.add_argument("--offset", type=non_negative_int, default=0)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument(
        "--market",
        choices=("SH", "SZ", "BJ", "HS"),
        default=None,
        help="只同步指定市场；HS 表示沪深两市，过滤发生在 offset/limit 切片之后。",
    )
    parser.add_argument(
        "--shard-count",
        type=positive_int,
        default=1,
        help="并行分片总数，默认1。",
    )
    parser.add_argument(
        "--shard-index",
        type=non_negative_int,
        default=0,
        help="当前进程分片编号，从0开始。",
    )
    parser.add_argument("--only-code", default=None, help="只同步一个六位代码。")
    parser.add_argument("--batch-size", type=positive_int, default=1000)
    parser.add_argument("--max-retries", type=positive_int, default=3)
    parser.add_argument(
        "--targets-from-database",
        action="store_true",
        help="从 stock_daily_detail 的截止日快照读取股票列表，避免依赖外部列表接口。",
    )
    parser.add_argument(
        "--resume-missing",
        action="store_true",
        help="只请求尚未覆盖到截止日的股票，并从每只股票最后时间点继续。",
    )
    parser.add_argument(
        "--baostock-proxy",
        action="store_true",
        help="强制沪深 BaoStock TCP 连接通过三分钟 HTTP 代理池。",
    )
    parser.add_argument(
        "--proxy-login-attempts",
        type=positive_int,
        default=20,
    )
    parser.add_argument(
        "--proxy-socket-timeout",
        type=positive_int,
        default=90,
    )
    parser.add_argument(
        "--proxy-max-queries",
        type=positive_int,
        default=40,
    )
    parser.add_argument(
        "--proxy-lifetime-seconds",
        type=positive_int,
        default=150,
    )
    parser.add_argument(
        "--proxy-retry-delay",
        type=positive_int,
        default=15,
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="只同步目标集合中尚无任何记录的股票。",
    )
    return parser


def load_database_targets(
    database: Any,
    *,
    snapshot_date: date,
    only_code: str | None,
    offset: int,
    limit: int | None,
) -> list[StockTarget]:
    if only_code:
        return load_targets(only_code=only_code, offset=0, limit=None)

    rows = database["stock_daily_detail"].find(
        {
            "trade_date": snapshot_date.isoformat(),
            "adjust": "qfq",
        },
        {"_id": 0, "code": 1, "name": 1},
    )
    by_code: dict[str, StockTarget] = {}
    for row in rows:
        code = str(row["code"]).strip().zfill(6)
        try:
            market = market_for_code(code)
        except ValueError:
            continue
        by_code[code] = StockTarget(
            code=code,
            name=str(row.get("name") or "").strip() or None,
            market=market,
        )
    targets = sorted(by_code.values(), key=lambda item: item.code)
    stop = None if limit is None else offset + limit
    return targets[offset:stop]


def shard_targets(
    targets: list[StockTarget],
    *,
    shard_count: int,
    shard_index: int,
) -> list[StockTarget]:
    if shard_count <= 0:
        raise ValueError("shard_count 必须大于0")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index 必须小于 shard_count")
    return targets[shard_index::shard_count]


def plan_missing_ranges(
    collection: Any,
    targets: Iterable[StockTarget],
    *,
    default_start_date: date,
    end_date: date,
) -> list[tuple[StockTarget, date]]:
    end_timestamp = f"{end_date.isoformat()}T15:00:00+08:00"
    planned: list[tuple[StockTarget, date]] = []
    for target in targets:
        latest = collection.find_one(
            {"code": target.code},
            {"_id": 0, "timestamp": 1},
            sort=[("timestamp", DESCENDING)],
        )
        if latest and str(latest["timestamp"]) >= end_timestamp:
            continue
        start_date = default_start_date
        if latest:
            start_date = max(
                default_start_date,
                date.fromisoformat(str(latest["timestamp"])[:10]),
            )
        planned.append((target, start_date))
    return planned


def minute_document(
    target: StockTarget,
    row: Mapping[str, Any],
    *,
    timestamp: str,
    source: str,
) -> dict[str, Any]:
    return {
        "code": target.code,
        "name": target.name,
        "market": target.market,
        "trade_date": timestamp[:10],
        "timestamp": timestamp,
        "open": required_float(row["open"], field="open"),
        "high": required_float(row["high"], field="high"),
        "low": required_float(row["low"], field="low"),
        "close": required_float(row["close"], field="close"),
        "volume": optional_float(row.get("volume")) or 0.0,
        "amount": optional_float(row.get("amount")) or 0.0,
        "volume_unit": "share",
        "adjust": "",
        "source": source,
    }


def iter_baostock_15m_documents(
    baostock: Any,
    target: StockTarget,
    *,
    start_date: date,
    end_date: date,
) -> Iterable[dict[str, Any]]:
    prefix = "sh" if target.market == "SH" else "sz"
    result = baostock.query_history_k_data_plus(
        f"{prefix}.{target.code}",
        BAOSTOCK_FIELDS,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        frequency="15",
        adjustflag="3",
    )
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock 15 分钟请求失败: {result.error_msg}")

    fields = BAOSTOCK_FIELDS.split(",")
    while result.next():
        row = dict(zip(fields, result.get_row_data()))
        yield minute_document(
            target,
            row,
            timestamp=baostock_bar_timestamp(row["time"]),
            source="baostock.query_history_k_data_plus",
        )
    import baostock.common.context as baostock_context

    active_socket = getattr(baostock_context, "default_socket", None)
    socket_failure = getattr(active_socket, "failure", None)
    if socket_failure is not None:
        raise RuntimeError(f"BaoStock 15 分钟分页连接失败: {socket_failure}")
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock 15 分钟游标失败: {result.error_msg}")


def iter_bse_15m_documents(
    sina: SinaMinuteClient,
    eastmoney: EastMoneyKlineClient,
    target: StockTarget,
    *,
    end_date: date,
) -> Iterable[dict[str, Any]]:
    try:
        rows = sina.fetch_rows(code=target.code)
        if rows:
            for row in rows:
                timestamp = sina_bar_timestamp(row["day"])
                if date.fromisoformat(timestamp[:10]) > end_date:
                    continue
                yield minute_document(
                    target,
                    row,
                    timestamp=timestamp,
                    source="sina.stock_zh_a_minute",
                )
            return
    except Exception as exc:
        print(
            f"minute_bse_fallback code={target.code} source=sina error={exc}",
            flush=True,
        )

    fallback_start = end_date.replace(year=end_date.year - 1)
    for row in eastmoney.fetch_rows(
        code=target.code,
        interval="15m",
        start_date=fallback_start,
        end_date=end_date,
    ):
        timestamp = sina_bar_timestamp(row["time"])
        yield minute_document(
            target,
            row,
            timestamp=timestamp,
            source="eastmoney.quote_api.proxy",
        )


def create_indexes(collection: Any) -> None:
    collection.create_index(
        [("code", ASCENDING), ("timestamp", ASCENDING)],
        unique=True,
        name="uniq_history_15m_code_timestamp",
    )
    collection.create_index(
        [
            ("trade_date", ASCENDING),
            ("code", ASCENDING),
            ("timestamp", ASCENDING),
        ],
        name="idx_history_15m_trade_date_code_timestamp",
    )


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date 不能晚于 end-date")
    if args.shard_index >= args.shard_count:
        parser.error("shard-index 必须小于 shard-count")

    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("缺少 baostock，请安装 requirements.txt") from exc

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    create_indexes(collection)
    targets = (
        load_database_targets(
            database,
            snapshot_date=args.end_date,
            only_code=args.only_code,
            offset=args.offset,
            limit=args.limit,
        )
        if args.targets_from_database
        else load_targets(
            only_code=args.only_code,
            offset=args.offset,
            limit=args.limit,
        )
    )
    if args.market == "HS":
        targets = [target for target in targets if target.market in {"SH", "SZ"}]
    elif args.market is not None:
        targets = [target for target in targets if target.market == args.market]
    targets = shard_targets(
        targets,
        shard_count=args.shard_count,
        shard_index=args.shard_index,
    )
    if not targets:
        client.close()
        raise RuntimeError("没有可同步的股票")

    targets = fill_missing_target_names(
        database["stock_history_daily_bars"],
        targets,
    )
    if args.missing_only:
        original_count = len(targets)
        targets = keep_targets_without_data(collection, targets)
        print(
            f"minute_missing_filter before={original_count} after={len(targets)}",
            flush=True,
        )
        if not targets:
            client.close()
            print("minute_finished targets=0 rows=0 affected=0 empty=0 failed=0")
            return

    target_ranges = (
        plan_missing_ranges(
            collection,
            targets,
            default_start_date=args.start_date,
            end_date=args.end_date,
        )
        if args.resume_missing
        else [(target, args.start_date) for target in targets]
    )
    if args.resume_missing:
        print(
            f"minute_resume_filter before={len(targets)} "
            f"after={len(target_ranges)}",
            flush=True,
        )
    if not target_ranges:
        client.close()
        print("minute_finished targets=0 rows=0 affected=0 empty=0 failed=0")
        return

    needs_baostock = any(
        target.market != "BJ" for target, _start_date in target_ranges
    )
    eastmoney = EastMoneyKlineClient() if any(
        target.market == "BJ" for target, _start_date in target_ranges
    ) else None
    sina = SinaMinuteClient() if any(
        target.market == "BJ" for target, _start_date in target_ranges
    ) else None
    baostock_proxy = (
        BaoStockProxySession(
            bs,
            socket_timeout=args.proxy_socket_timeout,
            max_queries_per_proxy=args.proxy_max_queries,
            max_lifetime_seconds=args.proxy_lifetime_seconds,
            login_attempts=args.proxy_login_attempts,
            retry_delay_seconds=args.proxy_retry_delay,
        )
        if needs_baostock and args.baostock_proxy
        else None
    )
    if needs_baostock and baostock_proxy is None:
        ensure_baostock_login(bs)

    total_rows = 0
    total_affected = 0
    empty_count = 0
    failures: list[str] = []
    try:
        for index, (target, target_start_date) in enumerate(
            target_ranges,
            start=1,
        ):
            started = time.monotonic()
            for attempt in range(1, args.max_retries + 1):
                try:
                    if target.market != "BJ" and baostock_proxy is not None:
                        baostock_proxy.ensure_login()
                    documents = (
                        iter_bse_15m_documents(
                            sina,
                            eastmoney,
                            target,
                            end_date=args.end_date,
                        )
                        if target.market == "BJ"
                        else iter_baostock_15m_documents(
                            bs,
                            target,
                            start_date=target_start_date,
                            end_date=args.end_date,
                        )
                    )
                    write_documents = (
                        insert_missing_documents
                        if args.resume_missing
                        else upsert_documents
                    )
                    stats = write_documents(
                        collection,
                        documents,
                        key_fields=("code", "timestamp"),
                        batch_size=args.batch_size,
                    )
                    if target.market != "BJ" and baostock_proxy is not None:
                        baostock_proxy.note_query()
                    if stats.rows == 0:
                        empty_count += 1
                        print(
                            f"minute_empty={index}/{len(target_ranges)} "
                            f"code={target.code} market={target.market} "
                            f"start={target_start_date}",
                            flush=True,
                        )
                        break
                    total_rows += stats.rows
                    total_affected += stats.affected
                    print(
                        f"minute_progress={index}/{len(target_ranges)} "
                        f"code={target.code} market={target.market} "
                        f"start={target_start_date} rows={stats.rows} "
                        f"affected={stats.affected} "
                        f"seconds={time.monotonic() - started:.2f}",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    if target.market != "BJ" and attempt < args.max_retries:
                        if baostock_proxy is not None:
                            baostock_proxy.rotate(exc)
                        else:
                            try:
                                bs.logout()
                            except Exception:
                                pass
                            ensure_baostock_login(bs)
                    if attempt == args.max_retries:
                        failures.append(f"{target.code}: {type(exc).__name__}: {exc}")
                        print(f"minute_failed code={target.code} error={exc}", flush=True)
                    else:
                        time.sleep(min(2**attempt, 8))
    finally:
        if baostock_proxy is not None:
            baostock_proxy.close()
        elif needs_baostock:
            try:
                bs.logout()
            except Exception:
                pass
        if eastmoney is not None:
            eastmoney.close()
        if sina is not None:
            sina.close()
        client.close()

    print(
        f"minute_finished targets={len(target_ranges)} rows={total_rows} "
        f"affected={total_affected} empty={empty_count} failed={len(failures)} "
        f"collection={COLLECTION_NAME}",
        flush=True,
    )
    if failures:
        raise RuntimeError("15 分钟同步存在失败股票: " + "; ".join(failures[:20]))


if __name__ == "__main__":
    run()
