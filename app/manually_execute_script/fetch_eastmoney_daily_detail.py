# 用法示例（在项目根目录执行；该脚本只输出结果，不写 MongoDB）：
# python app/manually_execute_script/fetch_eastmoney_daily_detail.py \
#   --code 002185 --start-date 20260701 --end-date 20260713
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.crawlers.stock_daily_detail_crawler import StockDailyDetailCrawler  # noqa: E402


CN_TZ = timezone(timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    """解析只读单股逆向抓取参数。"""

    parser = argparse.ArgumentParser(
        description="通过逆向 HTTP 协议抓取单只股票并展示最新日线详情。",
    )
    parser.add_argument("--code", default="002185", help="股票代码。")
    parser.add_argument(
        "--start-date",
        default="20240101",
        help="开始日期，格式 YYYYMMDD。",
    )
    parser.add_argument(
        "--end-date",
        default=datetime.now(CN_TZ).strftime("%Y%m%d"),
        help="结束日期，格式 YYYYMMDD，默认今天。",
    )
    parser.add_argument(
        "--adjust",
        choices=("qfq", "hfq", ""),
        default="qfq",
        help="复权口径，默认前复权 qfq。",
    )
    parser.add_argument("--name", default=None, help="可选股票名称。")
    return parser.parse_args()


async def async_main() -> None:
    """逆向抓取并打印最新一条结果，且始终释放网络资源。"""

    args = parse_args()
    crawler = StockDailyDetailCrawler()
    try:
        items = await crawler.build_stock_daily_details(
            code=args.code,
            name=args.name,
            start_date=args.start_date,
            end_date=args.end_date,
            adjust=args.adjust,
        )
    finally:
        await crawler.close()

    if not items:
        raise RuntimeError("指定日期范围内没有抓到日线数据")

    print("transport=reverse_http")
    print(f"reference_url={items[-1].source.page_url}")
    print(f"daily_count={len(items)}")
    print("latest_daily_detail=")
    print(items[-1].model_dump_json(indent=2))


def main() -> None:
    """以独立 asyncio 事件循环运行只读检查。"""

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
