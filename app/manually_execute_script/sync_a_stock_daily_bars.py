# 在项目根目录执行：
# python app/manually_execute_script/sync_a_stock_daily_bars.py --limit 3
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from pymongo import ASCENDING


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.manually_execute_script.stock_history_common import (  # noqa: E402
    EastMoneyKlineClient,
    StockTarget,
    ensure_baostock_login,
    five_years_before,
    keep_targets_without_data,
    load_targets,
    non_negative_int,
    open_database,
    optional_float,
    parse_date,
    positive_int,
    required_float,
    today_cn,
    upsert_documents,
)


COLLECTION_NAME = "stock_history_daily_bars"
BAOSTOCK_FIELDS = "date,code,open,high,low,close,volume,amount"


def build_argument_parser() -> argparse.ArgumentParser:
    end_date = today_cn()
    parser = argparse.ArgumentParser(
        description="同步当前在市 A 股最近五年的原始日线到独立 MongoDB 集合。"
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=five_years_before(end_date),
        help="开始日期，默认北京时间今天向前五年。",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=end_date,
        help="截止日期，默认北京时间今天。",
    )
    parser.add_argument("--offset", type=non_negative_int, default=0)
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument("--only-code", default=None, help="只同步一个六位代码。")
    parser.add_argument("--batch-size", type=positive_int, default=1000)
    parser.add_argument("--max-retries", type=positive_int, default=3)
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="只同步目标集合中尚无任何记录的股票。",
    )
    return parser


def daily_document(
    target: StockTarget,
    row: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    trade_date = str(row["date"])[:10]
    return {
        "code": target.code,
        "name": target.name,
        "market": target.market,
        "trade_date": trade_date,
        "trade_date_int": int(trade_date.replace("-", "")),
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


def iter_baostock_daily_documents(
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
        frequency="d",
        adjustflag="3",
    )
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock 日线请求失败: {result.error_msg}")

    fields = BAOSTOCK_FIELDS.split(",")
    while result.next():
        yield daily_document(
            target,
            dict(zip(fields, result.get_row_data())),
            source="baostock.query_history_k_data_plus",
        )
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock 日线游标失败: {result.error_msg}")


def iter_bse_daily_documents(
    akshare: Any,
    eastmoney: EastMoneyKlineClient,
    target: StockTarget,
    *,
    start_date: date,
    end_date: date,
) -> Iterable[dict[str, Any]]:
    try:
        frame = akshare.stock_zh_a_daily(
            symbol=f"bj{target.code}",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adjust="",
        )
        required_columns = {"date", "open", "high", "low", "close"}
        if not frame.empty and required_columns.issubset(frame.columns):
            for row in frame.to_dict("records"):
                yield daily_document(target, row, source="sina.stock_zh_a_daily")
            return
    except Exception as exc:
        print(
            f"daily_bse_fallback code={target.code} source=sina error={exc}",
            flush=True,
        )

    for row in eastmoney.fetch_rows(
        code=target.code,
        interval="daily",
        start_date=start_date,
        end_date=end_date,
    ):
        yield daily_document(
            target,
            {**row, "date": row["time"]},
            source="eastmoney.quote_api.proxy",
        )


def create_indexes(collection: Any) -> None:
    collection.create_index(
        [("code", ASCENDING), ("trade_date", ASCENDING)],
        unique=True,
        name="uniq_history_daily_code_trade_date",
    )
    collection.create_index(
        [("trade_date", ASCENDING), ("code", ASCENDING)],
        name="idx_history_daily_trade_date_code",
    )


def run() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("start-date 不能晚于 end-date")

    targets = load_targets(
        only_code=args.only_code,
        offset=args.offset,
        limit=args.limit,
    )
    if not targets:
        raise RuntimeError("没有可同步的股票")

    try:
        import akshare as ak
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("缺少 akshare 或 baostock，请安装 requirements.txt") from exc

    client, database = open_database()
    collection = database[COLLECTION_NAME]
    create_indexes(collection)
    if args.missing_only:
        original_count = len(targets)
        targets = keep_targets_without_data(collection, targets)
        print(
            f"daily_missing_filter before={original_count} after={len(targets)}",
            flush=True,
        )
        if not targets:
            client.close()
            print("daily_finished targets=0 rows=0 affected=0 empty=0 failed=0")
            return
    needs_baostock = any(target.market != "BJ" for target in targets)
    eastmoney = EastMoneyKlineClient() if any(
        target.market == "BJ" for target in targets
    ) else None
    if needs_baostock:
        ensure_baostock_login(bs)

    total_rows = 0
    total_affected = 0
    empty_count = 0
    failures: list[str] = []
    try:
        for index, target in enumerate(targets, start=1):
            started = time.monotonic()
            for attempt in range(1, args.max_retries + 1):
                try:
                    documents = (
                        iter_bse_daily_documents(
                            ak,
                            eastmoney,
                            target,
                            start_date=args.start_date,
                            end_date=args.end_date,
                        )
                        if target.market == "BJ"
                        else iter_baostock_daily_documents(
                            bs,
                            target,
                            start_date=args.start_date,
                            end_date=args.end_date,
                        )
                    )
                    stats = upsert_documents(
                        collection,
                        documents,
                        key_fields=("code", "trade_date"),
                        batch_size=args.batch_size,
                    )
                    if stats.rows == 0:
                        empty_count += 1
                        print(
                            f"daily_empty={index}/{len(targets)} code={target.code} "
                            f"market={target.market}",
                            flush=True,
                        )
                        break
                    total_rows += stats.rows
                    total_affected += stats.affected
                    print(
                        f"daily_progress={index}/{len(targets)} code={target.code} "
                        f"market={target.market} rows={stats.rows} affected={stats.affected} "
                        f"seconds={time.monotonic() - started:.2f}",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    if target.market != "BJ" and attempt < args.max_retries:
                        try:
                            bs.logout()
                        except Exception:
                            pass
                        ensure_baostock_login(bs)
                    if attempt == args.max_retries:
                        failures.append(f"{target.code}: {type(exc).__name__}: {exc}")
                        print(f"daily_failed code={target.code} error={exc}", flush=True)
                    else:
                        time.sleep(min(2**attempt, 8))
    finally:
        if needs_baostock:
            try:
                bs.logout()
            except Exception:
                pass
        if eastmoney is not None:
            eastmoney.close()
        client.close()

    print(
        f"daily_finished targets={len(targets)} rows={total_rows} "
        f"affected={total_affected} empty={empty_count} failed={len(failures)} "
        f"collection={COLLECTION_NAME}",
        flush=True,
    )
    if failures:
        raise RuntimeError("日线同步存在失败股票: " + "; ".join(failures[:20]))


if __name__ == "__main__":
    run()
