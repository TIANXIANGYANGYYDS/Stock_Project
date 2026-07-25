from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.llm import LLMResponseError
from app.llm.morning_analysis_llm import MorningAnalysisLLMAnalyzer
from app.models.daily_market_analysis import (
    CreatorContext,
    CreatorWorkContext,
    MarketReview,
    MarketReviewSection,
    MorningAnalysisResult,
    MorningMainline,
    MorningReport,
    MorningReportSections,
    NewsWindowStats,
    SectorNewsEvidence,
    SectorRankingItem,
)
from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinSectorOpinion,
    DouyinTranscript,
    DouyinWorkAnalysis,
    DouyinWorkStatus,
)


FIXTURE_INDUSTRY_BOARDS_FILE = (
    Path(__file__).parents[1]
    / "app"
    / "manually_execute_script"
    / "data"
    / "a_stock_ths_industry_boards.json"
)
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
OPINION_ID = "douyin-work-1:半导体"
TRANSCRIPT_SENTINEL = "绝密原始转写，不得进入主提示词"


def build_analyzer() -> MorningAnalysisLLMAnalyzer:
    return MorningAnalysisLLMAnalyzer(
        api_key="test-key",
        model="test-model",
        api_base_url="https://example.com/v1",
        industry_boards_file=str(FIXTURE_INDUSTRY_BOARDS_FILE),
    )


def test_morning_analyzer_uses_fixed_qwen_analysis_profile() -> None:
    analyzer = MorningAnalysisLLMAnalyzer(
        api_key="test",
        api_base_url="https://example.com/v1",
        industry_boards_file=str(FIXTURE_INDUSTRY_BOARDS_FILE),
    )

    assert analyzer.model == "qwen3.7-max"
    assert analyzer.thinking_enabled is True


def build_creator_context() -> CreatorContext:
    published_at = datetime(2026, 7, 23, 7, 30, tzinfo=CN_TZ)
    work = DouyinCreatorWork(
        work_id="douyin-work-1",
        creator_sec_uid="creator-1",
        creator_name="测试博主",
        creator_short_id="creator-short-id",
        description="盘前关注半导体。",
        published_at=published_at,
        publish_ts=int(published_at.timestamp()),
        canonical_url="https://www.douyin.com/video/douyin-work-1",
        duration_ms=60_000,
        first_seen_at=published_at,
        fetched_at=published_at,
        status=DouyinWorkStatus(status="finished"),
        transcript=DouyinTranscript(
            text=TRANSCRIPT_SENTINEL,
            provider="test-asr",
            model="test-model",
            transcribed_at=published_at,
        ),
        analysis=DouyinWorkAnalysis(
            summary="博主认为半导体产业政策可能形成增量催化。",
            sector_opinions=[
                DouyinSectorOpinion(
                    opinion_id=OPINION_ID,
                    sector_name="半导体",
                    stance_score=70,
                    reason="产业政策可能带来增量预期。",
                )
            ],
            analysis_version="douyin_creator_analysis_v1",
            analysis_model="test-model",
            analyzed_at=published_at,
        ),
    )
    return CreatorContext(
        status="available",
        source_date="2026-07-22",
        age_seconds=90 * 60,
        works=[work],
    )


def build_inputs() -> dict[str, Any]:
    evidence = SectorNewsEvidence(
        event_id="news-1",
        source="cls",
        title="芯片产业政策",
        publish_time="2026-07-23 08:00:00",
        publish_ts=1_774_224_000,
        score=70,
        reason="政策直接支持半导体产业。",
    )
    ranking = SectorRankingItem(
        rank=1,
        sector_name="半导体",
        final_score=70,
        news_count=1,
        positive_news_count=1,
        source_count=1,
        latest_publish_ts=evidence.publish_ts,
        evidence=[evidence],
    )
    return {
        "analysis_date": "2026-07-23",
        "previous_trade_date": "2026-07-22",
        "creator_context": build_creator_context(),
        "morning_report": MorningReport(
            report_date="2026-07-23",
            request_url="https://example.com/morning",
            response_url="https://example.com/morning",
            status_code=200,
            raw_content="早报",
            sections=MorningReportSections(major_news="芯片产业政策"),
        ),
        "previous_review": MarketReview(
            trade_date="2026-07-22",
            request_url="https://example.com/review",
            response_url="https://example.com/review",
            status_code=200,
            summary="半导体领涨",
            sections=[MarketReviewSection(title="主线", content="半导体形成联动")],
            raw_content="半导体领涨",
        ),
        "news_window": NewsWindowStats(
            window_start_ts=1,
            window_end_ts=2,
            window_hours=72,
            total_news_count=1,
            finished_news_count=1,
            unfinished_news_count=0,
            failed_news_count=0,
            completion_ratio=1,
            status_counts={"finished": 1},
        ),
        "investment_ranking": [ranking],
        "heat_ranking": [ranking],
    }


