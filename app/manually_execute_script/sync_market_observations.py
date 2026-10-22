"""Explicit one-shot ingestion. A manual hot rank is never a historical 5m slot."""
import argparse
import asyncio
import json

from app.db.mongo import client
from app.services.market_observation_service import MarketObservationService, observation_now


async def run(args):
    service = MarketObservationService()
    try:
        now, offset = await observation_now()
        result = await service.collect(args.kind, now=now, clock_offset=offset,
                                       manual=True, trade_date=args.trade_date)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await service.close()
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("hot", "billboard"))
    parser.add_argument("--trade-date", help="YYYY-MM-DD, billboard only")
    args = parser.parse_args()
    if args.kind == "hot" and args.trade_date:
        parser.error("hot rank has no historical query: omit --trade-date")
    asyncio.run(run(args))
