from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinSectorOpinion,
    DouyinTranscript,
    DouyinWorkAnalysis,
    FetchedDouyinWork,
)
from app.repositories.douyin_creator_work_repository import (
    CN_TZ,
    DouyinCreatorWorkRepository,
)


NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def build_fetched(work_id: str = "work-1") -> FetchedDouyinWork:
    return FetchedDouyinWork(
        work_id=work_id,
        creator_sec_uid="creator-1",
        creator_name="测试博主",
        creator_short_id="short-1",
        description="原始作品文案",
        published_at=NOW,
        publish_ts=int(NOW.timestamp()),
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        duration_ms=30_000,
        first_seen_at=NOW,
        fetched_at=NOW,
    )


def build_transcript() -> DouyinTranscript:
    return DouyinTranscript(
        text="转写正文",
        provider="test-asr",
        model="test-asr-model",
        transcribed_at=NOW,
    )


def build_analysis() -> DouyinWorkAnalysis:
    return DouyinWorkAnalysis(
        summary="结构化摘要",
        sector_opinions=[
            DouyinSectorOpinion(
                opinion_id="work-1:1",
                sector_name="半导体",
                stance_score=50,
                reason="测试理由",
            )
        ],
        analysis_version="v1",
        analysis_model="test-llm",
        analyzed_at=NOW,
    )


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
        self.bulk_operations = []
        self.find_calls: list[dict[str, Any]] = []
        self.find_rows: list[dict[str, Any]] = []
        self.find_one_result: dict[str, Any] | None = None
        self.claim_result: dict[str, Any] | None = None
        self.claim_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    async def create_index(self, keys, **kwargs):
        self.index_calls.append((keys, kwargs))
        return kwargs.get("name", "index")

    async def bulk_write(self, operations, *, ordered):
        self.bulk_operations.extend(operations)
        return SimpleNamespace(upserted_count=len(operations))

    def find(self, filters, **kwargs):
        self.find_calls.append({"filters": filters, **kwargs})
        return FakeCursor(self.find_rows)

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


class FakeDatabase:
    def __init__(self, collection: FakeCollection) -> None:
        self.collection = collection
        self.requested_names: list[str] = []

    def __getitem__(self, name: str):
        self.requested_names.append(name)
        return self.collection


def build_repository() -> tuple[DouyinCreatorWorkRepository, FakeCollection]:
    collection = FakeCollection()
    repository = DouyinCreatorWorkRepository(
        database=FakeDatabase(collection)  # type: ignore[arg-type]
    )
    return repository, collection


def test_repository_collection_and_indexes() -> None:
    repository, collection = build_repository()

    asyncio.run(repository.create_indexes())

    assert repository.get_collection_name() == "douyin_creator_works"
    assert [options["name"] for _, options in collection.index_calls] == [
        "uk_work_id",
        "idx_status_publish_ts",
        "idx_creator_publish_ts",
        "idx_status_processing_lease",
    ]


def test_save_rows_uses_set_on_insert_and_preserves_future_analysis() -> None:
    repository, collection = build_repository()

    result = asyncio.run(repository.save_rows([build_fetched()]))

    assert result.inserted_count == 1
    operation = collection.bulk_operations[0]
    assert operation._filter == {"work_id": "work-1"}
    assert "$setOnInsert" in operation._doc
    document = operation._doc["$setOnInsert"]
    assert document["status"]["status"] == "pending_transcription"
    assert document["published_at_cn"] == "2026-07-24T16:30:00.000+08:00"
    assert document["first_seen_at_cn"] == "2026-07-24T16:30:00.000+08:00"
    assert document["fetched_at_cn"] == "2026-07-24T16:30:00.000+08:00"
    assert "$set" not in operation._doc


def test_get_existing_work_ids_only_returns_requested_ids() -> None:
    repository, collection = build_repository()
    collection.find_rows = [{"work_id": "work-1"}, {"work_id": "work-2"}]

    result = asyncio.run(
        repository.get_existing_work_ids(["work-1", "work-2", "work-1"])
    )

    assert result == {"work-1", "work-2"}
    assert collection.find_calls[0]["filters"] == {
        "work_id": {"$in": ["work-1", "work-2"]}
    }


