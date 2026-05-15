from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.crawlers import TonghuashunNewsCrawler,CLSNewsCrawler,Jin10NewsCrawler
from app.models import FetchedNews
from app.repositories import NewsRepository


logger = logging.getLogger(__name__)


@dataclass
class SourceIngestionResult:
    source: str
    fetched_count: int = 0
    error_message: Optional[str] = None


@dataclass
class NewsIngestionResult:
    total_fetched_count: int
    unique_count: int
    inserted_count: int
    existing_count: int
    duplicate_count: int = 0
    source_results: List[SourceIngestionResult] = field(default_factory=list)


@dataclass(frozen=True)
class _CrawlerSource:
    source: str
    fetcher: Callable[[], List[FetchedNews]]


@dataclass
class _CrawlerFetchResult:
    source: str
    rows: List[FetchedNews]
    error_message: Optional[str] = None


class NewsIngestionService:
    """
    新闻入库服务。

    职责边界：
    1. 分别执行一次多个新闻 crawler
    2. 汇总本轮抓取结果
    3. 基于 event_id 做本轮内存去重
    4. 将去重后的数据写入数据库
    5. 返回本次入库统计

    不负责：
    1. 定时调度
    2. 并发互斥
    3. 后续 LLM 分析
    4. 消息队列投递
    5. 通知逻辑
    """

    def __init__(
        self,
        news_repository: Optional[NewsRepository] = None,
        cls_crawler: Optional[CLSNewsCrawler] = None,
        jin10_crawler: Optional[Jin10NewsCrawler] = None,
        tonghuashun_crawler: Optional[TonghuashunNewsCrawler] = None,
    ):
        self.news_repository = news_repository or NewsRepository()
        self.cls_crawler = cls_crawler or CLSNewsCrawler()
        self.jin10_crawler = jin10_crawler or Jin10NewsCrawler()
        self.tonghuashun_crawler = tonghuashun_crawler or TonghuashunNewsCrawler()

    async def ensure_indexes(self) -> None:
        """
        初始化新闻表索引。

        建议在应用启动阶段调用，而不是每次 ingest_latest_news 时调用。
        """
        await self.news_repository.create_indexes()

    async def ingest_latest_news(self) -> NewsIngestionResult:
        """
        执行一次最新新闻抓取入库。
        """
        crawler_sources = self._build_crawler_sources()

        fetch_results = await asyncio.gather(
            *(self._fetch_once_from_source(source) for source in crawler_sources)
        )

        all_rows = [
            row
            for result in fetch_results
            for row in result.rows
        ]

        unique_rows = self._deduplicate_rows_by_event_id(all_rows)

        write_result = await self.news_repository.save_rows(unique_rows)

        source_results = [
            SourceIngestionResult(
                source=result.source,
                fetched_count=len(result.rows),
                error_message=result.error_message,
            )
            for result in fetch_results
        ]

        total_fetched_count = len(all_rows)
        unique_count = len(unique_rows)

        return NewsIngestionResult(
            total_fetched_count=total_fetched_count,
            unique_count=unique_count,
            duplicate_count=total_fetched_count - unique_count,
            inserted_count=write_result.inserted_count,
            existing_count=write_result.existing_count,
            source_results=source_results,
        )

    def _build_crawler_sources(self) -> List[_CrawlerSource]:
        return [
            _CrawlerSource(
                source="cls",
                fetcher=self.cls_crawler.fetch_latest_news,
            ),
            _CrawlerSource(
                source="jin10",
                fetcher=self.jin10_crawler.fetch_latest_telegraphs,
            ),
            _CrawlerSource(
                source="10jqka",
                fetcher=self.tonghuashun_crawler.fetch_latest_telegraphs,
            ),
        ]

    async def _fetch_once_from_source(
        self,
        crawler_source: _CrawlerSource,
    ) -> _CrawlerFetchResult:
        try:
            rows = await asyncio.to_thread(crawler_source.fetcher)

            return _CrawlerFetchResult(
                source=crawler_source.source,
                rows=rows,
            )

        except Exception as exc:
            logger.exception(
                "fetch latest news failed, source=%s",
                crawler_source.source,
            )

            return _CrawlerFetchResult(
                source=crawler_source.source,
                rows=[],
                error_message=str(exc),
            )

    def _deduplicate_rows_by_event_id(
        self,
        rows: List[FetchedNews],
    ) -> List[FetchedNews]:
        unique_rows: Dict[str, FetchedNews] = {}

        for row in rows:
            if row.event_id in unique_rows:
                continue

            unique_rows[row.event_id] = row

        return sorted(
            unique_rows.values(),
            key=lambda row: row.publish_ts or 0,
            reverse=True,
        )

if __name__ == "__main__":
    
    service = NewsIngestionService()
    result = asyncio.run(service.ingest_latest_news())

    print(result)