from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.daily_market_analysis import (
    CreatorContext,
    CreatorOpinionAssessment,
    DailyMarketAnalysis,
    MarketReview,
    MorningAnalysisResult,
    MorningMainline,
    MorningReport,
    MorningReportSections,
    NewsWindowStats,
)
from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinSectorOpinion,
    DouyinTranscript,
    DouyinWorkAnalysis,
    DouyinWorkStatus,
)
from app.repositories.daily_market_analysis_repository import (
    DailyMarketAnalysisRepository,
)
from app.services.news_ranking_service import NewsRankingService


CN_TZ = timezone(timedelta(hours=8))


def build_finished_creator_work(
    *,
    work_id: str = "douyin-work-1",
    opinion_id: str = "douyin-work-1:半导体",
) -> DouyinCreatorWork:
    published_at = datetime(2026, 7, 23, 7, 30, tzinfo=CN_TZ)
    return DouyinCreatorWork(
        work_id=work_id,
        creator_sec_uid="creator-1",
        creator_name="测试博主",
        creator_short_id="creator-short-id",
        description="盘前关注半导体。",
        published_at=published_at,
        publish_ts=int(published_at.timestamp()),
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        duration_ms=60_000,
        first_seen_at=published_at,
        fetched_at=published_at,
        status=DouyinWorkStatus(status="finished"),
        transcript=DouyinTranscript(
            text="原始转写文本。",
            provider="test-asr",
            model="test-model",
            transcribed_at=published_at,
        ),
        analysis=DouyinWorkAnalysis(
            summary="博主认为半导体存在催化。",
            sector_opinions=[
                DouyinSectorOpinion(
                    opinion_id=opinion_id,
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


def build_mainlines() -> list[MorningMainline]:
    sectors = ["半导体", "通信设备", "汽车零部件", "银行", "医药商业"]
    return [
        MorningMainline(
            rank=rank,
            sector_name=sector_name,
            role="main_attack" if rank == 1 else "watch",
            confidence=80 - rank,
            reason=f"{sector_name}测试理由",
        )
        for rank, sector_name in enumerate(sectors, 1)
    ]


def build_report(*, created_at: datetime) -> DailyMarketAnalysis:
    return DailyMarketAnalysis(
        analysis_date="2026-07-23",
        trade_date="2026-07-23",
        prev_trade_date="2026-07-22",
        news_window=NewsWindowStats(
            window_start_ts=100,
            window_end_ts=200,
            window_hours=72,
            total_news_count=4,
            finished_news_count=4,
            unfinished_news_count=0,
            failed_news_count=0,
            completion_ratio=1.0,
            status_counts={"finished": 4},
        ),
        morning_report=MorningReport(
            report_date="2026-07-23",
            request_url="https://example.com/morning",
            response_url="https://example.com/morning",
            status_code=200,
            raw_content="盘前早报",
            sections=MorningReportSections(major_news="重大新闻"),
        ),
        previous_review=MarketReview(
            trade_date="2026-07-22",
            request_url="https://example.com/review",
            response_url="https://example.com/review",
            status_code=200,
            raw_content="昨日复盘",
        ),
        analysis=MorningAnalysisResult(
            market_style="结构性活跃",
            mainlines=build_mainlines(),
        ),
        created_at=created_at,
        updated_at=created_at,
    )


def build_news_document(
    event_id: str,
    *,
    publish_ts: int,
    analyses: list[dict[str, Any]] | None,
    source: str = "cls",
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source": source,
        "title": title if title is not None else f"新闻 {event_id}",
        "publish_time": "2026-07-23 08:00:00",
        "publish_ts": publish_ts,
        "sector_llm_analysis": analyses,
    }


def build_sector_analysis(
    sector_name: str,
    *,
    score: int,
    reason: str = "测试影响",
) -> dict[str, Any]:
    return {
        "sector_name": sector_name,
        "sector_llm_analysis": {
            "score": score,
            "reason": reason,
            "companies": None,
        },
    }


def test_morning_analysis_result_requires_ordered_unique_mainlines() -> None:
    result = MorningAnalysisResult(
        market_style="结构性活跃",
        mainlines=build_mainlines(),
    )

    assert [item.rank for item in result.mainlines] == [1, 2, 3, 4, 5]

    reversed_mainlines = list(reversed(build_mainlines()))
    with pytest.raises(ValueError, match="rank=1..5"):
        MorningAnalysisResult(
            market_style="结构性活跃",
            mainlines=reversed_mainlines,
        )

    duplicate_mainlines = build_mainlines()
    duplicate_mainlines[-1] = duplicate_mainlines[-1].model_copy(
        update={"sector_name": duplicate_mainlines[0].sector_name}
    )
    with pytest.raises(ValueError, match="重复板块"):
        MorningAnalysisResult(
            market_style="结构性活跃",
            mainlines=duplicate_mainlines,
        )


def test_creator_context_requires_finished_work_when_available() -> None:
    work = build_finished_creator_work()
    context = CreatorContext(status="available", works=[work])

    assert context.priority == "critical"
    assert context.works[0].work_id == work.work_id
    serialized = context.model_dump(mode="python")
    assert "transcript" not in serialized["works"][0]

    with pytest.raises(ValidationError, match="必须包含作品"):
        CreatorContext(status="available")

    with pytest.raises(ValidationError, match="必须说明原因"):
        CreatorContext(status="missing")


@pytest.mark.parametrize(
    "verdict",
    [
        "corroborated",
        "partially_corroborated",
        "unverified",
        "contradicted",
    ],
)
def test_creator_opinion_assessment_accepts_four_verdicts(verdict: str) -> None:
    assessment = CreatorOpinionAssessment(
        opinion_id="douyin-work-1:半导体",
        verdict=verdict,
        reason="测试判断。",
    )

    assert assessment.verdict == verdict

    with pytest.raises(ValidationError):
        CreatorOpinionAssessment(
            opinion_id="douyin-work-1:半导体",
            verdict="unsupported",
            reason="无效判断。",
        )


def test_news_rankings_require_complete_sector_analysis() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "valid",
            publish_ts=as_of_ts,
            analyses=[build_sector_analysis("半导体", score=70)],
        ),
        build_news_document(
            "other-sector",
            publish_ts=as_of_ts,
            analyses=[build_sector_analysis("不涉及版块", score=90)],
        ),
        build_news_document(
            "missing-detail",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "通信设备",
                    "sector_llm_analysis": None,
                }
            ],
        ),
        build_news_document(
            "future",
            publish_ts=as_of_ts + 1,
            analyses=[build_sector_analysis("银行", score=80)],
        ),
        build_news_document(
            "missing-analyses",
            publish_ts=as_of_ts,
            analyses=None,
        ),
    ]

    service = NewsRankingService()
    investment_ranking, heat_ranking, eligible_count = service.build_rankings(
        rows,
        as_of_ts=as_of_ts,
    )

    assert eligible_count == 1
    assert [item.sector_name for item in investment_ranking] == ["半导体"]
    assert [item.sector_name for item in heat_ranking] == ["半导体"]
    assert investment_ranking[0].news_count == 1
    assert heat_ranking[0].news_count == 1


