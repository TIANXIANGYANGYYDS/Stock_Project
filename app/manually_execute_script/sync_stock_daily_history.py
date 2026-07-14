# 用法示例（在项目根目录执行；该脚本会写 MongoDB 并使用代理）：
# 1. 先用 100 只股票验证：
#    python app/manually_execute_script/sync_stock_daily_history.py \
#      --start-date 20240101 --end-date 20260713 --limit 100
# 2. 只补一只股票：
#    python app/manually_execute_script/sync_stock_daily_history.py \
#      --start-date 20240101 --end-date 20260713 --only-code 002185
# 3. 分批补第 101～200 只股票：
#    python app/manually_execute_script/sync_stock_daily_history.py \
#      --start-date 20240101 --end-date 20260713 --offset 100 --limit 100
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.services.stock_daily_detail_service import (  # noqa: E402
    STOCK_DAILY_DEFAULT_ADJUST,
    STOCK_DAILY_DEFAULT_CONCURRENCY,
    STOCK_DAILY_DEFAULT_START_DATE,
    resolve_a_stock_target_trade_date,
    run_stock_daily_detail_sync,
)


CN_TZ = timezone(timedelta(hours=8))


def today_yyyymmdd() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")


def optional_positive_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None

    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("limit 必须大于 0")

    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("offset 不能小于 0")

    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="手动补齐 A 股详细日线历史数据。",
    )
    parser.add_argument(
        "--start-date",
        default=STOCK_DAILY_DEFAULT_START_DATE,
        help="开始日期，格式 YYYYMMDD。",
    )
    parser.add_argument(
        "--end-date",
        default=today_yyyymmdd(),
        help="结束日期，格式 YYYYMMDD，默认今天。",
    )
    parser.add_argument(
        "--adjust",
        default=STOCK_DAILY_DEFAULT_ADJUST,
        help='复权口径，例如 qfq、hfq 或 ""。',
    )
    parser.add_argument(
        "--limit",
        type=optional_positive_int,
        default=None,
        help="只处理股票列表前 N 只，便于小范围验证。",
    )
    parser.add_argument(
        "--offset",
        type=non_negative_int,
        default=0,
        help="跳过股票列表前 N 只，配合 limit 做分批补齐。",
    )
    parser.add_argument(
        "--only-code",
        default=None,
        help="只补某一只股票，代码会自动补齐 6 位。",
    )
    parser.add_argument(
        "--target-trade-date",
        default=None,
        help="写入 sync_runs 的目标交易日，格式 YYYY-MM-DD；默认解析 end-date 当天或之前的最近交易日。",
    )
    parser.add_argument(
        "--concurrency",
        type=optional_positive_int,
        default=STOCK_DAILY_DEFAULT_CONCURRENCY,
        help="协程 worker 数；实际网页并发仍受服务端信号量限制。",
    )

    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    target_trade_date = args.target_trade_date
    if target_trade_date is None:
        target_trade_date = (
            await resolve_a_stock_target_trade_date(args.end_date)
        ).target_trade_date

    result = await run_stock_daily_detail_sync(
        start_date=args.start_date,
        end_date=args.end_date,
        adjust=args.adjust,
        limit=args.limit,
        only_code=args.only_code,
        run_mode="history_backfill",
        target_trade_date=target_trade_date,
        offset=args.offset,
        concurrency=args.concurrency,
    )

    print("history_backfill_finished")
    print(f"run_id={result.run_id}")
    print(f"target_trade_date={result.target_trade_date}")
    print(f"status={result.status}")
    print(f"expected_count={result.expected_count}")
    print(f"success_count={result.success_count}")
    print(f"failed_count={result.failed_count}")
    print(f"affected_total={result.affected_total}")
    print(f"concurrency={args.concurrency}")

    if result.failed_items:
        print("failed_items_head=")
        for item in result.failed_items[:10]:
            print(item)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
