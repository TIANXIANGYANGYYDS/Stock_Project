from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.llm.news_sector_judge_llm import NewsSectorJudgeLLMAnalyzer
from app.models import News, NewsSectorLLMAnalysis
from app.repositories import NewsRepository


logger = logging.getLogger(__name__)


class NewsSectorAnalyzer(Protocol):
    """
    板块判断分析器协议。

    这里用 Protocol 而不是直接写死 NewsSectorJudgeLLMAnalyzer，是为了方便测试
    注入 fake analyzer，也方便以后替换成其他 LLM 实现。只要对象实现 analyze
    方法并返回 list[NewsSectorLLMAnalysis]，就可以被 service 使用。
    """

    async def analyze(
        self,
        *,
        title: str,
        content: str,
        publish_time: str,
    ) -> list[NewsSectorLLMAnalysis]:
        """
        分析单条新闻直接涉及的行业板块。

        title/content/publish_time 都由 News 模型拆出来传入，返回值会直接写回
        News.sector_llm_analysis。
        """

        ...


@dataclass
class NewsSectorJudgeProcessResult:
    """
    单次 process_once 的处理结果。

    这个结果用于 worker 打日志、统计成功失败数量，也方便测试验证 service 行为。
    """

    # 被领取并处理的新闻 event_id；没有待处理新闻时为 None。
    event_id: str | None = None

    # 是否真的领取到新闻并执行了处理；没有待处理新闻时为 False。
    processed: bool = False

    # 本次处理是否成功写入 sector_llm_analysis 并推进到 sector_judged。
    success: bool = False

    # LLM 返回并被写入的板块数量；失败或未处理时为 0。
    sector_count: int = 0

    # 失败时记录错误信息；成功或未处理时为 None。
    error_message: str | None = None


@dataclass
class NewsSectorJudgeBatchResult:
    """
    一批板块判断处理的汇总结果。

    process_batch 会连续调用 process_once，直到达到 batch_size 或没有待处理新闻。
    results 只保存实际处理过的新闻；空闲时 results 为空。
    """

    # 本批次每条实际处理新闻的结果列表。
    results: list[NewsSectorJudgeProcessResult] = field(default_factory=list)

    @property
    def total_claimed_count(self) -> int:
        """
        本批次实际领取并处理的新闻数量。

        这个数量等于 results 长度，不包含最后一次发现无待处理新闻的空结果。
        """

        return len(self.results)

    @property
    def success_count(self) -> int:
        """
        本批次成功完成板块判断的新闻数量。
        """

        return sum(1 for item in self.results if item.success)

    @property
    def failed_count(self) -> int:
        """
        本批次处理失败的新闻数量。

        只统计已经领取到新闻但处理失败的情况，不把空闲无任务算作失败。
        """

        return sum(1 for item in self.results if item.processed and not item.success)


class NewsSectorJudgeService:
    """
    新闻板块判断消费服务。

    职责边界：
    1. 从 MongoDB 原子领取 crawled 新闻
    2. 调用 NewsSectorJudgeLLMAnalyzer 完成板块判断
    3. 将结果写回 sector_llm_analysis
    4. 推进状态到 sector_judged，失败时标记 sector_judge_failed

    不负责：
    1. 长循环
    2. 信号处理
    3. 进程生命周期
    4. 定时调度
    """

    def __init__(
        self,
        news_repository: NewsRepository | None = None,
        analyzer: NewsSectorAnalyzer | None = None,
    ) -> None:
        """
        初始化板块判断 service。

        news_repository：
            新闻集合读写入口。生产环境默认使用 NewsRepository；测试时可以注入 fake。

        analyzer：
            LLM 板块判断器。生产环境默认使用 NewsSectorJudgeLLMAnalyzer；测试或
            后续切模型时可以注入替代实现。
        """

        # 负责 claim 新闻、写入 LLM 结果、推进状态。
        self.news_repository = news_repository or NewsRepository()

        # 负责纯 LLM 分析，不直接接触数据库。
        self.analyzer = analyzer or NewsSectorJudgeLLMAnalyzer()

    async def ensure_indexes(self) -> None:
        """
        确保新闻集合索引存在。

        worker 启动时调用一次即可。重复调用 create_index 是幂等的，MongoDB 会复用
        已有同名索引。
        """

        await self.news_repository.create_indexes()

    async def process_once(self) -> NewsSectorJudgeProcessResult:
        """
        处理一条新闻的完整板块判断闭环。

        流程：
        1. 从 repository 原子领取一条 crawled 新闻；
        2. 没有待处理新闻时返回 processed=False；
        3. 有新闻时调用 LLM 分析器；
        4. 成功则写入 sector_llm_analysis，并更新为 sector_judged；
        5. 失败则记录异常信息，并更新为 sector_judge_failed。

        这个方法不抛业务异常给 worker，失败会被转换成 result，避免常驻 worker
        因单条新闻失败而退出。
        """

        news = await self.news_repository.claim_next_sector_judge_news()

        if news is None:
            return NewsSectorJudgeProcessResult()

        try:
            analysis = await self._analyze_news(news)
            await self.news_repository.mark_sector_judge_success(
                news.event_id,
                analysis,
            )

            return NewsSectorJudgeProcessResult(
                event_id=news.event_id,
                processed=True,
                success=True,
                sector_count=len(analysis),
            )
        except Exception as exc:
            error_message = str(exc) or exc.__class__.__name__
            logger.exception(
                "news sector judge failed event_id=%s",
                news.event_id,
            )
            await self.news_repository.mark_sector_judge_failed(
                news.event_id,
                error_message,
            )

            return NewsSectorJudgeProcessResult(
                event_id=news.event_id,
                processed=True,
                success=False,
                error_message=error_message,
            )

    async def process_batch(self, *, batch_size: int = 10) -> NewsSectorJudgeBatchResult:
        """
        连续处理一批新闻。

        batch_size 控制单轮最多处理多少条，避免 worker 一次循环长期占用事件循环。
        如果中途发现没有待处理新闻，会提前结束本批次。
        """

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        results: list[NewsSectorJudgeProcessResult] = []

        for _ in range(batch_size):
            result = await self.process_once()

            if not result.processed:
                break

            results.append(result)

        return NewsSectorJudgeBatchResult(results=results)

    async def _analyze_news(self, news: News) -> list[NewsSectorLLMAnalysis]:
        """
        把 News 模型转换为 analyzer 需要的输入参数。

        这个方法只做参数适配，不做数据库读写。单独抽出来是为了让 process_once 的
        业务流程更清晰，也方便后续加入 prompt 版本、输入快照等扩展点。
        """

        return await self.analyzer.analyze(
            title=news.title,
            content=news.content,
            publish_time=news.publish_time or "",
        )