def test_claim_next_for_processing_is_atomic() -> None:
    repository, collection = build_repository()
    claimed = DouyinCreatorWork(**build_fetched().model_dump(mode="python"))
    claimed.status.status = "transcribing"
    claimed.processing_attempts = 1
    claimed.processing_started_at = NOW
    collection.claim_result = claimed.model_dump(mode="python")

    result = asyncio.run(
        repository.claim_next_for_processing(
            lease_timeout_seconds=1800,
            now=NOW,
        )
    )

    assert result is not None
    assert result.status.status == "transcribing"
    call = collection.claim_calls[0]
    stale_before = NOW.replace(minute=0)
    status_filter, attempt_filter = call["filters"]["$and"]
    pending_filter, retry_filter, stale_filter = status_filter["$or"]
    assert pending_filter["status.status"] == "pending_transcription"
    assert retry_filter["status.status"]["$in"] == [
        "transcription_failed",
        "analysis_failed",
    ]
    assert retry_filter["$or"][-1] == {"next_retry_at": {"$lte": NOW}}
    assert stale_filter["status.status"]["$in"] == ["transcribing", "analyzing"]
    assert stale_filter["$or"][-1] == {"processing_started_at": {"$lte": stale_before}}
    assert attempt_filter["$or"] == [
        {"processing_attempts": {"$exists": False}},
        {"processing_attempts": {"$lt": 3}},
    ]
    assert call["update"]["$set"]["status"]["status"] == "transcribing"
    assert call["update"]["$set"]["processing_started_at"] == NOW
    assert call["update"]["$set"]["processing_started_at_cn"] == (
        "2026-07-24T16:30:00.000+08:00"
    )
    assert call["update"]["$inc"] == {"processing_attempts": 1}
    assert call["update"]["$unset"] == {
        "next_retry_at": "",
        "next_retry_at_cn": "",
    }
    assert call["sort"] == [("publish_ts", -1)]


def test_repository_enforces_transcription_and_analysis_state_transitions() -> None:
    repository, collection = build_repository()
    transcript = build_transcript()
    analysis = build_analysis()

    asyncio.run(
        repository.mark_transcription_success("work-1", transcript, expected_attempt=2)
    )
    asyncio.run(
        repository.mark_transcription_failed(
            "work-2", "ASR timeout", expected_attempt=2
        )
    )
    asyncio.run(
        repository.mark_analysis_success("work-1", analysis, expected_attempt=2)
    )
    asyncio.run(
        repository.mark_analysis_failed("work-3", "LLM timeout", expected_attempt=2)
    )

    transcription_success, transcription_failed, analysis_success, analysis_failed = (
        collection.update_calls
    )
    assert transcription_success["filters"] == {
        "work_id": "work-1",
        "status.status": "transcribing",
        "processing_attempts": 2,
    }
    assert transcription_success["update"]["$set"]["status"]["status"] == "analyzing"
    assert transcription_success["update"]["$set"]["transcript"]["text"] == "转写正文"
    assert transcription_success["update"]["$set"]["transcript"][
        "transcribed_at_cn"
    ] == "2026-07-24T16:30:00.000+08:00"
    assert transcription_success["update"]["$set"]["processing_started_at_cn"].endswith(
        "+08:00"
    )
    assert transcription_failed["update"]["$set"]["status"]["status"] == (
        "transcription_failed"
    )
    assert transcription_failed["update"]["$set"]["next_retry_at"] > datetime.now(
        timezone.utc
    )
    assert transcription_failed["update"]["$set"]["next_retry_at_cn"].endswith(
        "+08:00"
    )
    assert transcription_failed["update"]["$unset"] == {
        "processing_started_at": "",
        "processing_started_at_cn": "",
    }
    assert analysis_success["filters"] == {
        "work_id": "work-1",
        "status.status": "analyzing",
        "processing_attempts": 2,
    }
    assert analysis_success["update"]["$set"]["status"]["status"] == "finished"
    assert analysis_success["update"]["$set"]["analysis"]["summary"] == "结构化摘要"
    assert analysis_success["update"]["$set"]["analysis"]["analyzed_at_cn"] == (
        "2026-07-24T16:30:00.000+08:00"
    )
    assert analysis_failed["update"]["$set"]["status"]["status"] == "analysis_failed"
    assert analysis_failed["update"]["$set"]["next_retry_at"] > datetime.now(
        timezone.utc
    )
    assert analysis_failed["update"]["$set"]["next_retry_at_cn"].endswith(
        "+08:00"
    )