def build_llm_result(*, event_id: str = "news-1", first_sector: str = "半导体") -> str:
    sectors = [first_sector, "通信设备", "软件开发", "电池", "银行"]
    return json.dumps(
        {
            "market_bias": "bullish",
            "risk_level": "medium",
            "risk_summary": "海外波动仍需观察，但产业政策提供承接。",
            "market_style": "成长进攻",
            "creator_opinion_assessments": [
                {
                    "opinion_id": OPINION_ID,
                    "verdict": "corroborated",
                    "reason": "昨日盘面与今晨产业政策形成双重印证。",
                }
            ],
            "mainlines": [
                {
                    "rank": rank,
                    "sector_name": sector,
                    "role": "main_attack" if rank == 1 else "watch",
                    "confidence": 80 - rank,
                    "reason": "结合昨日盘面和今晨催化后的比较结论。",
                    "supporting_news_ids": [event_id] if rank == 1 else [],
                    "supporting_creator_opinion_ids": (
                        [OPINION_ID] if rank == 1 else []
                    ),
                    "risks": [],
                }
                for rank, sector in enumerate(sectors, 1)
            ],
        },
        ensure_ascii=False,
    )


def test_morning_analyzer_returns_validated_structured_result() -> None:
    analyzer = build_analyzer()

    def fake_chat(**kwargs: Any) -> str:
        assert kwargs["response_format"] == {"type": "json_object"}
        assert OPINION_ID in kwargs["user_prompt"]
        assert TRANSCRIPT_SENTINEL not in kwargs["user_prompt"]
        prompt_payload = json.loads(kwargs["user_prompt"].split("\n", 1)[-1])
        assert prompt_payload["creator_context"]["priority"] == "critical"
        assert prompt_payload["creator_context"]["source_date"] == "2026-07-22"
        if "market_risk_assessment" in prompt_payload:
            assert "news-1" in kwargs["user_prompt"]
            assert prompt_payload["market_risk_assessment"]["risk_level"] == (
                "medium"
            )
        creator_work = prompt_payload["creator_context"]["works"][0]
        assert "publish_ts" not in creator_work
        assert "analysis_version" not in creator_work["analysis"]
        assert "transcript" not in json.dumps(
            prompt_payload["creator_context"],
            ensure_ascii=False,
        )
        return "分析结果：" + build_llm_result()

    analyzer.chat = fake_chat  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.market_style == "成长进攻"
    assert result.market_bias == "bullish"
    assert result.risk_level == "medium"
    assert [item.rank for item in result.mainlines] == [1, 2, 3, 4, 5]
    assert result.mainlines[0].supporting_news_ids == ["news-1"]
    assert result.mainlines[0].supporting_creator_opinion_ids == [OPINION_ID]
    assert result.creator_opinion_assessments[0].verdict == "corroborated"


def test_morning_analyzer_drops_unknown_evidence_id_after_retries() -> None:
    analyzer = build_analyzer()
    analyzer.chat = lambda **kwargs: build_llm_result(event_id="invented")  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.mainlines[0].supporting_news_ids == []


def test_morning_analyzer_rejects_non_candidate_sector() -> None:
    analyzer = build_analyzer()
    analyzer.chat = lambda **kwargs: build_llm_result(first_sector="AI概念")  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="候选集外板块"):
        asyncio.run(analyzer.analyze(**build_inputs()))


def test_morning_analyzer_drops_evidence_from_another_sector_after_retries() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][0]["sector_name"] = "通信设备"
    raw_result["mainlines"][1]["sector_name"] = "半导体"
    raw_result["mainlines"][0]["supporting_creator_opinion_ids"] = []
    raw_result["mainlines"][1]["supporting_creator_opinion_ids"] = [OPINION_ID]
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.mainlines[0].supporting_news_ids == []


def test_morning_analyzer_rejects_unknown_creator_opinion_reference() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][0]["supporting_creator_opinion_ids"] = ["invented"]
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="未知或其他行业"):
        asyncio.run(analyzer.analyze(**build_inputs()))


