from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.crawlers.douyin_creator_crawler import DouyinCreatorCrawler
from app.llm.douyin_creator_analysis_llm import DouyinCreatorAnalysisLLMAnalyzer
from app.models.douyin_creator_work import (
    DouyinTranscript,
    DouyinWorkAnalysis,
)
from app.repositories.douyin_creator_work_repository import (
    DouyinCreatorWorkRepository,
)
from app.services.douyin_asr_service import FasterWhisperTranscriber


logger = logging.getLogger(__name__)
# 单个作品处理租约固定为 30 分钟；超时后其他 worker 才能接管未完成任务。
DOUYIN_PROCESSING_LEASE_SECONDS = 30 * 60
# 转写或 LLM 分析失败后固定等待 60 秒再允许队列重新领取该作品。
DOUYIN_RETRY_DELAY_SECONDS = 60


class MediaProvider(Protocol):
    """定义按作品 ID 下载本地媒体文件的最小接口。"""

    async def download_media(self, work_id: str) -> Path:
        """下载作品媒体并返回由调用方负责清理的本地文件路径。"""
        ...


class Transcriber(Protocol):
    """定义把本地媒体转换为结构化抖音转写的接口。"""

    def transcribe(self, media_path: str | Path) -> DouyinTranscript:
        """同步识别本地媒体，返回正文、分段和识别元数据。"""
        ...


class CreatorAnalyzer(Protocol):
    """定义从单个作品转写中提取结构化博主观点的异步接口。"""

    async def analyze(
        self,
        *,
        work_id: str,
        description: str,
        transcript: str,
        published_at: datetime,
    ) -> DouyinWorkAnalysis:
        """结合作品标识、标题、转写和发布时间生成可持久化分析结果。"""
        ...


@dataclass
class DouyinCreatorProcessResult:
    """描述一次领取和处理尝试是否执行、是否成功以及停止阶段。"""

    # 是否实际领取到作品；False 表示当前队列为空。
    processed: bool = False
    # 已领取作品是否完整完成转写和 LLM 分析。
    success: bool = False
    # finished、transcription、analysis 或 lease_lost 等处理阶段。
    stage: str | None = None


@dataclass
class DouyinCreatorBatchResult:
    """聚合一个 worker 批次内所有单作品处理结果及派生计数。"""

    # 按实际处理顺序保存的单作品结果；未领取到作品不会加入列表。
    results: list[DouyinCreatorProcessResult] = field(default_factory=list)

    @property
    def total_claimed_count(self) -> int:
        """返回本批次实际领取并尝试处理的作品数量。"""
        return len(self.results)

    @property
    def success_count(self) -> int:
        """返回本批次完整完成转写与分析的作品数量。"""
        return sum(1 for item in self.results if item.success)

    @property
    def failed_count(self) -> int:
        """返回已领取但在任一处理阶段失败或丢失租约的作品数量。"""
        return sum(1 for item in self.results if item.processed and not item.success)


