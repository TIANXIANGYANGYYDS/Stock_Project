# 用法示例（在项目根目录执行；该脚本只输出结果，不写 MongoDB）：
# 1. 抓取单只股票，失败时诊断默认写入 .local/logs/：
#    python app/manually_execute_script/fetch_eastmoney_quote_page_daily_detail.py \
#      --code 002185 --start-date 20260701 --end-date 20260713
# 2. 指定诊断文件：
#    python app/manually_execute_script/fetch_eastmoney_quote_page_daily_detail.py \
#      --code 002185 --start-date 20260701 --end-date 20260713 \
#      --diagnostics-path .local/logs/eastmoney_runtime_diagnostics_002185.json
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.crawlers.stock_daily_detail_crawler import (  # noqa: E402
    EastMoneyQuotePageFetcher,
    StockDailyDetailCrawler,
)


CN_TZ = timezone(timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取单只股票的东方财富行情页并展示最新日线详情。",
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
    parser.add_argument("--max-retry", type=int, default=3, help="代理池最大尝试次数。")
    parser.add_argument(
        "--diagnostics-path",
        default=None,
        help="失败时的页面运行时诊断 JSON 路径。",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    crawler = StockDailyDetailCrawler(max_retry=args.max_retry)
    try:
        try:
            items = await crawler.build_stock_daily_details(
                code=args.code,
                name=args.name,
                start_date=args.start_date,
                end_date=args.end_date,
                adjust=args.adjust,
            )
        except Exception:
            diagnostics_path = args.diagnostics_path or (
                PROJECT_ROOT
                / ".local"
                / "logs"
                / f"eastmoney_runtime_diagnostics_{args.code}.json"
            )
            saved = crawler.quote_page_fetcher.dump_last_runtime_diagnostics(
                diagnostics_path
            )
            print(f"runtime_diagnostics={saved}", file=sys.stderr)
            raise
    finally:
        await crawler.close()

    if not items:
        raise RuntimeError("指定日期范围内没有抓到日线数据")

    print(f"page_url={EastMoneyQuotePageFetcher.get_daily_page_url(args.code)}")
    print(f"daily_count={len(items)}")
    print("latest_daily_detail=")
    print(items[-1].model_dump_json(indent=2))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
