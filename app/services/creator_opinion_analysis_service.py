from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.llm.creator_content_analysis_llm import CreatorContentAnalysisLLMAnalyzer
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorOpinion,
    CreatorWork,
    CreatorWorkAnalysis,
)
from app.repositories.creator_monitoring_repository import (
    CreatorOpinionAnalysisRepository,
    CreatorWorkRepository,
)
from app.services.trading_calendar_service import next_a_share_trade_date


logger = logging.getLogger(__name__)


class CreatorWorkAnalysisRepository(Protocol):
    """定义作品内容分析队列领取和状态迁移所需的尝试围栏接口。"""

    async def claim_next_for_analysis(
        self, *, lease_timeout_seconds: int
    ) -> CreatorWork | None:
        """领取一条已提取作品，并递增其内容分析尝试次数。"""

        ...

    async def mark_analysis_success(
        self,
        work_key: str,
        analysis: CreatorWorkAnalysis,
        *,
        expected_attempt: int,
    ) -> Any:
        """仅当 ``expected_attempt`` 仍持有租约时，写入成功分析结果。"""

        ...

    async def mark_analysis_failed(
        self,
        work_key: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int,
    ) -> Any:
        """为匹配的处理尝试记录可重试的内容分析失败状态。"""

        ...


class CreatorOpinionWriter(Protocol):
    """定义分析成功后同步博主待验证观点所需的最小接口。"""

    async def create_indexes(self) -> None: ...

    async def sync_work_opinions(self, work: CreatorWork) -> Any: ...


@dataclass(frozen=True)
class CreatorOpinionProcessResult:
    """表示一次领取并分析博主作品操作的标准化结果。"""

    # 是否成功领取到作品；为假表示内容分析队列暂时为空。
    processed: bool
    # 已领取作品是否成功进入完成状态。
    success: bool
    # 终止阶段标识，例如 ``finished``、``analysis`` 或 ``lease_lost``。
    stage: str
    # 已领取作品的稳定键；队列为空时不提供。
    work_key: str | None = None
    # 失败时保留的限长异常或租约丢失说明。
    reason: str | None = None


@dataclass(frozen=True)
class CreatorOpinionBatchResult:
    """表示一次限量轮询批次内按处理顺序排列的内容分析结果。"""

    # 已领取作品的结果；末尾用于确认空队列的探测结果不会包含在内。
    results: list[CreatorOpinionProcessResult] = field(default_factory=list)

    @property
    def total_claimed_count(self) -> int:
        """返回本批次实际领取到的作品数量。"""

        return len(self.results)

    @property
    def success_count(self) -> int:
        """返回本批次内成功完成内容分析的作品数量。"""

        return sum(item.success for item in self.results)

    @property
    def failed_count(self) -> int:
        """返回本批次内领取成功但未完成内容分析的作品数量。"""

        return sum(item.processed and not item.success for item in self.results)


