from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.models.creator_monitoring import (
    CreatorOpinion,
    CreatorOpinionAnalysisDisplay,
    CreatorWork,
    CreatorWorkAnalysis,
)
from app.repositories.creator_monitoring_repository import (
    CreatorOpinionAnalysisRepository,
    CreatorWorkRepository,
)


NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.limit_value: int | None = None

    def limit(self, value: int):
        self.limit_value = value
        return self

    async def to_list(self, *, length: int | None):
        limit = self.limit_value if self.limit_value is not None else length
        return self.rows if limit is None else self.rows[:limit]


class FakeCollection:
    def __init__(self) -> None:
        self.index_calls: list[tuple[Any, dict[str, Any]]] = []
        self.bulk_operations: list[Any] = []
        self.find_rows: list[dict[str, Any]] = []
        self.find_calls: list[dict[str, Any]] = []
        self.last_cursor: FakeCursor | None = None
        self.find_one_result: dict[str, Any] | None = None
        self.claim_result: dict[str, Any] | None = None
        self.claim_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.replace_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.count_result = 0

    async def create_index(self, keys, **kwargs):
        self.index_calls.append((keys, kwargs))
        return kwargs.get("name")

    async def bulk_write(self, operations, *, ordered):
        self.bulk_operations.extend(operations)
        return SimpleNamespace(upserted_count=len(operations))

    def find(self, filters, **kwargs):
        self.find_calls.append({"filters": filters, **kwargs})
        self.last_cursor = FakeCursor(self.find_rows)
        return self.last_cursor

    async def find_one(self, filters, *, projection=None, sort=None):
        self.find_calls.append(
            {"filters": filters, "projection": projection, "sort": sort}
        )
        return self.find_one_result

    async def find_one_and_update(self, filters, update, **kwargs):
        self.claim_calls.append({"filters": filters, "update": update, **kwargs})
        return self.claim_result

    async def update_one(self, filters, update, *, upsert=False):
        self.update_calls.append(
            {"filters": filters, "update": update, "upsert": upsert}
        )
        return SimpleNamespace(modified_count=1)

    async def update_many(self, filters, update, *, upsert=False):
        self.update_calls.append(
            {"filters": filters, "update": update, "upsert": upsert}
        )
        return SimpleNamespace(modified_count=1)

    async def replace_one(self, filters, replacement, *, upsert=False):
        self.replace_calls.append(
            {"filters": filters, "replacement": replacement, "upsert": upsert}
        )
        return SimpleNamespace(modified_count=1)

    async def delete_one(self, filters):
        self.delete_calls.append(filters)
        return SimpleNamespace(deleted_count=1)

    async def count_documents(self, filters):
        self.find_calls.append({"filters": filters, "operation": "count"})
        return self.count_result


class FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


def build_opinion() -> CreatorOpinion:
    return CreatorOpinion(
        opinion_id="douyin:work-1:1",
        work_key="douyin:work-1",
        target_type="sector",
        target_name="半导体",
        direction="bullish",
        stance_score=60,
        claim="未来一周相对沪深300走强",
        horizon="未来5个交易日",
        valid_from=NOW,
        valid_until=NOW + timedelta(days=7),
        metric="相对沪深300超额收益",
        source_quote="半导体未来一周还有机会。",
    )


def build_work(**overrides) -> CreatorWork:
    values = {
        "creator_id": "creator-1",
        "account_id": "douyin:account-1",
        "platform": "douyin",
        "platform_work_id": "work-1",
        "content_type": "video",
        "canonical_url": "https://www.douyin.com/video/work-1",
        "published_at": NOW,
        "first_seen_at": NOW,
        "fetched_at": NOW,
        "media_url": "https://example.com/work-1.mp4",
    }
    values.update(overrides)
    return CreatorWork(**values)


def build_analysis() -> CreatorWorkAnalysis:
    return CreatorWorkAnalysis(
        summary="看好半导体。",
        opinions=[build_opinion()],
        analysis_version="v1",
        analysis_model="test-model",
        analyzed_at=NOW,
    )


def test_work_indexes_and_idempotent_set_on_insert() -> None:
    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]

    asyncio.run(repository.create_indexes())
    result = asyncio.run(repository.save_works([build_work()]))

    collection = database["creator_works"]
    assert [options["name"] for _, options in collection.index_calls] == [
        "uk_work_key",
        "idx_account_published_at",
        "idx_creator_published_at",
        "idx_status_processing_lease",
        "idx_status_creator_published_at",
        "idx_a_share_relevant_published_at",
    ]
    assert result.inserted_count == 1
    operation = collection.bulk_operations[0]
    assert operation._filter == {"work_key": "douyin:work-1"}
    assert operation._doc["$setOnInsert"]["status"]["status"] == (
        "pending_extraction"
    )
    assert "$set" not in operation._doc