def test_morning_analyzer_drops_creator_opinion_from_another_sector() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][1]["supporting_creator_opinion_ids"] = [OPINION_ID]
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.mainlines[0].supporting_creator_opinion_ids == [OPINION_ID]
    assert result.mainlines[1].supporting_creator_opinion_ids == []


def test_morning_analyzer_requires_assessment_for_every_creator_opinion() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["creator_opinion_assessments"] = []
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="逐条评估"):
        asyncio.run(analyzer.analyze(**build_inputs()))


def test_morning_analyzer_requires_corroborated_opinion_in_mainlines() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][0]["supporting_creator_opinion_ids"] = []
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="已被印证"):
        asyncio.run(analyzer.analyze(**build_inputs()))


def test_corroborated_non_positive_opinion_does_not_force_sector_into_mainlines() -> None:
    analyzer = build_analyzer()
    inputs = build_inputs()
    opinion = inputs["creator_context"].works[0].analysis.sector_opinions[0]
    opinion.stance_score = -70
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][0]["supporting_creator_opinion_ids"] = []
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**inputs))

    assert result.creator_opinion_assessments[0].verdict == "corroborated"
    assert not result.mainlines[0].supporting_creator_opinion_ids


def test_morning_analyzer_downgrades_attack_with_active_creator_warning() -> None:
    analyzer = build_analyzer()
    inputs = build_inputs()
    opinion = inputs["creator_context"].works[0].analysis.sector_opinions[0]
    opinion.stance_score = 0
    raw_result = json.loads(build_llm_result())
    raw_result["mainlines"][0]["role"] = "secondary_attack"
    raw_result["creator_opinion_assessments"][0]["verdict"] = (
        "partially_corroborated"
    )
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**inputs))

    assert result.mainlines[0].role == "watch"
    assert "最终风险约束" in result.mainlines[0].reason
    assert result.mainlines[0].supporting_creator_opinion_ids == [OPINION_ID]
    assert result.mainlines[0].risks == [f"博主风险提示：{opinion.reason}"]


def test_morning_analyzer_handles_more_than_five_corroborated_sectors() -> None:
    analyzer = build_analyzer()
    published_at = datetime(2026, 7, 23, 7, 30, tzinfo=CN_TZ)
    sectors = ["半导体", "通信设备", "软件开发", "电池", "银行", "证券"]
    works = []
    for work_index, work_sectors in enumerate((sectors[:3], sectors[3:])):
        works.append(
            CreatorWorkContext(
                work_id=f"work-{work_index}",
                creator_name="测试博主",
                published_at=published_at,
                publish_ts=int(published_at.timestamp()),
                analysis=DouyinWorkAnalysis(
                    summary="多个行业观点",
                    sector_opinions=[
                        DouyinSectorOpinion(
                            opinion_id=f"opinion:{sector}",
                            sector_name=sector,
                            stance_score=50,
                            reason="已被其他材料印证",
                        )
                        for sector in work_sectors
                    ],
                    analysis_version="v1",
                    analysis_model="test",
                    analyzed_at=published_at,
                ),
            )
        )
    context = CreatorContext(status="available", works=works)

    def build_result(selected_sectors: list[str]) -> MorningAnalysisResult:
        return MorningAnalysisResult(
            risk_summary="测试系统性风险。",
            market_style="测试",
            creator_opinion_assessments=[
                {
                    "opinion_id": f"opinion:{sector}",
                    "verdict": "corroborated",
                    "reason": "交叉证据一致",
                }
                for sector in sectors
            ],
            mainlines=[
                MorningMainline(
                    rank=rank,
                    sector_name=sector,
                    role="main_attack" if rank == 1 else "watch",
                    confidence=70,
                    reason="测试",
                    supporting_creator_opinion_ids=(
                        [f"opinion:{sector}"] if sector in sectors else []
                    ),
                )
                for rank, sector in enumerate(selected_sectors, 1)
            ],
        )

    analyzer._validate_business_constraints(
        build_result(sectors[:5]),
        creator_context=context,
        investment_ranking=[],
        heat_ranking=[],
    )
    with pytest.raises(LLMResponseError, match="五条主线必须全部"):
        analyzer._validate_business_constraints(
            build_result([*sectors[:4], "食品加工制造"]),
            creator_context=context,
            investment_ranking=[],
            heat_ranking=[],
        )