class DouyinCreatorAnalysisService:
    """
    编排抖音作品从媒体下载、转写到 LLM 观点分析的完整处理链路。

    每次先从仓储原子领取一个带 attempt 的租约，阶段写回均校验该 attempt，
    防止旧 worker 的迟到结果覆盖新任务。临时媒体在转写后立即清理，失败状态
    则记录原因并按照配置延迟重试。
    """

    def __init__(
        self,
        *,
        repository: DouyinCreatorWorkRepository | None = None,
        media_provider: MediaProvider | None = None,
        transcriber: Transcriber | None = None,
        analyzer: CreatorAnalyzer | None = None,
        processing_lease_seconds: int | None = None,
        retry_delay_seconds: int | None = None,
    ) -> None:
        """
        初始化仓储、媒体下载器、转写器、分析器和重试时间参数。

        所有依赖均允许显式注入用于测试；未注入时依据模块固定值构造生产实现。
        租约时长决定异常 worker 多久后可被接管，重试延迟决定失败任务何时再次
        进入可领取状态。
        """
        # 作品状态、租约、转写和分析结果的持久化仓储。
        self.repository = repository or DouyinCreatorWorkRepository()
        # 负责下载公开视频到临时文件的媒体提供器。
        self.media_provider = media_provider or DouyinCreatorCrawler()
        # 负责本地 ASR 和可选字幕 OCR 的同步转写器。
        self.transcriber = transcriber or FasterWhisperTranscriber()
        # 负责把转写文本提取为市场摘要和行业观点的 LLM 分析器。
        self.analyzer = analyzer or DouyinCreatorAnalysisLLMAnalyzer()
        # 单次 worker 处理租约的秒数，超时后其他 worker 可以重新领取。
        self.processing_lease_seconds = (
            DOUYIN_PROCESSING_LEASE_SECONDS
            if processing_lease_seconds is None
            else processing_lease_seconds
        )
        if self.processing_lease_seconds <= 0:
            raise ValueError("processing_lease_seconds 必须大于 0")
        # 转写或分析失败后再次允许领取前的等待秒数。
        self.retry_delay_seconds = (
            DOUYIN_RETRY_DELAY_SECONDS
            if retry_delay_seconds is None
            else retry_delay_seconds
        )
        if self.retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds 必须大于 0")

    async def ensure_indexes(self) -> None:
        """确保作品唯一键、状态查询和处理租约所需的 MongoDB 索引存在。"""
        await self.repository.create_indexes()

    async def process_once(self) -> DouyinCreatorProcessResult:
        """
        原子领取并处理一个作品，返回本次处理状态。

        已有转写的重试任务会跳过媒体下载和 ASR；新作品先下载、在线程中转写并
        推进到分析状态，再调用 LLM 保存最终结果。每次状态迁移校验 attempt，
        修改数不为一时返回 lease_lost。下载的临时文件在转写阶段结束后始终清理。
        """
        work = await self.repository.claim_next_for_processing(
            lease_timeout_seconds=self.processing_lease_seconds
        )
        if work is None:
            return DouyinCreatorProcessResult()

        media_path: Path | None = None
        try:
            transcript = work.transcript
            if transcript is None:
                media_path = await self.media_provider.download_media(work.work_id)
                transcript = await asyncio.to_thread(
                    self.transcriber.transcribe, media_path
                )
            transition = await self.repository.mark_transcription_success(
                work.work_id,
                transcript,
                expected_attempt=work.processing_attempts,
            )
            if int(transition.modified_count) != 1:
                return self._lease_lost_result(work.work_id)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.exception("douyin transcription failed work_id=%s", work.work_id)
            await self.repository.mark_transcription_failed(
                work.work_id,
                error,
                expected_attempt=work.processing_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            return DouyinCreatorProcessResult(
                processed=True,
                success=False,
                stage="transcription",
            )
        finally:
            if media_path is not None:
                try:
                    media_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "failed to remove douyin temporary media work_id=%s path=%s",
                        work.work_id,
                        media_path,
                        exc_info=True,
                    )

        try:
            analysis = await self.analyzer.analyze(
                work_id=work.work_id,
                description=work.description,
                transcript=transcript.text,
                published_at=work.published_at,
            )
            transition = await self.repository.mark_analysis_success(
                work.work_id,
                analysis,
                expected_attempt=work.processing_attempts,
            )
            if int(transition.modified_count) != 1:
                return self._lease_lost_result(work.work_id)
            return DouyinCreatorProcessResult(
                processed=True,
                success=True,
                stage="finished",
            )
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.exception("douyin creator analysis failed work_id=%s", work.work_id)
            await self.repository.mark_analysis_failed(
                work.work_id,
                error,
                expected_attempt=work.processing_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            return DouyinCreatorProcessResult(
                processed=True,
                success=False,
                stage="analysis",
            )

    @staticmethod
    def _lease_lost_result(work_id: str) -> DouyinCreatorProcessResult:
        """记录 attempt 条件写入失败，并构造统一的租约丢失结果。"""
        logger.warning("douyin processing lease lost work_id=%s", work_id)
        return DouyinCreatorProcessResult(
            processed=True,
            success=False,
            stage="lease_lost",
        )

    async def process_batch(self, *, batch_size: int) -> DouyinCreatorBatchResult:
        """
        顺序处理最多 `batch_size` 个作品并汇总结果。

        队列首次返回未领取结果时立即停止当前批次，避免空轮询；单作品失败已经
        被 `process_once` 转换为结果，因此不会阻断批次内后续作品。
        """
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        results: list[DouyinCreatorProcessResult] = []
        for _ in range(batch_size):
            result = await self.process_once()
            if not result.processed:
                break
            results.append(result)
        return DouyinCreatorBatchResult(results=results)
