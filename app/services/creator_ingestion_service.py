from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Protocol

from app.crawlers.creator_platforms import (
    CREATOR_ACCOUNTS,
    CrawlPage,
    PlatformAccount,
    PlatformCrawler,
    PlatformFetchedWork,
    create_platform_crawler,
)
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorCrawlStatus,
    CreatorPlatform,
    CreatorWork,
    CreatorWorkStatus,
)
from app.repositories.creator_monitoring_repository import CreatorWorkRepository


logger = logging.getLogger(__name__)
# 每小时轮询覆盖最近两天；短暂停机也能补回，避免每轮反复翻历史长页。
DEFAULT_LOOKBACK_HOURS = 48
DEFAULT_PAGE_LIMIT = 20
DEFAULT_MAX_PAGES = 3
DEFAULT_CONCURRENCY = 1


class WorkRepository(Protocol):
    """定义采集所需的作品身份和仅插入式持久化操作。"""

    async def create_indexes(self) -> None:
        """创建稳定作品键和发布时间窗口索引。"""

        ...

    async def get_existing_work_keys(self, work_keys: list[str]) -> set[str]:
        """返回候选作品键中已经存在于持久化存储的部分。"""

        ...

    async def save_works(self, rows: list[CreatorWork]) -> Any:
        """插入未见过的规范化作品，不覆盖已有处理状态。"""

        ...

    async def get_latest_published_at(self, account_id: str) -> datetime | None:
        """返回该账号当前已入库作品的最新发布时间。"""

        ...


CrawlerFactory = Callable[[str], PlatformCrawler]


@dataclass(frozen=True)
class CreatorIngestionResult:
    """记录一个已配置平台账号的可观测采集结果。"""

    # 本次处理账号的稳定 ``platform:native_id`` 键。
    account_key: str
    # 跨平台聚合同一博主时使用的逻辑博主标识。
    creator_id: str
    # 本次处理账号所在的平台名称。
    platform: CreatorPlatform
    # 运行状态：completed、partial、blocked 或 failed。
    status: CreatorCrawlStatus
    # 发现的窗口内去重候选作品摘要数量。
    discovered_count: int
    # 之前未见过且已插入的作品数量。
    inserted_count: int
    # 已存储或被并发任务插入的候选作品数量。
    existing_count: int
    # 候选作品详情请求失败数量。
    detail_failed_count: int
    # 列表和详情是否共同证明完整覆盖请求窗口。
    coverage_completed: bool
    # 本次采集请求窗口的包含式起点。
    window_start: datetime
    # 本次采集请求窗口的不包含式终点。
    window_end: datetime
    # 本次账号采集开始时间。
    started_at: datetime
    # 本次账号采集结束并形成结果的时间。
    finished_at: datetime
    # 为诊断保留的上游覆盖或请求错误。
    error: str | None = None


@dataclass(frozen=True)
class CreatorIngestionBatchResult:
    """记录一次定时采集批次按账号顺序产生的结果。"""

    # 按配置顺序排列的所有启用账号结果。
    results: tuple[CreatorIngestionResult, ...]

    @property
    def inserted_count(self) -> int:
        """返回所有账号合计插入的新作品数量。"""

        return sum(item.inserted_count for item in self.results)

    @property
    def failed_account_count(self) -> int:
        """统计采集被阻断或完全失败的账号数量。"""

        return sum(item.status in {"failed", "blocked"} for item in self.results)

    @property
    def partial_account_count(self) -> int:
        """统计未能完整覆盖请求窗口的部分成功账号数量。"""

        return sum(item.status == "partial" for item in self.results)

    @property
    def detail_failed_count(self) -> int:
        """返回所有账号候选作品详情失败总数。"""

        return sum(item.detail_failed_count for item in self.results)


