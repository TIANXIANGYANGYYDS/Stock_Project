from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.crawlers.douyin_creator_crawler import DouyinWorkCandidate
from app.models.douyin_creator_work import FetchedDouyinWork
from app.repositories.douyin_creator_work_repository import DouyinWorkBatchWriteResult
from app.services.douyin_creator_ingestion_service import (
    DouyinCreatorIngestionService,
)


NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def build_work(work_id: str, *, publish_ts: int) -> FetchedDouyinWork:
    return FetchedDouyinWork(
        work_id=work_id,
        creator_sec_uid="creator-1",
        creator_name="测试博主",
        creator_short_id="short-1",
        description=f"作品 {work_id}",
        published_at=datetime.fromtimestamp(publish_ts, tz=timezone.utc),
        publish_ts=publish_ts,
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        duration_ms=10_000,
        first_seen_at=NOW,
        fetched_at=NOW,
    )


class FakeCrawler:
    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.rows_by_id = {row.work_id: row for row in self.rows}
        self.error = error
        self.calls = []

    async def fetch_candidates(
        self, *, cutoff_ts: int, lookback_hours: int, limit: int
    ):
        self.calls.append(
            {
                "cutoff_ts": cutoff_ts,
                "lookback_hours": lookback_hours,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        return [
            DouyinWorkCandidate(
                work_id=row.work_id,
                estimated_publish_ts=row.publish_ts,
            )
            for row in self.rows
        ]

    async def fetch_work(self, work_id: str):
        return self.rows_by_id[work_id]


class FakeRepository:
    def __init__(self, existing_ids=None) -> None:
        self.existing_ids = set(existing_ids or [])
        self.index_calls = 0
        self.existing_id_calls = []
        self.saved_rows = []

    async def create_indexes(self):
        self.index_calls += 1

    async def get_existing_work_ids(self, work_ids):
        self.existing_id_calls.append(list(work_ids))
        return self.existing_ids.intersection(work_ids)

    async def save_rows(self, rows):
        self.saved_rows.extend(rows)
        return DouyinWorkBatchWriteResult(
            inserted_count=len(rows),
            existing_count=0,
        )


def test_ingestion_deduplicates_skips_existing_and_reports_counts() -> None:
    crawler = FakeCrawler(
        [
            build_work("new", publish_ts=200),
            build_work("existing", publish_ts=190),
        ]
    )
    repository = FakeRepository(existing_ids={"existing"})
    service = DouyinCreatorIngestionService(
        crawler=crawler,
        repository=repository,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        service.ingest_latest_works(cutoff_ts=250, lookback_hours=96, limit=10)
    )

    assert crawler.calls == [{"cutoff_ts": 250, "lookback_hours": 96, "limit": 10}]
    assert repository.existing_id_calls == [["new", "existing"]]
    assert [row.work_id for row in repository.saved_rows] == ["new"]
    assert result.discovered_count == 2
    assert result.detail_failed_count == 0
    assert result.inserted_count == 1
    assert result.existing_count == 1


def test_ingestion_ensure_indexes_delegates_to_repository() -> None:
    repository = FakeRepository()
    service = DouyinCreatorIngestionService(
        crawler=FakeCrawler(),
        repository=repository,  # type: ignore[arg-type]
    )

    asyncio.run(service.ensure_indexes())

    assert repository.index_calls == 1


def test_ingestion_rejects_invalid_limit_before_crawling() -> None:
    crawler = FakeCrawler()
    service = DouyinCreatorIngestionService(
        crawler=crawler,
        repository=FakeRepository(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="limit"):
        asyncio.run(
            service.ingest_latest_works(
                cutoff_ts=250,
                lookback_hours=96,
                limit=0,
            )
        )

    assert crawler.calls == []


def test_ingestion_propagates_single_source_crawler_failure() -> None:
    service = DouyinCreatorIngestionService(
        crawler=FakeCrawler(error=RuntimeError("douyin blocked")),
        repository=FakeRepository(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="douyin blocked"):
        asyncio.run(
            service.ingest_latest_works(
                cutoff_ts=250,
                lookback_hours=96,
                limit=10,
            )
        )
