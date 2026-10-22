"""python -m app.manually_execute_script.sync_short_sentiment [--trade-date YYYY-MM-DD]"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
import json

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.services.short_sentiment.service import SentimentWorker


async def sync_available(worker, **kwargs):
    while True:
        result = await worker.sync(**kwargs)
        if result["status"] != "busy":
            return result
        await asyncio.sleep(.5)


async def run(args):
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        worker = SentimentWorker(client[settings.mongo_db_name], scope=args.scope)
        if args.backfill_days:
            rows = await client[settings.mongo_db_name]["market_sentiment_daily"].find(
                {"scope": "hs_mainboard"}, {"trade_date": 1}).sort([("trade_date", -1)]).limit(args.backfill_days).to_list(length=args.backfill_days)
            for row in reversed(rows):
                if worker.daily_source and await worker.daily_source.get(row["trade_date"]) is None:
                    print(json.dumps({"status": "missing_daily_source", "trade_date": row["trade_date"]}), flush=True)
                    continue
                result = await sync_available(worker, trade_date=row["trade_date"])
                while result["status"] == "updated":
                    result = await sync_available(worker, trade_date=row["trade_date"])
                print(json.dumps(result, ensure_ascii=False), flush=True)
            return
        while True:
            result = await sync_available(worker, trade_date=args.trade_date.isoformat() if args.trade_date else None,
                                          max_batches=12, replay=args.replay)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result["status"] in {"waiting", "waiting_finalization", "final", "finalized", "no_sources"}:
                break
            await asyncio.sleep(0)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="生成短线情绪查询快照；只写独立看板集合")
    parser.add_argument("--scope", choices=("all_a", "hs_mainboard"), default="all_a")
    parser.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--replay", action="store_true", help="按已存快照重放；完成后不加此参数执行一次收盘定稿")
    parser.add_argument("--backfill-days", type=int, help="用已完成的每日原始依据初始化最近N日的看板日线（1~366）")
    args = parser.parse_args()
    if args.backfill_days is not None and (not 1 <= args.backfill_days <= 366 or args.trade_date or args.replay):
        parser.error("--backfill-days 应为1~366，且不能与 --trade-date/--replay 一起使用")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
