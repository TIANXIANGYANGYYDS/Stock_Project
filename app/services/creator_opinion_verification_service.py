from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Collection

from app.llm.creator_opinion_verification_llm import (
    CreatorOpinionVerificationLLMAnalyzer,
)
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorMarketEvidence,
    CreatorOpinionVerification,
    CreatorWork,
)


class CreatorOpinionVerificationService:
    """在收盘后把已分析的博主观点与冻结行情事实进行独立验证。

    本服务不领取作品、不进行 OCR、ASR 或观点提取；它只读取完成内容分析的作品，
    调用收盘验证 LLM，并把临时结论返回给统一每日验证编排服务。
    """

    def __init__(
        self,
        *,
        verifier: CreatorOpinionVerificationLLMAnalyzer | None = None,
    ) -> None:
        """绑定可选的收盘验证 LLM 实例。

        LLM 2 独立于内容分析 LLM，可以单独补跑和重试；返回结果没有数据库身份，
        只有每日编排服务能把它连同原观点和分数写入统一文档。
        """

        # 可复用的收盘验证 LLM；未传入时在首次验证时创建并缓存。
        self.verifier = verifier

    async def verify_work(
        self,
        *,
        work: CreatorWork,
        evidence: CreatorMarketEvidence,
        evaluation_date: date | str,
        source_window_start: date | str | None = None,
        market_mainline_targets: Collection[str] = (),
    ) -> list[CreatorOpinionVerification]:
        """验证一条已完成作品中的到期观点并返回内存结果。

        ``evidence`` 必须是 ``evaluation_date`` 对应交易日收盘后构建的行情事实。
        ``source_window_start`` 指定本批次允许的最早发布日期；为空时默认仅允许
        前一自然日。若作品尚未完成内容分析则立即拒绝，避免验证器根据原始文本自行
        补造观点。
        """

        if work.analysis is None:
            raise ValueError("作品尚未完成观点分析")
        evaluation_day = self._date_value(evaluation_date)
        active_source_start = (
            evaluation_day - timedelta(days=1)
            if source_window_start is None
            else self._date_value(source_window_start)
        )
        if active_source_start > evaluation_day:
            raise ValueError("作品来源窗口起点不能晚于评价日")
        source_day = work.published_at.astimezone(CN_TZ).date()
        if not active_source_start <= source_day <= evaluation_day:
            raise ValueError("作品发布时间不在本次收盘验证来源窗口内")
        market_close = datetime.combine(evaluation_day, time(15), tzinfo=CN_TZ)
        eligible_opinions = [
            opinion
            for opinion in work.analysis.opinions
            if opinion.verifiable
            and opinion.valid_until is not None
            and opinion.valid_until.astimezone(CN_TZ).date() == evaluation_day
            and opinion.valid_from.astimezone(CN_TZ) <= market_close
            and opinion.valid_until.astimezone(CN_TZ) >= market_close
        ]
        if not eligible_opinions:
            return []
        if self.verifier is None:
            self.verifier = CreatorOpinionVerificationLLMAnalyzer()
        verifier = self.verifier
        evaluations = await verifier.verify(
            opinions=eligible_opinions,
            source_published_at=work.published_at,
            evidence=evidence,
            evaluation_date=evaluation_date,
            source_window_start=active_source_start,
            market_mainline_targets=market_mainline_targets,
        )
        return evaluations

    @staticmethod
    def _date_value(value: date | str) -> date:
        """把日期对象或 ISO 日期文本规范为收盘验证使用的日历日期。"""

        if isinstance(value, datetime):
            return value.astimezone(CN_TZ).date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip())


__all__ = [
    "CreatorOpinionVerificationService",
]
