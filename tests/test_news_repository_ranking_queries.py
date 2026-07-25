from __future__ import annotations

import asyncio

from app.models.crawlers.fetchednews import FetchedNews
from app.repositories.news_repository import NewsRepository


class FakeIndexCollection:
    def __init__(self) -> None:
        self.calls = []

    async def create_index(self, keys, **kwargs):
        self.calls.append((keys, kwargs))


def test_news_indexes_include_publish_time_for_ranking_window_stats() -> None:
    repository = object.__new__(NewsRepository)
    collection = FakeIndexCollection()
    repository.collection = collection

    asyncio.run(repository.create_indexes())

    assert ("publish_ts", {"name": "idx_publish_ts"}) in collection.calls


def test_ranking_query_reads_status_and_analysis_from_one_time_window() -> None:
    repository = object.__new__(NewsRepository)
    calls = []

    async def fake_find_many(filters, **kwargs):
        calls.append((filters, kwargs))
        return []

    repository.find_many = fake_find_many  # type: ignore[method-assign]

    asyncio.run(
        repository.list_news_for_ranking_window(start_ts=100, end_ts=200)
    )

    filters, options = calls[0]
    assert filters["publish_ts"] == {"$gte": 100, "$lte": 200}
    assert options["projection"]["status.status"] == 1
    assert options["projection"]["sector_llm_analysis"] == 1


def test_save_rows_recognizes_legacy_jin10_identity() -> None:
    repository = object.__new__(NewsRepository)
    written_rows = []

    async def fake_find_many(filters, **kwargs):
        assert filters == {
            "source": "jin10",
            "publish_ts": {"$in": [2_000_000]},
        }
        return [{"publish_ts": 2_000_000, "title": "同一条金十快讯"}]

    async def fake_upsert_many(rows):
        written_rows.extend(rows)
        return None

    repository.find_many = fake_find_many  # type: ignore[method-assign]
    repository.upsert_many = fake_upsert_many  # type: ignore[method-assign]
    row = FetchedNews(
        event_id="stable-detail-url-hash",
        publish_time="2026-07-24 09:00:00",
        publish_ts=2_000_000,
        title="同一条金十快讯",
        content="正文",
        source="jin10",
    )

    result = asyncio.run(repository.save_rows([row]))

    assert written_rows == []
    assert result.inserted_count == 0
    assert result.existing_count == 1
