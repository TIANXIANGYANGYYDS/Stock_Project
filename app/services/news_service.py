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
    """记录单个新闻来源在本轮抓取中的数量和错误摘要。"""

    # 新闻来源稳定标识，例如 cls、jin10 或 10jqka。
    source: str
    # 该来源本轮成功返回、尚未跨来源去重的新闻条数。
    fetched_count: int = 0
    # 抓取失败时的异常文本；成功时为 None。
    error_message: Optional[str] = None


@dataclass
class NewsIngestionResult:
    """汇总一次多来源新闻抓取、去重和数据库写入的统计结果。"""

    # 所有来源抓取条数之和，包含来源间重复事件。
    total_fetched_count: int
    # 按 event_id 做本轮内存去重后的事件数。
    unique_count: int
    # 本轮实际新写入数据库的事件数。
    inserted_count: int
    # 去重后已存在于数据库、未重复插入的事件数。
    existing_count: int
    # 本轮抓取结果中因 event_id 重复而移除的条数。
    duplicate_count: int = 0
    # 各来源的独立抓取统计与错误信息。
    source_results: List[SourceIngestionResult] = field(default_factory=list)


@dataclass(frozen=True)
class _CrawlerSource:
    """把来源标识与其同步抓取函数绑定为一个不可变执行项。"""

    # 用于日志和统计的新闻来源标识。
    source: str
    # 无参数同步抓取函数，返回该来源的标准化新闻列表。
    fetcher: Callable[[], List[FetchedNews]]


@dataclass
class _CrawlerFetchResult:
    """保存一个抓取函数的标准化行及可选错误，供主流程统一汇总。"""

    # 本结果所属的新闻来源标识。
    source: str
    # 抓取成功得到的标准化新闻；失败时为空列表。
    rows: List[FetchedNews]
    # 抓取异常文本；成功时为 None。
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
        """创建新闻入库服务并允许调用方替换仓储或任一来源爬虫。

        未注入的依赖使用生产默认实现；注入点主要供测试和独立运行场景隔离
        数据库与外部网络。构造过程本身不会创建索引或执行抓取。
        """

        # 负责新闻索引创建、幂等写入和数据库重复判断的仓储。
        self.news_repository = news_repository or NewsRepository()
        # 财联社最新新闻同步爬虫。
        self.cls_crawler = cls_crawler or CLSNewsCrawler()
        # 金十快讯同步爬虫。
        self.jin10_crawler = jin10_crawler or Jin10NewsCrawler()
        # 同花顺电报同步爬虫。
        self.tonghuashun_crawler = tonghuashun_crawler or TonghuashunNewsCrawler()

    async def ensure_indexes(self) -> None:
        """
        初始化新闻表索引。

        建议在应用启动阶段调用，而不是每次 ingest_latest_news 时调用。
        """
        await self.news_repository.create_indexes()

    async def ingest_latest_news(self) -> NewsIngestionResult:
        """并发抓取全部新闻源，对事件去重后批量写入仓储。

        单一来源失败会记录在对应来源结果中，不阻断其他来源；返回值汇总各来源抓取量、
        去重前后数量以及 MongoDB 新增、更新和跳过统计，供调度与监控调用方使用。
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
        """按固定来源顺序构造本轮要并发执行的抓取任务描述。"""

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

    async def _run_fetcher_in_thread(
        self,
        fetcher: Callable[[], List[FetchedNews]],
    ) -> List[FetchedNews]:
        """在线程池中运行同步爬虫，避免阻塞当前 asyncio 事件循环。"""

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fetcher)

    async def _fetch_once_from_source(
        self,
        crawler_source: _CrawlerSource,
    ) -> _CrawlerFetchResult:
        """执行一个来源的一次抓取，并把异常转换为可汇总的失败结果。

        单来源失败不会向外传播或取消其他来源任务；异常仍写入日志，并在结果中
        保留错误文本供调度日志和监控使用。
        """

        try:
            rows = await self._run_fetcher_in_thread(crawler_source.fetcher)

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
        """按 ``event_id`` 保留每个事件首次出现的行，并按发布时间倒序返回。

        ``publish_ts`` 缺失时按 0 排序，因此会自然落在有明确发布时间的新闻之后。
        """

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