def test_investment_ranking_requires_both_score_and_reason() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "valid",
            publish_ts=as_of_ts,
            analyses=[build_sector_analysis("半导体", score=70)],
        ),
        build_news_document(
            "missing-score",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "通信设备",
                    "sector_llm_analysis": {"reason": "只有理由"},
                }
            ],
        ),
        build_news_document(
            "missing-reason",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "银行",
                    "sector_llm_analysis": {"score": 60},
                }
            ],
        ),
    ]

    ranking = NewsRankingService().build_investment_ranking(
        rows,
        as_of_ts=as_of_ts,
    )

    assert [item.sector_name for item in ranking] == ["半导体"]


def test_heat_ranking_requires_score_and_reason_but_accepts_zero_score() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "zero-score",
            publish_ts=as_of_ts,
            analyses=[build_sector_analysis("通信设备", score=0)],
        ),
        build_news_document(
            "missing-detail",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "通信设备",
                    "sector_llm_analysis": None,
                }
            ],
        ),
        build_news_document(
            "missing-score",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "通信设备",
                    "sector_llm_analysis": {"reason": "只有理由"},
                }
            ],
        ),
        build_news_document(
            "missing-reason",
            publish_ts=as_of_ts,
            analyses=[
                {
                    "sector_name": "通信设备",
                    "sector_llm_analysis": {"score": 60},
                }
            ],
        ),
    ]

    ranking = NewsRankingService().build_heat_ranking(rows, as_of_ts=as_of_ts)

    assert [item.sector_name for item in ranking] == ["通信设备"]
    assert ranking[0].news_count == 1
    assert ranking[0].neutral_news_count == 1
    assert [item.event_id for item in ranking[0].evidence] == ["zero-score"]
    assert ranking[0].evidence[0].score == 0