def test_claim_and_stage_updates_use_attempt_fencing() -> None:
    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]
    collection = database["creator_works"]
    claimed = build_work().model_dump(mode="python")
    claimed["status"] = {"status": "extracting", "reason": None}
    claimed["processing_attempts"] = 1
    claimed["processing_started_at"] = NOW
    collection.claim_result = claimed

    result = asyncio.run(
        repository.claim_next_for_extraction(
            lease_timeout_seconds=1800,
            now=NOW,
        )
    )
    asyncio.run(
        repository.mark_extraction_success(
            "douyin:work-1",
            "转写正文",
            expected_attempt=1,
            asr_text="转写正文",
        )
    )
    asyncio.run(
        repository.mark_analysis_success(
            "douyin:work-1",
            build_analysis(),
            expected_attempt=2,
        )
    )

    assert result is not None
    assert result.processing_attempts == 1
    claim = collection.claim_calls[0]
    assert claim["update"]["$inc"] == {"processing_attempts": 1}
    extraction_update = collection.update_calls[1]
    assert extraction_update["filters"] == {
        "work_key": "douyin:work-1",
        "status.status": "extracting",
        "processing_attempts": 1,
    }
    assert extraction_update["update"]["$set"]["status"]["status"] == (
        "pending_analysis"
    )
    analysis_update = collection.update_calls[2]
    assert analysis_update["filters"]["processing_attempts"] == 2
    assert analysis_update["update"]["$set"]["status"]["status"] == "finished"


def test_claim_prioritizes_latest_top_five_then_oldest_publish_time() -> None:
    """验证作品队列先筛选最近评分 Top 5，并按发布时间从旧到新领取。"""

    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]
    ranking_collection = database["creator_opinion_analyses"]
    ranking_collection.find_rows = [
        {
            "_id": "creator-1",
            "accuracy_score": 90,
            "verified_opinions": [{"opinion_id": "opinion-1"}],
        }
    ]
    work_collection = database["creator_works"]
    claimed = build_work().model_dump(mode="python")
    claimed["status"] = {"status": "extracting", "reason": None}
    claimed["processing_attempts"] = 1
    claimed["processing_started_at"] = NOW
    work_collection.claim_result = claimed

    result = asyncio.run(
        repository.claim_next_for_extraction(
            lease_timeout_seconds=1800,
            now=NOW,
        )
    )

    assert result is not None
    assert len(work_collection.claim_calls) == 1
    claim = work_collection.claim_calls[0]
    assert {"creator_id": {"$in": ["creator-1"]}} in claim["filters"]["$and"]
    assert claim["sort"] == [("published_at", 1), ("work_key", 1)]


def test_opinion_query_uses_published_and_availability_windows() -> None:
    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]
    collection = database["creator_works"]
    collection.find_rows = [
        {"analysis": {"opinions": [build_opinion().model_dump(mode="python")]}}
    ]
    end = NOW + timedelta(days=1)
    available = end + timedelta(hours=9)

    opinions = asyncio.run(
        repository.list_opinions_by_published_window(
            creator_id="creator-1",
            start_at=NOW,
            end_at=end,
            available_at=available,
        )
    )

    assert [item.opinion_id for item in opinions] == ["douyin:work-1:1"]
    filters = collection.find_calls[0]["filters"]
    assert filters["published_at"] == {"$gte": NOW, "$lt": end}
    assert filters["first_seen_at"] == {"$lte": available}
    assert filters["analysis.analyzed_at"] == {"$lte": available}


def test_finished_work_query_applies_limit_and_availability_cutoff() -> None:
    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]
    collection = database["creator_works"]
    finished = build_work(
        extracted_text="转写正文",
        status={"status": "finished"},
        analysis=build_analysis(),
    )
    collection.find_rows = [finished.model_dump(mode="python")]
    end = NOW + timedelta(days=1)
    available = end + timedelta(hours=9)

    works = asyncio.run(
        repository.list_finished_works_by_published_window(
            creator_id="creator-1",
            start_at=NOW,
            end_at=end,
            available_at=available,
            limit=1,
        )
    )

    assert [item.work_key for item in works] == ["douyin:work-1"]
    assert collection.last_cursor is not None
    assert collection.last_cursor.limit_value == 1
    call = collection.find_calls[0]
    assert call["filters"] == {
        "creator_id": "creator-1",
        "status.status": "finished",
        "published_at": {"$gte": NOW, "$lt": end},
        "first_seen_at": {"$lte": available},
        "analysis.analyzed_at": {"$lte": available},
    }
    assert call["sort"] == [("published_at", -1), ("work_key", 1)]


def test_find_latest_finished_before_uses_cutoff_and_newest_sort() -> None:
    database = FakeDatabase()
    repository = CreatorWorkRepository(database=database)  # type: ignore[arg-type]
    collection = database["creator_works"]
    finished = build_work(
        extracted_text="转写正文",
        status={"status": "finished"},
        analysis=build_analysis(),
    )
    collection.find_one_result = finished.model_dump(mode="python")
    end = NOW + timedelta(days=1)
    available = end + timedelta(hours=9)

    latest = asyncio.run(
        repository.find_latest_finished_before(
            creator_id="creator-1",
            end_at=end,
            available_at=available,
        )
    )

    assert latest is not None
    assert latest.work_key == "douyin:work-1"
    assert collection.find_calls == [
        {
            "filters": {
                "creator_id": "creator-1",
                "status.status": "finished",
                "published_at": {"$lt": end},
                "first_seen_at": {"$lte": available},
                "analysis.analyzed_at": {"$lte": available},
            },
            "projection": {"_id": 0},
            "sort": [("published_at", -1), ("work_key", 1)],
        }
    ]
