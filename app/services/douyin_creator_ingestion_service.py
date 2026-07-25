from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

from app.crawlers.douyin_creator_crawler import DouyinWorkCandidate
from app.models.douyin_creator_work import FetchedDouyinWork
from app.repositories.douyin_creator_work_repository import (
    DouyinCreatorWorkRepository,
)


logger = logging.getLogger(__name__)


class DouyinWorkCrawler(Protocol):
    """定义发现候选作品并获取单作品详情的抓取接口。"""

    async def fetch_candidates(
        self,
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> list[DouyinWorkCandidate]:
        """返回截止时间和回看窗口内按发布时间排序的作品候选。"""
        ...

    async def fetch_work(self, work_id: str) -> FetchedDouyinWork:
        """获取并校验候选作品的完整领域详情。"""
        ...


@dataclass(frozen=True)
class DouyinCreatorIngestionResult:
    """记录一次抖音作品发现、详情抓取和幂等写入的统计结果。"""

    # 列表接口发现的候选作品总数。
    discovered_count: int
    # 本次新插入 MongoDB 的作品数。
    inserted_count: int
    # 已存在或 upsert 时未新建的作品数。
    existing_count: int
    # 详情页抓取失败、因此没有写入的候选数。
    detail_failed_count: int


class DouyinCreatorIngestionService:
    """
    抓取单一抖音博主的最新作品并幂等写入待处理集合。

    列表发现和详情抓取分两步执行；详情失败只计数并继续其他候选，已有作品
    通过 `work_id` 去重，避免周期性调度覆盖 worker 已经写入的处理状态。
    """

    def __init__(
        self,
        *,
        crawler: DouyinWorkCrawler | None = None,
        repository: DouyinCreatorWorkRepository | None = None,
    ) -> None:
        """初始化可注入的抓取器与作品仓储，未提供时使用生产默认实现。"""
        if crawler is None:
            from app.crawlers.douyin_creator_crawler import DouyinCreatorCrawler

            crawler = DouyinCreatorCrawler()
        # 负责发现候选作品和拉取完整详情的抓取器。
        self.crawler = crawler
        # 以 work_id 幂等保存待处理作品的仓储。
        self.repository = repository or DouyinCreatorWorkRepository()

    async def ensure_indexes(self) -> None:
        """创建作品抓取去重和 worker 状态处理所需的 MongoDB 索引。"""
        await self.repository.create_indexes()

    async def ingest_latest_works(
        self,
        *,
        cutoff_ts: int,
        lookback_hours: int,
        limit: int,
    ) -> DouyinCreatorIngestionResult:
        """
        发现截止时间前的作品、补全详情并批量写入数据库。

        `cutoff_ts` 只限制作品发布时间；抓取得到的详情会再次校验该条件。对
        7 月 24 日盘前链路而言，调度器通常按当前时间发现 7 月 23 日及更近作品，
        而最终是否能进入 09:00 盘前上下文还由仓储分别检查首次发现和分析完成时间。
        """
        if cutoff_ts < 0:
            raise ValueError("cutoff_ts 不能小于 0")
        if limit <= 0:
            raise ValueError("limit 必须大于 0")

        if lookback_hours <= 0:
            raise ValueError("lookback_hours 必须大于 0")

        candidates = await self.crawler.fetch_candidates(
            cutoff_ts=cutoff_ts,
            lookback_hours=lookback_hours,
            limit=limit,
        )
        existing_ids = await self.repository.get_existing_work_ids(
            [item.work_id for item in candidates]
        )
        new_rows: list[FetchedDouyinWork] = []
        detail_failed_count = 0
        for candidate in candidates:
            if candidate.work_id in existing_ids:
                continue
            try:
                row = await self.crawler.fetch_work(candidate.work_id)
            except Exception as exc:
                detail_failed_count += 1
                logger.warning(
                    "douyin work detail fetch failed work_id=%s error=%s",
                    candidate.work_id,
                    str(exc)[:300],
                )
                continue
            if row.publish_ts <= cutoff_ts:
                new_rows.append(row)
        write_result = await self.repository.save_rows(new_rows)

        return DouyinCreatorIngestionResult(
            discovered_count=len(candidates),
            inserted_count=write_result.inserted_count,
            existing_count=len(existing_ids) + write_result.existing_count,
            detail_failed_count=detail_failed_count,
        )
