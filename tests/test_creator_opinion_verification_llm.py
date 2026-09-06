from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.llm.base_llm import LLMResponseError
from app.llm.creator_opinion_verification_llm import (
    CreatorOpinionVerificationLLMAnalyzer,
)
from app.models.creator_monitoring import CreatorMarketEvidence, CreatorOpinion


UTC = timezone.utc
PUBLISHED_AT = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)


def verifier() -> CreatorOpinionVerificationLLMAnalyzer:
    """构造不访问真实接口的收盘观点验证器。"""

    return CreatorOpinionVerificationLLMAnalyzer(
        api_key="test",
        model="test-model",
        api_base_url="https://example.com/v1",
        extra_body={"enable_thinking": False},
    )


def opinion() -> CreatorOpinion:
    """构造一条需要使用次日行情验证的半导体观点。"""

    return CreatorOpinion(
        opinion_id="douyin:work-1:1",
        work_key="douyin:work-1",
        target_type="sector",
        target_name="半导体",
        direction="bullish",
        stance_score=70,
        claim="未来一日半导体相对沪深300走强",
        horizon="未来一日",
        valid_from=PUBLISHED_AT,
        valid_until=datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
        metric="相对沪深300收益",
        confidence=0.8,
        source_quote="半导体明天会更强",
    )


def evidence(
    *,
    market_date: str = "2026-07-24",
    as_of: datetime = datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
) -> CreatorMarketEvidence:
    """构造包含单一板块相对收益的临时收盘行情证据。"""

    return CreatorMarketEvidence(
        evidence_id="evidence-1",
        market_date=market_date,
        as_of=as_of,
        facts={"sector_relative_return": {"半导体": 2.4}},
        source="frozen-test-data",
        evidence_version="v1",
        generated_at=datetime(2026, 7, 24, 8, 1, tzinfo=UTC),
    )


def test_verify_uses_only_catalog_refs_and_programmatic_mainline_weight() -> None:
    """验证主线权重由程序计算，且结果只能引用冻结事实目录。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "is_market_mainline": False,
                    "reason": "半导体相对收益为正。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = asyncio.run(
        llm.verify(
            opinions=[opinion()],
            source_published_at=PUBLISHED_AT,
            evidence=evidence(),
            evaluation_date="2026-07-24",
            market_mainline_targets={"半导体"},
        )
    )

    assert result[0].is_market_mainline is True
    assert llm.extra_body["enable_search"] is True
    assert not hasattr(llm, "analyze")


def test_verify_accepts_same_day_source_published_before_close() -> None:
    """收盘前发布的当日作品应能在当日收盘验证。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "reason": "收盘事实支持观点。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )
    same_day_source = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
    same_day_opinion = opinion().model_copy(
        update={
            "valid_from": same_day_source,
            "valid_until": datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
        }
    )

    result = asyncio.run(
        llm.verify(
            opinions=[same_day_opinion],
            source_published_at=same_day_source,
            evidence=evidence(),
            evaluation_date="2026-07-24",
            source_window_start="2026-07-23",
        )
    )

    assert result[0].verdict == "corroborated"


def test_verify_accepts_auditable_web_evidence() -> None:
    """验证联网结果会保留 URL、标题和原文引用，并允许其支持理由数据。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "reason": "半导体板块收涨2.40%，观点成立。",
                    "evidence_refs": [],
                    "web_evidence": [
                        {
                            "url": "https://example.com/market-close",
                            "title": "7月24日半导体板块收盘数据",
                            "source": "测试行情网",
                            "published_at": "2026-07-24T15:30:00+08:00",
                            "quote": "半导体板块当日收涨2.40%。",
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = asyncio.run(
        llm.verify(
            opinions=[opinion()],
            source_published_at=PUBLISHED_AT,
            evidence=evidence(),
            evaluation_date="2026-07-24",
        )
    )

    assert result[0].web_evidence[0].url == "https://example.com/market-close"


def test_verify_rejects_web_evidence_published_after_as_of() -> None:
    """验证历史收盘核验不会使用知识截止时间之后发布的网页。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "reason": "网页称半导体上涨。",
                    "web_evidence": [
                        {
                            "url": "https://example.com/future",
                            "title": "未来报道",
                            "published_at": "2026-07-25T10:00:00+08:00",
                            "quote": "半导体上涨。",
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(LLMResponseError, match="发布时间"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(),
                evaluation_date="2026-07-24",
                schema_retries=0,
            )
        )


def test_verify_rejects_reference_outside_market_evidence() -> None:
    """验证收盘分析会拒绝模型引用行情证据以外的外部信息。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "is_market_mainline": False,
                    "reason": "引用了外部事实。",
                    "evidence_refs": ["internet.future_price"],
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(LLMResponseError, match="冻结快照之外"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(),
                evaluation_date="2026-07-24",
                schema_retries=0,
            )
        )


def test_verify_rejects_reason_percentage_without_selected_evidence() -> None:
    """验证理由中的具体涨跌幅必须由所选引用值或原观点直接支持。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "contradicted",
                    "is_market_mainline": False,
                    "reason": "板块下跌2.15%，与观点相反。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(LLMResponseError, match="百分比 2.15% 未被"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(),
                evaluation_date="2026-07-24",
                schema_retries=0,
            )
        )