class CreatorIngestionService:
    """发现所有已配置博主账号的作品，并只持久化规范化作品。

    生产账号清单来自代码配置，每轮从最新页面开始按固定回看窗口扫描，再依靠
    ``work_key`` 幂等去重。抓取结果、提取正文和观点分析状态都保存在
    ``creator_works``，采集失败只写运行日志，不再创建额外运维集合。
    """

    def __init__(
        self,
        *,
        work_repository: WorkRepository | None = None,
        crawler_factory: CrawlerFactory | None = None,
        accounts: tuple[PlatformAccount, ...] = CREATOR_ACCOUNTS,
    ) -> None:
        """组装唯一作品仓储、平台抓取器工厂和不可变账号配置。"""

        # 规范化原始作品的仅插入式目标及已存在作品键查询。
        self.work_repository = work_repository or CreatorWorkRepository()
        # 为每个账号来源平台创建适配器的工厂。
        self.crawler_factory = crawler_factory or (
            lambda platform: create_platform_crawler(platform)  # type: ignore[arg-type]
        )
        # ``ingest_all`` 要处理的不可变账号定义。
        self.accounts = accounts

    async def ensure_indexes(self) -> None:
        """创建唯一作品键、发布时间和处理状态索引。"""

        await self.work_repository.create_indexes()

    async def ingest_all(
        self,
        *,
        reference_datetime: datetime | None = None,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = DEFAULT_MAX_PAGES,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> CreatorIngestionBatchResult:
        """按配置顺序串行采集所有启用账号，并返回本轮运行结果。

        所有账号共用可选参考时间以及窗口、分页设置。单个账号失败会体现在
        ``CreatorIngestionResult`` 中，不会中断其他账号；即使调用方传入更大的
        ``concurrency``，这里仍保持单账号串行，避免和服务器上的其他服务争用网络、
        CPU 及连接池。
        """

        if concurrency <= 0:
            raise ValueError("concurrency 必须大于 0")
        async def ingest(account: PlatformAccount) -> CreatorIngestionResult:
            """隔离一个账号的异常，并在日志中保留本轮诊断。"""

            started_at = datetime.now(CN_TZ)
            try:
                return await self.ingest_account(
                    account,
                    reference_datetime=reference_datetime,
                    lookback_hours=lookback_hours,
                    page_limit=page_limit,
                    max_pages=max_pages,
                )
            except Exception as exc:
                window_end = self._window_end(reference_datetime)
                result = CreatorIngestionResult(
                    account_key=account.account_key,
                    creator_id=account.creator_id,
                    platform=account.platform,
                    status="failed",
                    discovered_count=0,
                    inserted_count=0,
                    existing_count=0,
                    detail_failed_count=0,
                    coverage_completed=False,
                    window_start=window_end - timedelta(hours=lookback_hours),
                    window_end=window_end,
                    started_at=started_at,
                    finished_at=datetime.now(CN_TZ),
                    error=(str(exc) or exc.__class__.__name__)[:500],
                )
                logger.exception(
                    "creator ingestion account failed account=%s",
                    account.account_key,
                )
                return result

        results: list[CreatorIngestionResult] = []
        for account in self.accounts:
            if account.enabled:
                results.append(await ingest(account))
        return CreatorIngestionBatchResult(results=tuple(results))

    async def ingest_account(
        self,
        account: PlatformAccount,
        *,
        reference_datetime: datetime | None = None,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> CreatorIngestionResult:
        """在有界时间窗口内发现、补全并持久化一个账号的作品。

        最新页面始终会刷新；生产流程从该页开始在 ``max_pages`` 限制内向历史翻页，
        依靠作品键去重，不需要持久化游标。只有窗口内未见过的候选作品才请求详情；
        方法返回覆盖状态供日志与调度监控使用，并在返回前关闭平台适配器。
        """

        if lookback_hours <= 0:
            raise ValueError("lookback_hours 必须大于 0")
        if page_limit <= 0 or max_pages <= 0:
            raise ValueError("page_limit 和 max_pages 必须大于 0")
        window_end = self._window_end(reference_datetime)
        window_start = window_end - timedelta(hours=lookback_hours)
        started_at = datetime.now(CN_TZ)
        crawler = self.crawler_factory(account.platform)

        pages: list[CrawlPage] = []
        candidates: dict[str, Any] = {}
        try:
            head_page = await self._list_page(
                crawler,
                account,
                cursor=None,
                limit=page_limit,
            )
            if account.platform == "douyin":
                head_page = await self._reject_stale_douyin_page(
                    account,
                    head_page,
                )
            pages.append(head_page)
            self._add_candidates(candidates, head_page)

            head_reached_window_start = self._reached_window_start(
                head_page,
                window_start,
            )
            backlog_cursor = (
                head_page.next_cursor
                if head_page.coverage not in {"failed", "blocked"}
                and head_page.has_more
                and not head_reached_window_start
                else None
            )
            for _ in range(max_pages):
                if backlog_cursor is None:
                    break
                page = await self._list_page(
                    crawler,
                    account,
                    cursor=backlog_cursor,
                    limit=page_limit,
                )
                pages.append(page)
                self._add_candidates(candidates, page)
                if page.coverage in {"failed", "blocked"}:
                    break
                if not page.has_more or self._reached_window_start(
                    page,
                    window_start,
                ):
                    break
                backlog_cursor = page.next_cursor

            window_candidates = [
                item
                for item in candidates.values()
                if window_start
                <= item.published_at.astimezone(CN_TZ)
                < window_end
            ]
            existing_keys = await self.work_repository.get_existing_work_keys(
                [item.work_key for item in window_candidates]
            )
            rows: list[CreatorWork] = []
            detail_failed_count = 0
            for item in window_candidates:
                if item.work_key in existing_keys:
                    continue
                try:
                    fetched = await crawler.fetch_work(
                        account,
                        item.platform_work_id,
                    )
                    if not (
                        window_start
                        <= fetched.published_at.astimezone(CN_TZ)
                        < window_end
                    ):
                        continue
                    rows.append(self._to_work(account, fetched, first_seen_at=started_at))
                except Exception as exc:
                    detail_failed_count += 1
                    logger.warning(
                        "creator work detail fetch failed account=%s work=%s error=%s",
                        account.account_key,
                        item.work_key,
                        (str(exc) or exc.__class__.__name__)[:300],
                    )
            write_result = await self.work_repository.save_works(rows)
            inserted_count = int(getattr(write_result, "inserted_count", 0))
            write_existing_count = int(getattr(write_result, "existing_count", 0))
            status, coverage_completed = self._run_status(
                pages,
                window_start=window_start,
                detail_failed_count=detail_failed_count,
                listing_contiguous=True,
            )
            error = next(
                (
                    page.coverage_reason
                    for page in pages
                    if page.coverage in {"failed", "blocked"}
                ),
                None,
            ) or next(
                (
                    page.coverage_reason
                    for page in pages
                    if page.coverage_reason
                ),
                None,
            )
            logger.info(
                "creator ingestion account=%s status=%s discovered=%s inserted=%s "
                "detail_failed=%s coverage_completed=%s",
                account.account_key,
                status,
                len(window_candidates),
                inserted_count,
                detail_failed_count,
                coverage_completed,
            )
            return CreatorIngestionResult(
                account_key=account.account_key,
                creator_id=account.creator_id,
                platform=account.platform,
                status=status,
                discovered_count=len(window_candidates),
                inserted_count=inserted_count,
                existing_count=len(existing_keys) + write_existing_count,
                detail_failed_count=detail_failed_count,
                coverage_completed=coverage_completed,
                window_start=window_start,
                window_end=window_end,
                started_at=started_at,
                finished_at=datetime.now(CN_TZ),
                error=error,
            )
        finally:
            close = getattr(crawler, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    logger.warning(
                        "failed to close creator crawler account=%s",
                        account.account_key,
                        exc_info=True,
                    )

    async def _reject_stale_douyin_page(
        self,
        account: PlatformAccount,
        page: CrawlPage,
    ) -> CrawlPage:
        """拒绝最新候选时间早于库内水位的抖音降级旧页。"""

        if page.coverage in {"failed", "blocked"}:
            return page
        latest_stored = await self.work_repository.get_latest_published_at(
            account.account_key
        )
        latest_listed = max(
            (item.published_at for item in page.items),
            default=None,
        )
        if latest_stored is None:
            return page
        if latest_listed is None or latest_listed < latest_stored:
            listed_text = latest_listed.isoformat() if latest_listed else "empty"
            return page.model_copy(
                update={
                    "coverage": "blocked",
                    "coverage_reason": (
                        "抖音列表返回陈旧成功响应: "
                        f"listed_latest={listed_text}, "
                        f"stored_latest={latest_stored.isoformat()}"
                    ),
                }
            )
        return page

    @staticmethod
    def _window_end(reference_datetime: datetime | None) -> datetime:
        """返回带中国时区的采集窗口终点，并拒绝无时区历史参考时间。"""

        value = reference_datetime or datetime.now(CN_TZ)
        if value.tzinfo is None:
            raise ValueError("reference_datetime 必须包含时区")
        return value.astimezone(CN_TZ)

    @staticmethod
    async def _list_page(
        crawler: PlatformCrawler,
        account: PlatformAccount,
        *,
        cursor: str | None,
        limit: int,
    ) -> CrawlPage:
        """获取一个列表页，并将适配器异常转换为失败覆盖结果。

        返回结构化失败页面后，采集流程可以把明确状态写入日志和批次返回值，
        调度器下次运行时会从最新页重新扫描并依靠作品键去重。
        """

        try:
            return await crawler.list_works(
                account,
                cursor=cursor,
                limit=limit,
            )
        except Exception as exc:
            return CrawlPage(
                account_key=account.account_key,
                platform=account.platform,
                coverage="failed",
                coverage_reason=(str(exc) or exc.__class__.__name__)[:500],
                cursor=cursor,
            )

    @staticmethod
    def _add_candidates(candidates: dict[str, Any], page: CrawlPage) -> None:
        """按作品键添加页面摘要，不替换之前较新的同一作品记录。"""

        for item in page.items:
            candidates.setdefault(item.work_key, item)

    @staticmethod
    def _reached_window_start(page: CrawlPage, window_start: datetime) -> bool:
        """返回页面是否包含发布时间早于或等于窗口起点的作品。"""

        return any(
            item.published_at.astimezone(CN_TZ) <= window_start
            for item in page.items
        )

    @staticmethod
    def _run_status(
        pages: list[CrawlPage],
        *,
        window_start: datetime,
        detail_failed_count: int,
        listing_contiguous: bool,
    ) -> tuple[str, bool]:
        """根据列表与详情结果推导运行状态及完整覆盖证明。

        只要存在受阻或失败页面，运行状态就以其为准。完整覆盖还要求列表边界
        连续、每一页均报告完整覆盖、存在终止页或足够早的作品，并且详情请求
        不能失败；否则运行结果为 partial。
        """

        if not pages:
            return "failed", False
        failed_page = next(
            (
                page
                for page in pages
                if page.coverage in {"failed", "blocked"}
            ),
            None,
        )
        if failed_page is not None:
            return failed_page.coverage, False
        terminal = pages[-1]
        reached_window_start = any(
            item.published_at.astimezone(CN_TZ) <= window_start
            for page in pages
            for item in page.items
        )
        listing_complete = (
            listing_contiguous
            and all(page.coverage == "complete" for page in pages)
            and (not terminal.has_more or reached_window_start)
        )
        coverage_completed = listing_complete and detail_failed_count == 0
        return ("completed", True) if coverage_completed else ("partial", False)

    @staticmethod
    def _to_work(
        account: PlatformAccount,
        fetched: PlatformFetchedWork,
        *,
        first_seen_at: datetime,
    ) -> CreatorWork:
        """规范化一个平台作品，并选择其初始处理状态。

        视频和图片作品始终进入内容提取阶段。已有来源文本的文字作品可以直接
        进入单作品观点分析；没有文本的文字作品仍需尝试提取，以明确暴露缺失内容。
        """

        source_text = fetched.text.strip()
        needs_media_extraction = fetched.content_type in {"video", "image_post"}
        if needs_media_extraction or not source_text:
            status = CreatorWorkStatus(status="pending_extraction")
            extracted_text = ""
        else:
            status = CreatorWorkStatus(status="pending_analysis")
            extracted_text = source_text
        return CreatorWork(
            creator_id=account.creator_id,
            creator_name=fetched.author_name,
            account_id=account.account_key,
            platform=fetched.platform,
            platform_work_id=fetched.platform_work_id,
            content_type=fetched.content_type,
            title=fetched.title,
            canonical_url=fetched.canonical_url,
            published_at=fetched.published_at,
            first_seen_at=first_seen_at,
            fetched_at=fetched.fetched_at,
            duration_ms=fetched.duration_ms,
            media_url=(fetched.media_urls[0] if fetched.media_urls else None),
            source_text=source_text,
            extracted_text=extracted_text,
            status=status,
        )

__all__ = [
    "CreatorIngestionResult",
    "CreatorIngestionBatchResult",
    "CreatorIngestionService",
]
