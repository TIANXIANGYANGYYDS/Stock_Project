"""Explicit ingestion. Manual hot ranks / fund flows retain actual receipt times."""
import argparse
import asyncio
import json

from app.db.mongo import client
from app.services.market_observation_service import MarketObservationService, observation_now
from app.services.fund_flow_service import FundFlowService


async def run(args):
    service = FundFlowService() if args.kind.startswith('fund_flow') else MarketObservationService()
    try:
        now, offset = await observation_now()
        if args.kind == 'fund_flow':
            result = await service.sample(now=now, clock_offset=offset, manual=True)
        elif args.kind == 'fund_flow_daily':
            result = await service.collect_daily(now=now, clock_offset=offset)
        else:
            result = await service.collect(args.kind, now=now, clock_offset=offset,
                                           manual=True, trade_date=args.trade_date)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await service.close()
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("hot", "billboard", "fund_flow", "fund_flow_daily"))
    parser.add_argument("--trade-date", help="YYYY-MM-DD, billboard only")
    args = parser.parse_args()
    if args.kind != "billboard" and args.trade_date:
        parser.error("this feed has no historical query: omit --trade-date")
    asyncio.run(run(args))