def test_news_ranking_reduces_each_sector_effect_for_multi_sector_news() -> None:
    as_of_ts = 2_000_000
    single_sector = build_news_document(
        "single",
        publish_ts=as_of_ts,
        analyses=[build_sector_analysis("半导体", score=80)],
    )
    multi_sector = build_news_document(
        "multi",
        publish_ts=as_of_ts,
        analyses=[
            build_sector_analysis("半导体", score=80),
            build_sector_analysis("通信设备", score=80),
        ],
    )

    service = NewsRankingService()
    single_investment_ranking = service.build_investment_ranking(
        [single_sector], as_of_ts=as_of_ts
    )
    multi_investment_ranking = service.build_investment_ranking(
        [multi_sector], as_of_ts=as_of_ts
    )
    single_heat_ranking = service.build_heat_ranking([single_sector], as_of_ts=as_of_ts)
    multi_heat_ranking = service.build_heat_ranking([multi_sector], as_of_ts=as_of_ts)
    single_investment = single_investment_ranking[0]
    multi_investment = next(
        item for item in multi_investment_ranking if item.sector_name == "半导体"
    )
    single_heat = single_heat_ranking[0]
    multi_heat = next(
        item for item in multi_heat_ranking if item.sector_name == "半导体"
    )

    assert multi_investment.final_score < single_investment.final_score
    assert multi_heat.final_score < single_heat.final_score


def test_news_ranking_counts_directions_and_limits_evidence() -> None:
    as_of_ts = 2_000_000
    scores = [70, -50, 0, 40]
    rows = [
        build_news_document(
            f"event-{index}",
            publish_ts=as_of_ts - index,
            source="cls" if index % 2 else "jin10",
            analyses=[build_sector_analysis("半导体", score=score)],
        )
        for index, score in enumerate(scores, 1)
    ]

    service = NewsRankingService()
    investment_ranking = service.build_investment_ranking(
        rows,
        as_of_ts=as_of_ts,
        evidence_limit=2,
    )
    heat_ranking = service.build_heat_ranking(
        rows,
        as_of_ts=as_of_ts,
        evidence_limit=2,
    )
    investment = investment_ranking[0]
    heat = heat_ranking[0]

    assert investment.news_count == 4
    assert investment.positive_news_count == 2
    assert investment.negative_news_count == 1
    assert investment.neutral_news_count == 1
    assert investment.source_count == 2
    assert len(investment.evidence) == 2
    assert len(heat.evidence) == 2
    assert [item.score for item in investment.evidence] == [70, 40]
    assert [item.score for item in heat.evidence] == [70, 40]


def test_news_ranking_collapses_same_title_event_and_uses_median_score() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "duplicate-high",
            publish_ts=as_of_ts,
            title="同一条产业新闻",
            source="cls",
            analyses=[build_sector_analysis("半导体", score=50)],
        ),
        build_news_document(
            "duplicate-median-a",
            publish_ts=as_of_ts - 60,
            title="同一条产业新闻",
            source="jin10",
            analyses=[build_sector_analysis("半导体", score=12)],
        ),
        build_news_document(
            "duplicate-median-b",
            publish_ts=as_of_ts - 120,
            title="同一条产业新闻",
            source="10jqka",
            analyses=[build_sector_analysis("半导体", score=12)],
        ),
    ]
    service = NewsRankingService()

    ranking = service.build_investment_ranking(rows, as_of_ts=as_of_ts)
    reversed_ranking = service.build_investment_ranking(
        reversed(rows),
        as_of_ts=as_of_ts,
    )

    assert ranking == reversed_ranking
    assert ranking[0].news_count == 1
    assert ranking[0].source_count == 1
    assert ranking[0].latest_publish_ts == as_of_ts - 120
    assert ranking[0].evidence[0].event_id == "duplicate-median-b"
    assert ranking[0].evidence[0].score == 12