def test_verify_removes_unsupported_percentage_after_schema_retry() -> None:
    """最后一次重试只移除无依据数字，不放宽冻结证据引用校验。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "contradicted",
                    "is_market_mainline": False,
                    "reason": "板块下跌2.15%，与观点相反。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = asyncio.run(
        llm.verify(
            opinions=[opinion()],
            source_published_at=PUBLISHED_AT,
            evidence=evidence(),
            evaluation_date="2026-07-24",
            schema_retries=1,
        )
    )

    assert "2.15%" not in result[0].reason
    assert "相应幅度" in result[0].reason
    assert result[0].evidence_refs == ["facts.sector_relative_return.半导体"]


def test_verify_rejects_reason_quarter_without_selected_evidence() -> None:
    """验证理由中的财报季度必须出现在所选证据或原观点中。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "is_market_mainline": False,
                    "reason": "Q2利润不及预期，因此观点成立。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(LLMResponseError, match="季度 Q2 未被"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(),
                evaluation_date="2026-07-24",
                schema_retries=0,
            )
        )


def test_verify_completes_reason_refs_from_same_market_evidence() -> None:
    """验证程序会从同一行情证据补齐模型漏选的百分比和季度引用。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "contradicted",
                    "is_market_mainline": False,
                    "reason": "纳指下跌2.15%，且Q2利润不及预期。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )
    enriched_evidence = evidence().model_copy(
        update={
            "facts": {
                "sector_relative_return": {"半导体": 2.4},
                "global_index": "纳斯达克下跌2.15%",
                "earnings": "第二季度利润不及预期",
            }
        }
    )

    result = asyncio.run(
        llm.verify(
            opinions=[opinion()],
            source_published_at=PUBLISHED_AT,
            evidence=enriched_evidence,
            evaluation_date="2026-07-24",
        )
    )

    assert result[0].evidence_refs == [
        "facts.sector_relative_return.半导体",
        "facts.global_index",
        "facts.earnings",
    ]


def test_verify_rejects_evidence_for_another_market_date() -> None:
    """验证交易日与行情证据日期不一致时不会调用 LLM。"""

    llm = verifier()

    with pytest.raises(ValueError, match="行情证据日期"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(market_date="2026-07-23"),
                evaluation_date="2026-07-24",
            )
        )


def test_verify_rejects_preclose_evidence() -> None:
    """验证盘中行情证据不能被包装成收盘验证结果。"""

    llm = verifier()
    preclose = evidence().model_copy(
        update={"as_of": datetime(2026, 7, 24, 6, 59, tzinfo=UTC)}
    )

    with pytest.raises(ValueError, match="收盘后"):
        asyncio.run(
            llm.verify(
                opinions=[opinion()],
                source_published_at=PUBLISHED_AT,
                evidence=preclose,
                evaluation_date="2026-07-24",
            )
        )


def test_verify_rejects_nonverifiable_opinion() -> None:
    """验证底层 LLM 边界也会拒绝不可验证评论。"""

    llm = verifier()
    item = opinion().model_copy(update={"verifiable": False})

    with pytest.raises(ValueError, match="不可验证"):
        asyncio.run(
            llm.verify(
                opinions=[item],
                source_published_at=PUBLISHED_AT,
                evidence=evidence(),
                evaluation_date="2026-07-24",
            )
        )


def test_verify_accepts_older_source_when_opinion_is_still_valid() -> None:
    """验证长周期观点不会因作品早于前一日而被来源窗口误删。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "reason": "半导体相对收益为正。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )

    result = asyncio.run(
        llm.verify(
            opinions=[opinion()],
            source_published_at=datetime(2026, 7, 22, 4, 0, tzinfo=UTC),
            evidence=evidence(),
            evaluation_date="2026-07-24",
        )
    )

    assert result[0].verdict == "corroborated"


def test_verify_accepts_weekend_source_for_monday_window() -> None:
    """验证底层收盘 LLM 接受显式周一窗口内的周末作品观点。"""

    llm = verifier()
    llm.chat = lambda **kwargs: json.dumps(  # type: ignore[method-assign]
        {
            "evaluations": [
                {
                    "opinion_id": "douyin:work-1:1",
                    "verdict": "corroborated",
                    "is_market_mainline": False,
                    "reason": "半导体相对收益为正。",
                    "evidence_refs": ["facts.sector_relative_return.半导体"],
                }
            ]
        },
        ensure_ascii=False,
    )
    published_at = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)
    weekend_opinion = opinion().model_copy(
        update={
            "valid_from": published_at,
            "valid_until": datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        }
    )

    result = asyncio.run(
        llm.verify(
            opinions=[weekend_opinion],
            source_published_at=published_at,
            evidence=evidence(
                market_date="2026-07-27",
                as_of=datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
            ),
            evaluation_date="2026-07-27",
            source_window_start="2026-07-24",
        )
    )

    assert len(result) == 1
    assert result[0].opinion_id == weekend_opinion.opinion_id
