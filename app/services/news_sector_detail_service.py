from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.llm.news_sector_detail_llm import NewsSectorDetailLLMAnalyzer
from app.models import News, NewsSectorLLMAnalysis
from app.repositories import NewsRepository


logger = logging.getLogger(__name__)


class NewsSectorDetailAnalyzer(Protocol):
    """
    板块详情分析器协议。

    只约束 service 需要的 analyze 接口，便于测试注入 fake analyzer，也方便以后把
    NewsSectorDetailLLMAnalyzer 替换成其他 provider。
    """

    async def analyze(
        self,
        *,
        title: str,
        content: str,
        publish_time: str,
        sectors: Sequence[str | NewsSectorLLMAnalysis] | None,
    ) -> list[NewsSectorLLMAnalysis]:
        """
        分析单条新闻对已确定板块的短线影响详情。

        sectors 来自第一阶段板块判断结果，第二阶段不重新判断板块，只补充每个
        sector_name 对应的 sector_llm_analysis。
        """

        ...


@dataclass
class NewsSectorDetailProcessResult:
    """
    单次详情分析处理结果。

    worker 用这个对象做日志统计；测试也用它判断 service 是否按预期推进状态。
    """

    # 被领取并处理的新闻 event_id；没有待处理新闻时为 None。
    event_id: str | None = None

    # 是否真的领取到新闻并执行了详情分析。
    processed: bool = False

    # 本次详情分析是否成功写入结果并推进到 finished。
    success: bool = False

    # 本次写回的板块数量。
    sector_count: int = 0

    # 失败时记录错误信息；成功或未处理时为 None。
    error_message: str | None = None


@dataclass
class NewsSectorDetailBatchResult:
    """
    一批详情分析处理结果。

    results 只包含实际领取并处理过的新闻；如果当前没有待处理新闻，results 为空。
    """

    # 本批次每条实际处理新闻的结果列表。
    results: list[NewsSectorDetailProcessResult] = field(default_factory=list)

    @property
    def total_claimed_count(self) -> int:
        """
        本批次实际领取并处理的新闻数量。
        """

        return len(self.results)

    @property
    def success_count(self) -> int:
        """
        本批次成功完成详情分析的新闻数量。
        """

        return sum(1 for item in self.results if item.success)

    @property
    def failed_count(self) -> int:
        """
        本批次领取后处理失败的新闻数量。
        """

        return sum(1 for item in self.results if item.processed and not item.success)


class NewsSectorDetailService:
    """
    新闻板块详情分析消费服务。

    职责边界：
    1. 从 MongoDB 原子领取 sector_judged 新闻；
    2. 把第一阶段 sector_llm_analysis 传给 NewsSectorDetailLLMAnalyzer；
    3. 写回补齐后的 sector_llm_analysis；
    4. 成功推进到 finished，失败标记 sector_detail_failed。

    不负责长循环、进程生命周期和信号处理，这些由 worker 负责。
    """

    def __init__(
        self,
        news_repository: NewsRepository | None = None,
        analyzer: NewsSectorDetailAnalyzer | None = None,
    ) -> None:
        """
        初始化详情分析 service。

        news_repository 负责数据库状态流转；analyzer 负责纯 LLM 分析。两者都允许
        注入 fake 对象，方便单元测试不访问真实 MongoDB 和 LLM。
        """

        # 负责领取待详情分析新闻、写回结果、更新状态。
        self.news_repository = news_repository or NewsRepository()

        # 负责第二阶段 LLM 分析，不直接接触数据库。
        self.analyzer = analyzer or NewsSectorDetailLLMAnalyzer()

    async def ensure_indexes(self) -> None:
        """
        确保新闻集合索引存在。

        详情 worker 和判断 worker 使用同一张 news_data 表，因此复用同一套索引。
        """

        await self.news_repository.create_indexes()

    async def process_once(self) -> NewsSectorDetailProcessResult:
        """
        处理一条新闻的完整详情分析闭环。

        流程：
        1. 原子领取一条 sector_judged 新闻；
        2. 没有待处理新闻时返回 processed=False；
        3. 使用第一阶段的 sector_llm_analysis 作为待分析板块；
        4. 调用详情 LLM；
        5. 成功则写回详情并标记 finished；
        6. 失败则记录错误并标记 sector_detail_failed。
        """

        news = await self.news_repository.claim_next_sector_detail_news()

        if news is None:
            return NewsSectorDetailProcessResult()

        try:
            analysis = await self._analyze_news(news)
            await self.news_repository.mark_sector_detail_success(
                news.event_id,
                analysis,
            )

            return NewsSectorDetailProcessResult(
                event_id=news.event_id,
                processed=True,
                success=True,
                sector_count=len(analysis),
            )
        except Exception as exc:
            error_message = str(exc) or exc.__class__.__name__
            logger.exception(
                "news sector detail failed event_id=%s",
                news.event_id,
            )
            await self.news_repository.mark_sector_detail_failed(
                news.event_id,
                error_message,
            )

            return NewsSectorDetailProcessResult(
                event_id=news.event_id,
                processed=True,
                success=False,
                error_message=error_message,
            )

    async def process_batch(
        self,
        *,
        batch_size: int = 10,
    ) -> NewsSectorDetailBatchResult:
        """
        连续处理一批待详情分析新闻。

        到达 batch_size 或发现没有待处理新闻时结束本轮，避免 worker 单轮无限运行。
        """

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        results: list[NewsSectorDetailProcessResult] = []

        for _ in range(batch_size):
            result = await self.process_once()

            if not result.processed:
                break

            results.append(result)

        return NewsSectorDetailBatchResult(results=results)

    async def _analyze_news(self, news: News) -> list[NewsSectorLLMAnalysis]:
        """
        把 News 模型适配成详情 LLM 的输入。

        第一阶段结果保存在 news.sector_llm_analysis 中，里面的 sector_name 会被第二
        阶段原样传入；详情 LLM 只补充 score、reason、companies。
        """

        return await self.analyzer.analyze(
            title=news.title,
            content=news.content,
            publish_time=news.publish_time or "",
            sectors=news.sector_llm_analysis,
        )