def test_restore_mongo_datetimes_returns_china_timezone() -> None:
    restored = DouyinCreatorWorkRepository._restore_mongo_timezones(
        {"published_at": datetime(2026, 7, 23, 13, 44)}
    )

    published_at = restored["published_at"]
    assert published_at.isoformat() == "2026-07-23T21:44:00+08:00"
    assert int(published_at.timestamp()) == int(
        datetime(2026, 7, 23, 13, 44, tzinfo=timezone.utc).timestamp()
    )


def test_claim_finalizes_exhausted_stale_processing() -> None:
    repository, collection = build_repository()

    result = asyncio.run(
        repository.claim_next_for_processing(
            lease_timeout_seconds=1800,
            now=NOW,
        )
    )

    assert result is None
    transcription_recovery, analysis_recovery = collection.update_calls
    assert transcription_recovery["filters"]["status.status"] == "transcribing"
    assert transcription_recovery["filters"]["processing_attempts"] == {"$gte": 3}
    assert transcription_recovery["update"]["$set"]["status"]["status"] == (
        "transcription_failed"
    )
    assert transcription_recovery["update"]["$unset"] == {
        "processing_started_at": "",
        "processing_started_at_cn": "",
    }
    assert analysis_recovery["filters"]["status.status"] == "analyzing"
    assert analysis_recovery["update"]["$set"]["status"]["status"] == (
        "analysis_failed"
    )


def test_finished_queries_apply_creator_cutoff_and_limit() -> None:
    repository, collection = build_repository()
    completed = DouyinCreatorWork(
        **build_fetched().model_dump(mode="python"),
        status={"status": "finished"},
        transcript=build_transcript(),
        analysis=build_analysis(),
    )
    collection.find_rows = [completed.model_dump(mode="python")]
    collection.find_one_result = completed.model_dump(mode="python")

    rows = asyncio.run(
        repository.list_finished_for_morning(
            creator_sec_uid="creator-1",
            start_ts=100,
            end_ts=200,
            available_at_ts=300,
            limit=3,
        )
    )
    latest = asyncio.run(
        repository.find_latest_finished_before(
            creator_sec_uid="creator-1",
            end_ts=200,
            available_at_ts=300,
        )
    )

    assert len(rows) == 1
    list_call = collection.find_calls[0]
    assert list_call["filters"] == {
        "creator_sec_uid": "creator-1",
        "status.status": "finished",
        "publish_ts": {"$gte": 100, "$lte": 200},
        "first_seen_at": {"$lte": datetime.fromtimestamp(300, tz=CN_TZ)},
        "analysis.analyzed_at": {"$lte": datetime.fromtimestamp(300, tz=CN_TZ)},
    }
    assert list_call["sort"] == [("publish_ts", -1)]
    assert latest is not None
    latest_call = collection.find_calls[1]
    assert latest_call["filters"]["publish_ts"] == {"$lte": 200}
    assert latest_call["filters"]["first_seen_at"] == {
        "$lte": datetime.fromtimestamp(300, tz=CN_TZ)
    }
    assert latest_call["filters"]["analysis.analyzed_at"] == {
        "$lte": datetime.fromtimestamp(300, tz=CN_TZ)
    }
    assert latest_call["sort"] == [("publish_ts", -1)]
