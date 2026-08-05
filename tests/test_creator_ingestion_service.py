from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.crawlers.creator_platforms.base import (
    CrawlPage,
    PlatformAccount,
    PlatformFetchedWork,
    PlatformWorkCandidate,
)
from app.models.creator_monitoring import CN_TZ
from app.services.creator_ingestion_service import CreatorIngestionService


REFERENCE = datetime(2026, 7, 24, 12, tzinfo=CN_TZ)


def account(platform: str = "weibo") -> PlatformAccount:
    """构造一条不依赖数据库账号表的启用博主配置。"""

    return PlatformAccount(
        rank=1,
        creator_id="creator-1",
        display_name="测试博主",
        platform=platform,
        platform_account_id="account-1",
        platform_id_type=f"{platform}_id",
        homepage_url="https://example.com/account",
        sec_uid="sec" if platform == "douyin" else "",
        seed_work_id="123" if platform == "douyin" else "",
    )


class FakeCrawler:
    """返回单条固定作品的最小平台抓取器替身。"""

    def __init__(
        self,
        *,
        content_type: str = "short_post",
        text: str = "看好半导体",
        coverage: str = "complete",
        detail_error: bool = False,
    ) -> None:
        """保存内容形态、列表覆盖状态和详情失败开关。"""

        self.content_type = content_type
        self.text = text
        self.coverage = coverage
        self.detail_error = detail_error
        self.closed = False

    async def list_works(self, target, *, cursor=None, limit=20):
        """返回窗口内的一条作品摘要，并模拟平台覆盖质量。"""

        candidate = PlatformWorkCandidate(
            platform=target.platform,
            platform_work_id="work-1",
            author_platform_id=target.platform_account_id,
            title="作品",
            published_at=REFERENCE - timedelta(hours=2),
            canonical_url="https://example.com/work-1",
            content_type=self.content_type,
        )
        return CrawlPage(
            account_key=target.account_key,
            platform=target.platform,
            items=[candidate],
            coverage=self.coverage,
            coverage_reason=("访客列表不完整" if self.coverage == "partial" else ""),
        )

    async def fetch_work(self, target, platform_work_id):
        """返回作品详情，或按测试开关模拟详情请求失败。"""

        if self.detail_error:
            raise RuntimeError("detail unavailable")
        return PlatformFetchedWork(
            platform=target.platform,
            platform_work_id=platform_work_id,
            author_platform_id=target.platform_account_id,
            author_name=target.display_name,
            title="作品",
            published_at=REFERENCE - timedelta(hours=2),
            canonical_url=f"https://example.com/{platform_work_id}",
            content_type=self.content_type,
            text=self.text,
            media_urls=["https://example.com/media.mp4"],
            fetched_at=REFERENCE,
        )

    async def aclose(self):
        """记录采集服务已经释放平台客户端。"""

        self.closed = True


class FakeWorkRepository:
    """只记录作品索引创建和新增作品的内存仓储替身。"""

    def __init__(self) -> None:
        """初始化空作品列表和索引调用次数。"""

        self.rows = []
        self.index_calls = 0
        self.latest_published_at = None

    async def create_indexes(self):
        """记录唯一生产集合的索引初始化。"""

        self.index_calls += 1

    async def get_existing_work_keys(self, _keys):
        """声明所有候选作品均为首次发现。"""

        return set()

    async def save_works(self, rows):
        """保存新作品并返回采集服务需要的数量统计。"""

        self.rows.extend(rows)
        return SimpleNamespace(inserted_count=len(rows), existing_count=0)

    async def get_latest_published_at(self, _account_id):
        return self.latest_published_at


class FakeCheckpointRepository:
    """记录检查点索引和采集尝试，不连接真实 MongoDB。"""

    def __init__(self) -> None:
        """初始化空检查点列表和索引调用次数。"""

        self.rows = []
        self.index_calls = 0

    async def create_indexes(self):
        """记录轻量检查点集合索引初始化。"""

        self.index_calls += 1

    async def record_attempt(self, checkpoint):
        """按调用顺序保存采集服务生成的检查点模型。"""

        self.rows.append(checkpoint)
        return SimpleNamespace(modified_count=1)


def build_service(crawler: FakeCrawler):
    """组装只注入作品仓储和平台抓取器的新采集服务。"""

    works = FakeWorkRepository()
    service = CreatorIngestionService(
        work_repository=works,
        crawler_factory=lambda _platform: crawler,
        accounts=(account(),),
    )
    return service, works, None


class PaginatedCrawler(FakeCrawler):
    """按游标返回预置页面，用于验证无状态连续翻页。"""

    def __init__(self, pages) -> None:
        """保存游标到页面或异常的映射，并初始化调用记录。"""

        super().__init__()
        self.pages = pages
        self.list_cursors = []

    async def list_works(self, target, *, cursor=None, limit=20):
        """记录请求游标并返回对应预置结果。"""

        self.list_cursors.append(cursor)
        result = self.pages[cursor]
        if isinstance(result, Exception):
            raise result
        return result


