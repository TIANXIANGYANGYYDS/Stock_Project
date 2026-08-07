from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, UpdateOne

from app.models.realtime_minute_bar import RealtimeMinuteBar
from app.repositories.base import BaseMongoRepository


class RealtimeMinuteBarRepository(BaseMongoRepository):
    """Mongo persistence for locally aggregated real-time minute bars."""

    model_class = RealtimeMinuteBar

    def __init__(self, database: AsyncIOMotorDatabase | None = None) -> None:
        super().__init__(database=database)

    async def create_indexes(self) -> None:
        await self.collection.update_many(
            {"interval": {"$exists": False}},
            {"$set": {"interval": "1m"}},
        )
        existing_indexes = await self.collection.index_information()
        for obsolete_name in (
            "uniq_realtime_code_timestamp",
            "idx_realtime_trade_date_timestamp_code",
        ):
            if obsolete_name in existing_indexes:
                await self.collection.drop_index(obsolete_name)

        await self.collection.create_index(
            [("code", ASCENDING), ("interval", ASCENDING), ("timestamp", ASCENDING)],
            unique=True,
            name="uniq_realtime_code_interval_timestamp",
        )
        await self.collection.create_index(
            [
                ("trade_date", ASCENDING),
                ("interval", ASCENDING),
                ("timestamp", ASCENDING),
                ("code", ASCENDING),
            ],
            name="idx_realtime_trade_date_interval_timestamp_code",
        )

    async def upsert_bars(self, bars: Iterable[RealtimeMinuteBar]) -> int:
        operations: list[UpdateOne] = []
        for bar in bars:
            document = self.build_document(bar)
            key = {
                "code": document["code"],
                "interval": document["interval"],
                "timestamp": document["timestamp"],
            }
            created_at = document.pop("created_at")
            operations.append(
                UpdateOne(
                    key,
                    {"$set": document, "$setOnInsert": {"created_at": created_at}},
                    upsert=True,
                )
            )
        if not operations:
            return 0
        result = await self.collection.bulk_write(operations, ordered=False)
        return int(result.upserted_count + result.modified_count)
