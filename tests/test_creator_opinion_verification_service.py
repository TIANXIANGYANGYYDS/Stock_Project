from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from app.models.creator_monitoring import (
    CreatorMarketEvidence,
    CreatorOpinion,
    CreatorOpinionVerification,
    CreatorWork,
    CreatorWorkAnalysis,
)
from app.services.creator_opinion_verification_service import (
    CreatorOpinionVerificationService,
)


UTC = timezone.utc
PUBLISHED_AT = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)


def finished_work() -> CreatorWork:
    """构造包含一条已分析观点的完成态作品。"""

    opinion = CreatorOpinion(
        opinion_id="douyin:work-1:1",
        work_key="douyin:work-1",
        target_type="sector",
        target_name="半导体",
        direction="bullish",
        stance_score=70,
        claim="半导体次日走强",
        horizon="次日",
        valid_from=PUBLISHED_AT,
        valid_until=datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
        metric="相对收益",
        confidence=0.8,
        source_quote="半导体次日走强",
    )
    analysis = CreatorWorkAnalysis(
        summary="看好半导体。",
        opinions=[opinion],
        analysis_version="v1",
        analysis_model="test",
        analyzed_at=PUBLISHED_AT,
    )
    return CreatorWork(
        creator_id="creator-1",
        account_id="douyin:account-1",
        platform="douyin",
        platform_work_id="work-1",
        content_type="video",
        canonical_url="https://example.com/work-1",
        published_at=PUBLISHED_AT,
        first_seen_at=PUBLISHED_AT,
        fetched_at=PUBLISHED_AT,
        extracted_text="半导体次日走强",
        status={"status": "finished"},
        analysis=analysis,
    )


def evidence(
    *,
    market_date: str = "2026-07-24",
    as_of: datetime = datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
) -> CreatorMarketEvidence:
    """构造用于次日收盘验证的临时行情证据。"""

    return CreatorMarketEvidence(
        evidence_id="evidence-1",
        market_date=market_date,
        as_of=as_of,
        facts={"return": 1.2},
        source="test",
        evidence_version="v1",
        generated_at=datetime(2026, 7, 24, 8, 1, tzinfo=UTC),
    )


class FakeVerifier:
    """根据传入观点返回固定收盘验证结果的 LLM 替身。"""

    def __init__(self) -> None:
        """初始化空的验证调用记录。"""

        self.calls = []

    async def verify(self, **kwargs):
        """使用调用参数物化一条可审计验证结果。"""

        self.calls.append(kwargs)
        item = kwargs["opinions"][0]
        return [
            CreatorOpinionVerification(
                opinion_id=item.opinion_id,
                verdict="corroborated",
                reason="行情支持",
                evidence_refs=["facts.return"],
            )
        ]


def test_verify_work_returns_transient_verifications() -> None:
    """验证独立收盘服务只返回临时结果，不接受单独持久化仓储。"""

    service = CreatorOpinionVerificationService(
        verifier=FakeVerifier(),  # type: ignore[arg-type]
    )

    result = asyncio.run(
        service.verify_work(
            work=finished_work(),
            evidence=evidence(),
            evaluation_date="2026-07-24",
        )
    )

    assert len(result) == 1
    assert result[0].opinion_id == "douyin:work-1:1"


def test_verify_work_rejects_unanalyzed_work() -> None:
    """验证收盘服务不会绕过内容分析阶段直接解释原始作品。"""

    work = finished_work().model_copy(
        update={"status": {"status": "pending_analysis"}, "analysis": None}
    )
    service = CreatorOpinionVerificationService(
        verifier=FakeVerifier(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="尚未完成观点分析"):
        asyncio.run(
            service.verify_work(
                work=work,
                evidence=evidence(),
                evaluation_date="2026-07-24",
            )
        )


def test_verify_work_skips_nonverifiable_opinion() -> None:
    """验证不可验证评论不会进入收盘验证 LLM。"""

    work = finished_work()
    assert work.analysis is not None
    opinion = work.analysis.opinions[0].model_copy(update={"verifiable": False})
    work = work.model_copy(
        update={
            "analysis": work.analysis.model_copy(update={"opinions": [opinion]})
        }
    )
    verifier = FakeVerifier()
    service = CreatorOpinionVerificationService(
        verifier=verifier,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        service.verify_work(
            work=work,
            evidence=evidence(),
            evaluation_date="2026-07-24",
        )
    )

    assert result == []
    assert verifier.calls == []


def test_verify_work_skips_opinion_due_after_evaluation_day() -> None:
    """验证未来到期观点不会被当天收盘行情提前判断。"""

    work = finished_work()
    assert work.analysis is not None
    opinion = work.analysis.opinions[0].model_copy(
        update={"valid_until": datetime(2026, 7, 31, 8, 0, tzinfo=UTC)}
    )
    work = work.model_copy(
        update={
            "analysis": work.analysis.model_copy(update={"opinions": [opinion]})
        }
    )
    verifier = FakeVerifier()
    service = CreatorOpinionVerificationService(
        verifier=verifier,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        service.verify_work(
            work=work,
            evidence=evidence(),
            evaluation_date="2026-07-24",
        )
    )

    assert result == []
    assert verifier.calls == []


def test_verify_work_rejects_work_not_published_on_previous_day() -> None:
    """验证默认兼容模式不会处理来源窗口之外发布的作品。"""

    work = finished_work().model_copy(
        update={"published_at": datetime(2026, 7, 22, 4, 0, tzinfo=UTC)}
    )
    service = CreatorOpinionVerificationService(
        verifier=FakeVerifier(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="来源窗口"):
        asyncio.run(
            service.verify_work(
                work=work,
                evidence=evidence(),
                evaluation_date="2026-07-24",
            )
        )


def test_verify_work_accepts_weekend_source_for_monday_window() -> None:
    """验证显式周一来源窗口允许周五至周日作品进入独立收盘验证。"""

    work = finished_work()
    assert work.analysis is not None
    published_at = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)
    opinion = work.analysis.opinions[0].model_copy(
        update={
            "valid_from": published_at,
            "valid_until": datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        }
    )
    work = work.model_copy(
        update={
            "published_at": published_at,
            "analysis": work.analysis.model_copy(update={"opinions": [opinion]}),
        }
    )
    verifier = FakeVerifier()
    service = CreatorOpinionVerificationService(
        verifier=verifier,  # type: ignore[arg-type]
    )

    result = asyncio.run(
        service.verify_work(
            work=work,
            evidence=evidence(
                market_date="2026-07-27",
                as_of=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
            ),
            evaluation_date="2026-07-27",
            source_window_start="2026-07-24",
        )
    )

    assert len(result) == 1
    assert verifier.calls[0]["source_window_start"] == date(2026, 7, 24)
