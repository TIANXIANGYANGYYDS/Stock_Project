from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    PlatformAccount,
    PlatformFetchedWork,
    PlatformWorkCandidate,
)
from app.manually_execute_script import probe_creator_platforms as probe_module


NOW = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)


def account() -> PlatformAccount:
    return PlatformAccount(
        rank=1,
        creator_id="creator-1",
        display_name="测试博主",
        platform="bilibili",
        platform_account_id="123",
        platform_id_type="bilibili_uid",
        homepage_url="https://example.com/account",
    )


def candidate(work_id: str) -> PlatformWorkCandidate:
    return PlatformWorkCandidate(
        platform="bilibili",
        platform_work_id=work_id,
        author_platform_id="123",
        title="作品",
        published_at=NOW,
        canonical_url=f"https://example.com/{work_id}",
        content_type="video",
    )


class FakeCrawler:
    def __init__(self, *, media_urls: list[str]) -> None:
        self.media_urls = media_urls
        self.cursors: list[str | None] = []
        self.closed = False

    async def list_works(self, target, *, cursor=None, limit=20):
        self.cursors.append(cursor)
        if cursor is None:
            return CrawlPage(
                account_key=target.account_key,
                platform=target.platform,
                items=[candidate("work-1")],
                coverage="partial",
                coverage_reason="公开搜索列表不保证完整",
                next_cursor="2",
                has_more=True,
            )
        return CrawlPage(
            account_key=target.account_key,
            platform=target.platform,
            items=[candidate("work-2")],
            coverage="partial",
            coverage_reason="公开搜索列表不保证完整",
            cursor=cursor,
        )

    async def fetch_work(self, target, platform_work_id):
        return PlatformFetchedWork(
            platform=target.platform,
            platform_work_id=platform_work_id,
            author_platform_id=target.platform_account_id,
            author_name=target.display_name,
            title="作品",
            text="视频简介",
            published_at=NOW,
            canonical_url=f"https://example.com/{platform_work_id}",
            content_type="video",
            media_urls=self.media_urls,
            fetched_at=NOW,
        )

    async def aclose(self):
        self.closed = True


def test_probe_checks_identity_media_and_next_page(monkeypatch) -> None:
    crawler = FakeCrawler(media_urls=["https://media.example/work.mp4"])
    monkeypatch.setattr(probe_module, "create_platform_crawler", lambda _platform: crawler)

    result = asyncio.run(
        probe_module.probe_account(
            account(),
            limit=5,
            fetch_detail=True,
            check_media_download=False,
            check_pagination=True,
            timeout_seconds=1,
            content_preview_chars=20,
        )
    )

    assert result["list_ok"] is True
    assert result["pagination_attempted"] is True
    assert result["pagination_ok"] is True
    assert result["pagination_overlap_count"] == 0
    assert result["detail_identity_ok"] is True
    assert result["media_resolve_ok"] is True
    assert result["detail_source"] == "list"
    assert result["detail_ready_ok"] is True
    assert result["detail_author_name"] == "测试博主"
    assert result["detail_title"] == "作品"
    assert result["detail_published_at"] == NOW.isoformat()
    assert result["content_preview"] == "视频简介"
    assert result["list_elapsed_ms"] is not None
    assert result["pagination_elapsed_ms"] is not None
    assert result["detail_elapsed_ms"] is not None
    assert result["total_elapsed_ms"] is not None
    assert result["pipeline_ready_ok"] is True
    assert crawler.cursors == [None, "2"]
    assert crawler.closed is True


def test_probe_does_not_treat_video_description_as_resolved_media(monkeypatch) -> None:
    crawler = FakeCrawler(media_urls=[])
    monkeypatch.setattr(probe_module, "create_platform_crawler", lambda _platform: crawler)

    result = asyncio.run(
        probe_module.probe_account(
            account(),
            limit=5,
            fetch_detail=True,
            check_media_download=False,
            check_pagination=False,
            timeout_seconds=1,
        )
    )

    assert result["detail_identity_ok"] is True
    assert result["detail_content_ok"] is True
    assert result["media_resolve_ok"] is False
    assert result["pipeline_ready_ok"] is False


def test_probe_checks_seed_detail_without_claiming_list_discovery(monkeypatch) -> None:
    crawler = FakeCrawler(media_urls=["https://media.example/work.mp4"])
    monkeypatch.setattr(probe_module, "create_platform_crawler", lambda _platform: crawler)

    async def failed_list(target, *, cursor=None, limit=20):
        return CrawlPage(
            account_key=target.account_key,
            platform=target.platform,
            coverage="failed",
            coverage_reason="list signature unavailable",
            cursor=cursor,
        )

    crawler.list_works = failed_list
    seeded = account().model_copy(update={"seed_work_id": "seed-work"})

    result = asyncio.run(
        probe_module.probe_account(
            seeded,
            limit=1,
            fetch_detail=True,
            check_media_download=False,
            check_pagination=False,
            timeout_seconds=1,
        )
    )

    assert result["list_ok"] is False
    assert result["detail_work_id"] == "seed-work"
    assert result["detail_source"] == "seed"
    assert result["detail_ready_ok"] is True
    assert result["pipeline_ready_ok"] is False
    assert crawler.closed is True


def test_run_probe_can_limit_real_requests_to_one_account_per_platform(monkeypatch) -> None:
    accounts = (
        account(),
        account().model_copy(update={"rank": 2, "creator_id": "creator-2"}),
        account().model_copy(
            update={
                "rank": 3,
                "creator_id": "creator-3",
                "platform": "weibo",
                "platform_account_id": "456",
            }
        ),
    )
    seen: list[str] = []

    async def fake_probe(target, **_kwargs):
        seen.append(target.account_key)
        return {"rank": target.rank}

    monkeypatch.setattr(probe_module, "get_enabled_accounts", lambda _platform=None: accounts)
    monkeypatch.setattr(probe_module, "probe_account", fake_probe)

    rows = asyncio.run(probe_module.run_probe(one_per_platform=True))

    assert seen == ["bilibili:123", "weibo:456"]
    assert rows == [{"rank": 1}, {"rank": 3}]


def test_safe_error_removes_url_query_and_fragment() -> None:
    error = RuntimeError(
        "request failed: https://feed.example/private.xml?token=secret#fragment"
    )

    text = probe_module._safe_error(error)

    assert text == "request failed: https://feed.example/private.xml"
    assert "secret" not in text
