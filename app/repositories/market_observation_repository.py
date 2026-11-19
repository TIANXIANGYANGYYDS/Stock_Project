"""Atomic observations with explicit coverage; failed fetches never replace data."""
from datetime import datetime

from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.base import BaseMongoRepository

HOT_COLLECTION = "stock_ths_hot_rank_snapshots"
BILLBOARD_COLLECTION = "stock_billboard_daily"
FUND_FLOW_COLLECTION = "stock_fund_flow_snapshots"
RUN_COLLECTION = "market_observation_runs"
COLLECTIONS = {"hot": HOT_COLLECTION, "billboard": BILLBOARD_COLLECTION}


class MarketObservationRepository(BaseMongoRepository):
    collection_name = HOT_COLLECTION

    async def create_indexes(self):
        await self.database[HOT_COLLECTION].create_index([("trade_date", 1), ("observed_at", -1)])
        await self.database[HOT_COLLECTION].create_index([("stocks.code", 1), ("trade_date", -1)])
        await self.database[BILLBOARD_COLLECTION].create_index("trade_date", unique=True)
        await self.database[BILLBOARD_COLLECTION].create_index([("records.code", 1), ("trade_date", -1)])
        await self.database[RUN_COLLECTION].create_index([("kind", 1), ("finished_at", -1)])

    async def exists_snapshot(self, kind, identity):
        name = COLLECTIONS[kind]
        return await self.database[name].find_one({"_id": identity}, {"_id": 1}) is not None

    async def save(self, kind, identity, document):
        name = COLLECTIONS[kind]
        # One immutable snapshot per five-minute slot / daily collection.
        await self.database[name].update_one({"_id": identity}, {"$setOnInsert": document}, upsert=True)

    async def record_run(self, kind, identity, *, status, error=None):
        await self.database[RUN_COLLECTION].update_one(
            {"_id": f"{kind}:{identity}"},
            {"$set": {"kind": kind, "target": identity, "status": status,
                      "error": error, "finished_at": datetime.now(CN_TZ)},
             "$inc": {"attempts": 1}}, upsert=True)