def test_heat_ranking_ignores_incomplete_cross_source_copy() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "stage-one",
            publish_ts=as_of_ts - 120,
            title="同一条跨来源新闻",
            source="jin10",
            analyses=[{"sector_name": "通信设备", "sector_llm_analysis": None}],
        ),
        build_news_document(
            "with-detail",
            publish_ts=as_of_ts,
            title="同一条跨来源新闻",
            source="cls",
            analyses=[build_sector_analysis("通信设备", score=70)],
        ),
    ]

    ranking = NewsRankingService().build_heat_ranking(rows, as_of_ts=as_of_ts)

    assert ranking[0].news_count == 1
    assert ranking[0].recent_news_count == 1
    assert ranking[0].source_count == 1
    assert ranking[0].latest_publish_ts == as_of_ts
    assert ranking[0].evidence[0].event_id == "with-detail"
    assert ranking[0].evidence[0].score == 70


def test_heat_ranking_keeps_same_title_outside_deduplication_window() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            "first-event",
            publish_ts=as_of_ts - 901,
            title="周期性重复标题",
            analyses=[build_sector_analysis("电力", score=40)],
        ),
        build_news_document(
            "second-event",
            publish_ts=as_of_ts,
            title="周期性重复标题",
            analyses=[build_sector_analysis("电力", score=40)],
        ),
    ]

    ranking = NewsRankingService().build_heat_ranking(rows, as_of_ts=as_of_ts)

    assert ranking[0].news_count == 2


def test_heat_ranking_does_not_merge_short_titles() -> None:
    as_of_ts = 2_000_000
    rows = [
        build_news_document(
            event_id,
            publish_ts=as_of_ts,
            title="AI",
            analyses=[build_sector_analysis("软件开发", score=40)],
        )
        for event_id in ("short-a", "short-b")
    ]

    ranking = NewsRankingService().build_heat_ranking(rows, as_of_ts=as_of_ts)

    assert ranking[0].news_count == 2


def test_heat_ranking_scale_distinguishes_large_event_counts() -> None:
    as_of_ts = 2_000_000
    service = NewsRankingService()

    def score_for_count(count: int) -> float:
        rows = [
            build_news_document(
                f"event-{count}-{index}",
                publish_ts=as_of_ts,
                source=("cls", "jin10", "10jqka")[index % 3],
                analyses=[build_sector_analysis("半导体", score=40)],
            )
            for index in range(count)
        ]
        return service.build_heat_ranking(rows, as_of_ts=as_of_ts)[0].final_score

    score_20 = score_for_count(20)
    score_40 = score_for_count(40)
    score_80 = score_for_count(80)

    assert 0 <= score_20 < score_40 < score_80 <= 100
    assert score_40 - score_20 > 10
    assert score_80 - score_40 > 5


class FakeCollection:
    def __init__(self) -> None:
        self.document: dict[str, Any] | None = None
        self.update_calls: list[dict[str, Any]] = []

    async def update_one(
        self,
        filters: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool,
    ) -> None:
        self.update_calls.append(
            {
                "filters": filters,
                "update": update,
                "upsert": upsert,
            }
        )
        if self.document is None:
            self.document = dict(update.get("$setOnInsert", {}))
        self.document.update(update.get("$set", {}))


class FakeDatabase:
    def __init__(self, collection: FakeCollection) -> None:
        self.collection = collection
        self.requested_collection_names: list[str] = []

    def __getitem__(self, collection_name: str) -> FakeCollection:
        self.requested_collection_names.append(collection_name)
        return self.collection


def test_daily_repository_upsert_preserves_original_created_at() -> None:
    first_created_at = datetime(2026, 7, 23, 8, 0, tzinfo=CN_TZ)
    second_created_at = datetime(2026, 7, 23, 8, 5, tzinfo=CN_TZ)
    final_updated_at = datetime(2026, 7, 23, 8, 10, tzinfo=CN_TZ)
    collection = FakeCollection()
    database = FakeDatabase(collection)
    repository = DailyMarketAnalysisRepository(database=database)  # type: ignore[arg-type]

    async def run_upserts() -> None:
        await repository.upsert_report(
            build_report(created_at=first_created_at),
            updated_at=first_created_at,
        )
        await repository.upsert_report(
            build_report(created_at=second_created_at),
            updated_at=final_updated_at,
        )

    asyncio.run(run_upserts())

    assert database.requested_collection_names == ["daily_market_analysis"]
    assert len(collection.update_calls) == 2
    assert all(call["upsert"] is True for call in collection.update_calls)
    assert all(
        call["filters"] == {"analysis_date": "2026-07-23"}
        for call in collection.update_calls
    )
    assert collection.document is not None
    assert collection.document["created_at"] == first_created_at
    assert collection.document["updated_at"] == final_updated_at
    assert "created_at" not in collection.update_calls[1]["update"]["$set"]
