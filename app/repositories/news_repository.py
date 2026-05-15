from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pymongo import UpdateOne
from pymongo.results import BulkWriteResult, UpdateResult

from app.models import FetchedNews, News
from app.repositories.base import BaseMongoRepository


@dataclass
class NewsBatchWriteResult:
    total_count: int
    inserted_count: int
    existing_count: int


class NewsRepository(BaseMongoRepository):
    model_class = News

    async def create_indexes(self) -> None:
        await self.collection.create_index("event_id", unique=True, name="uk_event_id")
        await self.collection.create_index(
            [("source", 1), ("publish_ts", -1)],
            name="idx_source_publish_ts",
        )
        await self.collection.create_index(
            [("status.status", 1), ("publish_ts", -1)],
            name="idx_status_publish_ts",
        )

    def _build_document(
        self,
        row: News | FetchedNews | dict[str, Any],
    ) -> dict[str, Any]:
        return self.build_document(row)

    async def save_rows(
        self,
        rows: Sequence[News | FetchedNews | dict[str, Any]],
    ) -> NewsBatchWriteResult:
        if not rows:
            return NewsBatchWriteResult(
                total_count=0,
                inserted_count=0,
                existing_count=0,
            )

        write_result = await self.upsert_many(rows)
        inserted_count = 0 if write_result is None else int(getattr(write_result, "upserted_count", 0))
        total_count = len(rows)

        return NewsBatchWriteResult(
            total_count=total_count,
            inserted_count=inserted_count,
            existing_count=max(total_count - inserted_count, 0),
        )

    async def upsert_one(
        self,
        row: News | FetchedNews | dict[str, Any],
    ) -> UpdateResult:
        document = self._build_document(row)

        return await self.update_one(
            {"event_id": document["event_id"]},
            {
                # 重复抓取时保留已有状态和后续 LLM 分析结果。
                "$setOnInsert": document,
            },
            upsert=True,
        )

    async def upsert_many(
        self,
        rows: Sequence[News | FetchedNews | dict[str, Any]],
    ) -> BulkWriteResult | None:
        if not rows:
            return None

        operations = []
        for row in rows:
            document = self._build_document(row)
            operations.append(
                UpdateOne(
                    {"event_id": document["event_id"]},
                    {
                        # 重复抓取时保留已有状态和后续 LLM 分析结果。
                        "$setOnInsert": document,
                    },
                    upsert=True,
                )
            )

        return await self.bulk_write(operations, ordered=False)

    async def get_existing_event_ids(self, event_ids: Sequence[str]) -> set[str]:
        if not event_ids:
            return set()

        cursor = self.collection.find(
            {"event_id": {"$in": list(event_ids)}},
            projection={"event_id": 1, "_id": 0},
        )

        existing_event_ids: set[str] = set()
        async for doc in cursor:
            event_id = doc.get("event_id")
            if event_id:
                existing_event_ids.add(event_id)

        return existing_event_ids

    async def find_by_event_id(self, event_id: str) -> dict[str, Any] | None:
        if not event_id:
            return None

        return await self.find_one({"event_id": event_id}, projection={"_id": 0})