def crawl_page(
    *,
    cursor: str | None,
    next_cursor: str | None = None,
    published_at: datetime | None = None,
    coverage: str = "complete",
) -> CrawlPage:
    """构造带可选作品和下一页游标的平台列表页。"""

    items = []
    if published_at is not None:
        work_id = f"work-{cursor or 'head'}"
        items.append(
            PlatformWorkCandidate(
                platform="weibo",
                platform_work_id=work_id,
                author_platform_id="account-1",
                title="作品",
                published_at=published_at,
                canonical_url=f"https://example.com/{work_id}",
                content_type="short_post",
            )
        )
    return CrawlPage(
        account_key="weibo:account-1",
        platform="weibo",
        items=items,
        coverage=coverage,
        coverage_reason=("访客列表不完整" if coverage == "partial" else ""),
        cursor=cursor,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


def test_service_initializes_only_work_indexes() -> None:
    """验证采集服务只初始化作品业务集合。"""

    service, works, _ = build_service(FakeCrawler())

    asyncio.run(service.ensure_indexes())

    assert works.index_calls == 1
    assert set(vars(service)) == {
        "work_repository",
        "crawler_factory",
        "accounts",
    }


def test_text_work_skips_media_extraction_and_enters_analysis_queue() -> None:
    """验证有正文的文字作品可以直接进入 LLM 1 队列。"""

    crawler = FakeCrawler()
    service, works, _ = build_service(crawler)

    result = asyncio.run(
        service.ingest_account(
            account(),
            reference_datetime=REFERENCE,
            lookback_hours=24,
        )
    )

    assert result.status == "completed"
    assert works.rows[0].status.status == "pending_analysis"
    assert works.rows[0].extracted_text == "看好半导体"
    assert crawler.closed is True


def test_ingest_all_returns_coverage_without_extra_collection() -> None:
    """验证全账号批次通过返回值报告覆盖情况，不创建检查点表。"""

    service, _, _ = build_service(FakeCrawler())

    result = asyncio.run(
        service.ingest_all(
            reference_datetime=REFERENCE,
            lookback_hours=24,
        )
    )

    assert len(result.results) == 1
    assert result.results[0].account_key == account().account_key
    assert result.results[0].coverage_completed is True
    assert result.results[0].window_start == REFERENCE - timedelta(hours=24)
    assert result.results[0].window_end == REFERENCE


def test_video_and_image_always_enter_content_extraction() -> None:
    """验证视频和图片即使带描述也必须先执行 ASR 或 OCR。"""

    for content_type, source_text in (("video", "视频简介"), ("image_post", "图文正文")):
        service, works, _ = build_service(
            FakeCrawler(content_type=content_type, text=source_text)
        )
        asyncio.run(
            service.ingest_account(
                account(),
                reference_datetime=REFERENCE,
                lookback_hours=24,
            )
        )

        assert works.rows[0].status.status == "pending_extraction"
        assert works.rows[0].source_text == source_text
        assert works.rows[0].extracted_text == ""


def test_partial_listing_and_detail_failure_never_claim_complete_coverage() -> None:
    """验证列表不完整或详情失败都会通过返回值暴露，且不会声称完整覆盖。"""

    partial_service, _, _ = build_service(FakeCrawler(coverage="partial"))
    partial = asyncio.run(
        partial_service.ingest_account(
            account(), reference_datetime=REFERENCE, lookback_hours=24
        )
    )
    failed_service, _, _ = build_service(FakeCrawler(detail_error=True))
    failed = asyncio.run(
        failed_service.ingest_account(
            account(), reference_datetime=REFERENCE, lookback_hours=24
        )
    )

    assert partial.status == "partial"
    assert partial.coverage_completed is False
    assert failed.status == "partial"
    assert failed.detail_failed_count == 1
    assert failed.coverage_completed is False


def test_douyin_stale_success_page_is_reported_as_blocked() -> None:
    """抖音旧页不能再伪装成成功但不完整的正常采集。"""

    crawler = FakeCrawler(coverage="partial")
    service, works, _ = build_service(crawler)
    works.latest_published_at = REFERENCE - timedelta(hours=1)

    result = asyncio.run(
        service.ingest_account(
            account("douyin"),
            reference_datetime=REFERENCE,
            lookback_hours=24,
        )
    )

    assert result.status == "blocked"
    assert result.coverage_completed is False
    assert "陈旧成功响应" in (result.error or "")


def test_each_run_scans_contiguous_pages_from_current_head() -> None:
    """验证无状态采集从最新页连续翻页，抵达窗口起点后结束。"""

    crawler = PaginatedCrawler(
        {
            None: crawl_page(
                cursor=None,
                next_cursor="2",
                published_at=REFERENCE - timedelta(hours=1),
            ),
            "2": crawl_page(
                cursor="2",
                next_cursor="3",
                published_at=REFERENCE - timedelta(hours=24),
            ),
        }
    )
    service, _, _ = build_service(crawler)

    result = asyncio.run(
        service.ingest_account(
            account(),
            reference_datetime=REFERENCE,
            lookback_hours=24,
            max_pages=1,
        )
    )

    assert crawler.list_cursors == [None, "2"]
    assert result.status == "completed"
    assert result.coverage_completed is True


def test_failed_second_page_is_reported_without_persisted_cursor() -> None:
    """验证翻页异常直接形成失败结果，下轮会重新从最新页开始。"""

    crawler = PaginatedCrawler(
        {
            None: crawl_page(cursor=None, next_cursor="2"),
            "2": RuntimeError("page unavailable"),
        }
    )
    service, _, _ = build_service(crawler)

    result = asyncio.run(
        service.ingest_account(
            account(),
            reference_datetime=REFERENCE,
            lookback_hours=24,
            max_pages=1,
        )
    )

    assert crawler.list_cursors == [None, "2"]
    assert result.status == "failed"
    assert result.coverage_completed is False
