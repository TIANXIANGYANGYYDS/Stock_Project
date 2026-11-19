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


def test_expired_claims_get_a_new_token_and_all_writes_require_it():
    from unittest.mock import AsyncMock

    async def run():
        repo = object.__new__(NewsRepository)
        repo.collection = AsyncMock()
        for stage, pending, working in (("judge", "crawled", "sector_judging"),
                                        ("detail", "sector_judged", "sector_detail_analyzing")):
            def claim(filters, update, **options):
                new_status = update["$set"]["status"]
                assert filters["$or"][0] == {"status.status": pending}
                expired = filters["$or"][1]
                assert expired["status.status"] == working
                assert expired["status.claim_expires_at"]["$lte"] < new_status["claim_expires_at"]
                return {"event_id": "test", "publish_ts": 1, "content": "正文", "source": "cls",
                        "status": new_status}

            repo.collection.find_one_and_update.side_effect = claim
            acquire = getattr(repo, f"claim_next_sector_{stage}_news")
            first, next_claim = await acquire(), await acquire()
            assert first.status.claim_id and first.status.claim_id != next_claim.status.claim_id
            for result, value in (("success", []), ("failed", "超时")):
                save = getattr(repo, f"mark_sector_{stage}_{result}")
                await save(first.event_id, value, claim_id=first.status.claim_id)
                filters, change = repo.collection.update_one.await_args.args
                assert filters["status.claim_id"] == first.status.claim_id
                assert filters["status.status"] == working
                assert "claim_id" not in change["$set"]["status"]

    asyncio.run(run())
