from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models.creator_monitoring import (
    CreatorMarketEvidence,
    CreatorOpinion,
    CreatorOpinionVerification,
    CreatorWork,
    CreatorWorkAnalysis,
)


NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def build_opinion(**overrides) -> CreatorOpinion:
    values = {
        "opinion_id": "douyin:work-1:1",
        "work_key": "douyin:work-1",
        "target_type": "sector",
        "target_name": "半导体",
        "direction": "bullish",
        "stance_score": 60,
        "claim": "未来一周相对沪深300走强",
        "horizon": "未来5个交易日",
        "valid_from": NOW,
        "valid_until": NOW + timedelta(days=7),
        "metric": "相对沪深300超额收益",
        "conditions": ["成交量不低于20日均量"],
        "confidence": 0.7,
        "verifiable": True,
        "source_quote": "半导体未来一周还有机会。",
    }
    values.update(overrides)
    return CreatorOpinion(**values)


def build_work(**overrides) -> CreatorWork:
    values = {
        "creator_id": "creator-1",
        "account_id": "douyin:account-1",
        "platform": "douyin",
        "platform_work_id": "work-1",
        "content_type": "video",
        "canonical_url": "https://www.douyin.com/video/work-1",
        "published_at": NOW,
        "first_seen_at": NOW,
        "fetched_at": NOW,
        "media_url": "https://example.com/work-1.mp4",
    }
    values.update(overrides)
    return CreatorWork(**values)


def build_analysis() -> CreatorWorkAnalysis:
    return CreatorWorkAnalysis(
        summary="看好半导体未来一周表现。",
        opinions=[build_opinion()],
        analysis_version="v1",
        analysis_model="test-model",
        analyzed_at=NOW,
    )


def test_work_builds_stable_cross_platform_id() -> None:
    """验证作品键包含平台命名空间，并拒绝冲突的调用方输入。"""

    work = build_work()

    assert work.work_key == "douyin:work-1"

    with pytest.raises(ValidationError, match="work_key"):
        build_work(work_key="bilibili:work-1")


def test_text_work_can_enter_analysis_without_media_extraction() -> None:
    work = build_work(
        platform="sina_blog",
        account_id="sina_blog:1300871220",
        platform_work_id="article-1",
        content_type="article",
        canonical_url="https://blog.sina.com.cn/s/blog_article-1.html",
        media_url=None,
        source_text="原始文章正文",
        extracted_text="原始文章正文",
        status={"status": "pending_analysis"},
    )

    assert work.status.status == "pending_analysis"
    assert work.extracted_text == "原始文章正文"


def test_analysis_state_requires_extracted_text_and_finished_requires_analysis() -> None:
    with pytest.raises(ValidationError, match="extracted_text"):
        build_work(status={"status": "analyzing"})

    with pytest.raises(ValidationError, match="analysis"):
        build_work(
            status={"status": "finished"},
            extracted_text="转写正文",
        )

    finished = build_work(
        status={"status": "finished"},
        extracted_text="转写正文",
        analysis=build_analysis(),
    )
    assert finished.analysis is not None


def test_verifiable_opinion_requires_metric_and_ordered_window() -> None:
    with pytest.raises(ValidationError, match="metric"):
        build_opinion(metric=None)
    with pytest.raises(ValidationError, match="valid_until"):
        build_opinion(valid_until=NOW - timedelta(seconds=1))

    unscorable = build_opinion(
        verifiable=False,
        metric=None,
        valid_until=None,
    )
    assert unscorable.verifiable is False


def test_transient_evidence_and_verification_have_no_collection_identity() -> None:
    """验证行情证据和 LLM 2 临时结果不会被仓储基类映射为新集合。"""

    evidence = CreatorMarketEvidence(
        evidence_id="evidence-1",
        market_date="2026-07-25",
        as_of=NOW,
        facts={"return": 1.2},
        source="test",
        evidence_version="v1",
        generated_at=NOW,
    )
    verification = CreatorOpinionVerification(
        opinion_id="opinion-1",
        verdict="minor_deviation",
        reason="方向接近，但相对收益略低于阈值。",
        evidence_refs=["facts.return"],
    )

    assert evidence.facts["return"] == 1.2
    assert verification.verdict == "minor_deviation"
    assert not hasattr(CreatorMarketEvidence, "__tablename__")
    assert not hasattr(CreatorOpinionVerification, "__tablename__")