class CreatorOpinionAnalysisService:
    """处理单条博主作品内容，并持久化其结构化观点分析结果。

    本服务只领取已完成文本或媒体提取的作品，并调用内容分析 LLM 生成观点。收盘
    后的行情验证由独立的 ``CreatorOpinionVerificationService`` 执行，二者不共享
    LLM 实例、提示词或仓储职责。
    """

    def __init__(
        self,
        *,
        repository: CreatorWorkAnalysisRepository | None = None,
        opinion_repository: CreatorOpinionWriter | None = None,
        analyzer: CreatorContentAnalysisLLMAnalyzer | None = None,
        lease_timeout_seconds: int = 30 * 60,
        retry_delay_seconds: int = 5 * 60,
    ) -> None:
        """绑定作品仓储、内容分析器及失败重试时间参数。

        ``repository`` 负责作品状态迁移；观点仓储只维护每位博主的待验证列表。
        """

        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds 必须大于 0")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能小于 0")
        # 使用尝试次数围栏保护的作品仓储，负责领取和分析状态迁移。
        self.repository = repository or CreatorWorkRepository()
        self.opinion_repository = opinion_repository or CreatorOpinionAnalysisRepository()
        # 可复用的单作品内容分析 LLM；未传入时在首次调用时创建并缓存。
        self.analyzer = analyzer
        # 被遗弃的分析任务在该秒数后可以由其他 worker 重新领取。
        self.lease_timeout_seconds = lease_timeout_seconds
        # 分析失败后再次允许领取该作品前需等待的秒数。
        self.retry_delay_seconds = retry_delay_seconds

    async def ensure_indexes(self) -> None:
        """在 LLM 1 worker 启动前创建作品和观点汇总索引。

        两个仓储都绑定在两张生产集合上，不创建额外的处理集合。
        """

        create_indexes = getattr(self.repository, "create_indexes", None)
        if callable(create_indexes):
            await create_indexes()
        await self.opinion_repository.create_indexes()

    async def analyze_work(self, work: CreatorWork) -> CreatorWorkAnalysis:
        """分析一条已提取作品，但不改变仓储中的状态机数据。

        平台、标题、原文、规范正文、ASR 与 OCR 上下文都会传给带 Schema 校验的
        内容分析器，由分析器为每条观点分配稳定标识。本方法不读取或请求收盘行情。
        """

        if self.analyzer is None:
            self.analyzer = CreatorContentAnalysisLLMAnalyzer()
        analyzer = self.analyzer
        analysis = await analyzer.analyze(
            work_key=work.work_key,
            published_at=work.published_at,
            title=work.title,
            source_text=work.source_text,
            extracted_text=work.extracted_text,
            asr_text=work.asr_text,
            ocr_text=work.ocr_text,
            platform=work.platform,
            content_type=work.content_type,
        )
        return self._normalize_verification_dates(analysis)

    @staticmethod
    def _normalize_verification_dates(
        analysis: CreatorWorkAnalysis,
    ) -> CreatorWorkAnalysis:
        """把周末或休市日到期观点顺延到下一个 A 股收盘日。"""

        opinions: list[CreatorOpinion] = []
        for opinion in analysis.opinions:
            if not opinion.verifiable or opinion.valid_until is None:
                opinions.append(opinion)
                continue
            active_until = opinion.valid_until.astimezone(CN_TZ)
            trade_date = next_a_share_trade_date(active_until.date())
            if trade_date == active_until.date():
                opinions.append(opinion)
                continue
            shifted_until = datetime.combine(
                trade_date,
                active_until.timetz(),
            )
            values = opinion.model_dump(mode="python")
            values.update(valid_until=shifted_until, verification_date=None)
            opinions.append(CreatorOpinion(**values))
        return analysis.model_copy(update={"opinions": opinions})

    async def process_once(self) -> CreatorOpinionProcessResult:
        """领取并分析一条作品，同时保留基于尝试次数的状态迁移围栏。

        成功结果只会写入当前领取到的那次处理尝试。异常会转换为可重试失败状态；若
        更新行数为零，则记录租约丢失，避免过期 worker 覆盖后来者的处理结果。
        """

        work = await self.repository.claim_next_for_analysis(
            lease_timeout_seconds=self.lease_timeout_seconds,
        )
        if work is None:
            return CreatorOpinionProcessResult(
                processed=False,
                success=True,
                stage="empty",
            )
        try:
            analysis = await self.analyze_work(work)
            completed_work = CreatorWork(
                **{
                    **work.model_dump(mode="python"),
                    "status": {"status": "finished"},
                    "analysis": analysis,
                }
            )
            # 第二张表也是 LLM 1 成功结果的一部分；先同步，失败时作品才能继续重试。
            await self.opinion_repository.sync_work_opinions(completed_work)
            transition = await self.repository.mark_analysis_success(
                work.work_key,
                analysis,
                expected_attempt=work.processing_attempts,
            )
            if not self._was_modified(transition):
                return self._lease_lost(work.work_key)
            return CreatorOpinionProcessResult(
                processed=True,
                success=True,
                stage="finished",
                work_key=work.work_key,
            )
        except Exception as exc:
            reason = str(exc)[:1000] or type(exc).__name__
            logger.exception("creator opinion analysis failed work_key=%s", work.work_key)
            transition = await self.repository.mark_analysis_failed(
                work.work_key,
                reason,
                expected_attempt=work.processing_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if not self._was_modified(transition):
                return self._lease_lost(work.work_key)
            return CreatorOpinionProcessResult(
                processed=True,
                success=False,
                stage="analysis",
                work_key=work.work_key,
                reason=reason,
            )

    async def process_batch(self, *, batch_size: int) -> CreatorOpinionBatchResult:
        """最多处理 ``batch_size`` 条作品，并在队列为空时提前停止。"""

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        results: list[CreatorOpinionProcessResult] = []
        for _ in range(batch_size):
            result = await self.process_once()
            if not result.processed:
                break
            results.append(result)
        return CreatorOpinionBatchResult(results=results)

    @staticmethod
    def _was_modified(result: Any) -> bool:
        """解释更新结果元数据，同时兼容轻量级仓储测试替身。"""

        modified_count = getattr(result, "modified_count", None)
        return True if modified_count is None else bool(modified_count)

    @staticmethod
    def _lease_lost(work_key: str) -> CreatorOpinionProcessResult:
        """构造并记录内容分析状态迁移发生租约丢失时的标准结果。"""

        logger.warning("creator analysis lease lost work_key=%s", work_key)
        return CreatorOpinionProcessResult(
            processed=True,
            success=False,
            stage="lease_lost",
            work_key=work_key,
            reason="processing lease lost",
        )


__all__ = [
    "CreatorOpinionAnalysisService",
    "CreatorOpinionBatchResult",
    "CreatorOpinionProcessResult",
]