@pytest.mark.parametrize(
    ("verdict", "role", "risks"),
    [
        ("unverified", "main_attack", []),
        ("contradicted", "watch", []),
        ("contradicted", "main_attack", ["已有反证"]),
    ],
)
def test_morning_analyzer_restricts_unverified_and_contradicted_usage(
    verdict: str,
    role: str,
    risks: list[str],
) -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["creator_opinion_assessments"][0]["verdict"] = verdict
    raw_result["mainlines"][0]["role"] = role
    raw_result["mainlines"][0]["risks"] = risks
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="核验结论与主线角色冲突"):
        asyncio.run(analyzer.analyze(**build_inputs()))


def test_morning_analyzer_continues_without_available_creator_context() -> None:
    analyzer = build_analyzer()
    inputs = build_inputs()
    inputs["creator_context"] = CreatorContext(
        status="fetch_failed",
        reason="creator database unavailable",
    )
    raw_result = json.loads(build_llm_result())
    raw_result["creator_opinion_assessments"] = []
    for mainline in raw_result["mainlines"]:
        mainline["supporting_creator_opinion_ids"] = []

    def fake_chat(**kwargs: Any) -> str:
        assert TRANSCRIPT_SENTINEL not in kwargs["user_prompt"]
        prompt_payload = json.loads(kwargs["user_prompt"].split("\n", 1)[-1])
        assert prompt_payload["creator_context"]["status"] == "fetch_failed"
        assert prompt_payload["creator_context"]["works"] == []
        return json.dumps(raw_result, ensure_ascii=False)

    analyzer.chat = fake_chat  # type: ignore[method-assign]
    result = asyncio.run(analyzer.analyze(**inputs))

    assert result.creator_opinion_assessments == []
    assert all(not item.supporting_creator_opinion_ids for item in result.mainlines)


def test_morning_analyzer_retries_invalid_structured_result_once() -> None:
    analyzer = build_analyzer()
    responses = iter(
        [
            build_llm_result(),
            build_llm_result(event_id="invented"),
            build_llm_result(),
        ]
    )
    analyzer.chat = lambda **kwargs: next(responses)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.mainlines[0].supporting_news_ids == ["news-1"]


def test_morning_analyzer_drops_redundant_top_level_news_ids() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result["supporting_news_ids"] = ["news-1"]
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    result = asyncio.run(analyzer.analyze(**build_inputs()))

    assert result.mainlines[0].supporting_news_ids == ["news-1"]


def test_morning_analyzer_locks_independent_risk_assessment() -> None:
    analyzer = build_analyzer()
    risk_result = json.loads(build_llm_result())
    risk_result.update(
        {
            "market_bias": "bearish",
            "risk_level": "high",
            "risk_summary": "多类风险簇同时出现。",
        }
    )
    responses = iter(
        [
            json.dumps(risk_result, ensure_ascii=False),
            build_llm_result(),
        ]
    )
    analyzer.chat = lambda **kwargs: next(responses)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="改写了已锁定"):
        asyncio.run(analyzer.analyze(**build_inputs(), schema_retries=0))


def test_high_risk_morning_analysis_rejects_main_attack() -> None:
    analyzer = build_analyzer()
    raw_result = json.loads(build_llm_result())
    raw_result.update(
        {
            "market_bias": "bearish",
            "risk_level": "high",
            "risk_summary": "多类风险簇同时出现。",
        }
    )
    analyzer.chat = lambda **kwargs: json.dumps(raw_result, ensure_ascii=False)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="不得输出 main_attack"):
        asyncio.run(analyzer.analyze(**build_inputs(), schema_retries=0))


def test_non_positive_creator_opinion_cannot_support_attack_role() -> None:
    analyzer = build_analyzer()
    inputs = build_inputs()
    opinion = inputs["creator_context"].works[0].analysis.sector_opinions[0]
    opinion.stance_score = 0
    raw_result = json.loads(build_llm_result())
    raw_result["creator_opinion_assessments"][0]["verdict"] = (
        "partially_corroborated"
    )
    result = MorningAnalysisResult.model_validate(raw_result)

    with pytest.raises(LLMResponseError, match="non_positive"):
        analyzer._validate_business_constraints(
            result,
            creator_context=inputs["creator_context"],
            investment_ranking=inputs["investment_ranking"],
            heat_ranking=inputs["heat_ranking"],
        )
