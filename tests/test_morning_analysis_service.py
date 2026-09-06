from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from app.models.daily_market_analysis import (
    MarketReview,
    MorningAnalysisResult,
    MorningMainline,
    MorningReport,
    MorningReportSections,
    SectorNewsEvidence,
    SectorRankingItem,
)
from app.models.creator_monitoring import (
    CreatorOpinion,
    CreatorWork,
    CreatorWorkAnalysis,
    CreatorWorkStatus,
)
from app.models.news_ranking_snapshot import (
    NewsRankingFormulaVersions,
    NewsRankingSnapshot,
    NewsRankingSourceStats,
)
from app.services.morning_analysis_service import MorningAnalysisService
from app.services.trading_calendar_service import (
    MorningTradeDateDecision,
    resolve_morning_trade_dates,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class FakeRankingSnapshotRepository:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.dates = []

    async def find_latest_completed_by_biz_date(
        self,
        biz_date,
        *,
        window_end_ts_lte=None,
    ):
        self.dates.append((biz_date, window_end_ts_lte))
        return self.snapshot


class FakeReportRepository:
    def __init__(self):
        self.index_calls = 0
        self.reports = []

    async def create_indexes(self):
        self.index_calls += 1

    async def upsert_report(self, report, *, updated_at=None):
        self.reports.append((report, updated_at))


class FakeCreatorWorkRepository:
    def __init__(self, *, works=None, latest=None, error=None):
        self.works = list(works or [])
        self.latest = latest
        self.error = error
        self.list_calls = []
        self.latest_calls = []

    async def list_finished_works_by_published_window(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return [
            work
            for work in self.works
            if work.creator_id == kwargs["creator_id"]
        ]

    async def find_latest_finished_before(self, **kwargs):
        self.latest_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.latest


class FakeCreatorVerificationRepository:
    def __init__(self, verifications=None, *, error=None):
        self.verifications = list(verifications or [])
        self.error = error
        self.calls = []

    async def list_by_market_date(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.verifications


class FakeMorningCrawler:
    def __init__(self):
        self.dates = []

    async def fetch(self, trade_date):
        self.dates.append(trade_date)
        return MorningReport(
            report_date="2026-07-23",
            request_url="https://example.com/morning",
            response_url="https://example.com/morning",
            status_code=200,
            raw_content="早报",
            sections=MorningReportSections(major_news="产业政策"),
        )


class FakeReviewCrawler:
    def __init__(self):
        self.dates = []

    async def fetch(self, trade_date):
        self.dates.append(trade_date)
        return MarketReview(
            trade_date="2026-07-22",
            request_url="https://example.com/review",
            response_url="https://example.com/review",
            status_code=200,
            summary="半导体领涨",
            raw_content="半导体领涨",
        )


class FakeAnalyzer:
    def __init__(self):
        self.inputs = None
        self.last_source_memos = {"previous_review": "独立复盘备忘录"}
        self.last_scenario_memos = {"reversal": "独立反转论证"}

    async def analyze(self, **kwargs):
        self.inputs = kwargs
        sectors = ["半导体", "通信设备", "软件开发", "电池", "银行"]
        return MorningAnalysisResult(
            market_style="成长进攻",
            mainlines=[
                MorningMainline(
                    rank=rank,
                    sector_name=sector,
                    role="main_attack" if rank == 1 else "watch",
                    confidence=80 - rank,
                    reason="测试结论",
                )
                for rank, sector in enumerate(sectors, 1)
            ],
        )


def build_creator_work(
    now: datetime,
    *,
    age_hours: int = 17,
    first_seen_at: datetime | None = None,
    analyzed_at: datetime | None = None,
    work_id: str = "douyin-work-1",
    creator_id: str = "creator-1",
    creator_name: str = "测试博主",
) -> CreatorWork:
    published_at = now - timedelta(hours=age_hours)
    first_seen_at = first_seen_at or published_at
    work_key = f"douyin:{work_id}"
    return CreatorWork(
        creator_id=creator_id,
        creator_name=creator_name,
        account_id="douyin:creator-account",
        platform="douyin",
        platform_work_id=work_id,
        content_type="video",
        title="盘前关注半导体。",
        published_at=published_at,
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        duration_ms=60_000,
        first_seen_at=first_seen_at,
        fetched_at=first_seen_at,
        source_text="盘前关注半导体。",
        extracted_text="原始转写文本。",
        status=CreatorWorkStatus(status="finished"),
        analysis=CreatorWorkAnalysis(
            summary="博主认为半导体有增量催化。",
            opinions=[
                CreatorOpinion(
                    opinion_id=f"{work_key}:sector:0",
                    work_key=work_key,
                    target_type="sector",
                    target_name="半导体",
                    direction="bullish",
                    stance_score=70,
                    claim="产业政策可能形成增量预期。",
                    horizon="次日",
                    valid_from=published_at,
                    valid_until=published_at + timedelta(days=1),
                    metric="半导体行业相对表现",
                    source_quote="盘前关注半导体。",
                )
            ],
            analysis_version="creator_opinion_v1",
            analysis_model="test-model",
            analyzed_at=analyzed_at or first_seen_at,
        ),
    )


def build_creator_verification(
    creator_id: str = "creator-1",
    *,
    creator_name: str = "测试博主",
    rolling_score: float | None = 80.0,
    daily_score: float | None = 80.0,
    sample_count: int = 5,
):
    class Verification:
        pass

    verification = Verification()
    verification.creator_id = creator_id
    verification.creator_name = creator_name
    verification.rolling_score = rolling_score
    verification.daily_score = daily_score
    verification.sample_count = sample_count
    return verification


def build_snapshot(
    now: datetime,
    *,
    age_seconds: int = 60,
    status_counts: dict[str, int] | None = None,
) -> NewsRankingSnapshot:
    status_counts = status_counts or {
        "finished": 1,
        "crawled": 1,
        "sector_detail_failed": 1,
    }
    event_ts = int(now.timestamp()) - age_seconds
    evidence = SectorNewsEvidence(
        event_id="news-1",
        source="cls",
        title="芯片产业政策",
        publish_time="2026-07-23 08:19:00",
        publish_ts=event_ts,
        score=70,
        reason="政策直接支持产业。",
    )
    ranking = SectorRankingItem(
        rank=1,
        sector_name="半导体",
        final_score=80,
        news_count=1,
        evidence=[evidence],
    )
    end_ts = int(now.timestamp()) - age_seconds
    return NewsRankingSnapshot(
        snapshot_id=f"snapshot-{end_ts}",
        biz_date="2026-07-23",
        window_start_ts=end_ts - 72 * 3600,
        window_end_ts=end_ts,
        generated_at=datetime.fromtimestamp(end_ts, tz=CN_TZ),
        source_stats=NewsRankingSourceStats(
            total_news_count=sum(status_counts.values()),
            investment_eligible_count=status_counts.get("finished", 0),
            heat_eligible_count=2,
            status_counts=status_counts,
        ),
        formula_versions=NewsRankingFormulaVersions(
            investment="investment_v2",
            heat="heat_v2",
        ),
        investment_ranking=[ranking],
        heat_ranking=[ranking],
    )


def test_morning_analysis_service_builds_and_upserts_report() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    snapshot_repository = FakeRankingSnapshotRepository(build_snapshot(now))
    report_repository = FakeReportRepository()
    morning_crawler = FakeMorningCrawler()
    review_crawler = FakeReviewCrawler()
    analyzer = FakeAnalyzer()
    creator_repository = FakeCreatorWorkRepository(works=[build_creator_work(now)])
    verification_repository = FakeCreatorVerificationRepository(
        [build_creator_verification()]
    )
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=report_repository,
        ranking_snapshot_repository=snapshot_repository,
        creator_work_repository=creator_repository,
        creator_verification_repository=verification_repository,
        morning_crawler=morning_crawler,
        review_crawler=review_crawler,
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now, ranking_limit=12))

    assert result.skipped is False
    assert result.report is not None
    assert result.report.analysis_date == "2026-07-23"
    assert result.report.news_window.total_news_count == 3
    assert result.report.news_window.finished_news_count == 1
    assert result.report.news_window.failed_news_count == 1
    assert result.report.news_window.completion_ratio == pytest.approx(1 / 3)
    assert result.report.data_quality == "degraded"
    assert result.report.investment_ranking[0].sector_name == "半导体"
    assert result.report.ranking_snapshot_meta is not None
    assert result.report.ranking_snapshot_meta.snapshot_id.startswith("snapshot-")
    assert result.report.ranking_snapshot_meta.is_stale is False
    assert result.report.prompt_version == "morning_analysis_v10_active_creator_events"
    assert result.report.analysis_model == ""
    assert result.report.thinking_enabled is False
    assert result.report.source_analysis_memos == {
        "previous_review": "独立复盘备忘录"
    }
    assert result.report.scenario_analysis_memos == {"reversal": "独立反转论证"}
    assert result.report.creator_context.status == "available"
    assert result.report.creator_context.priority == "critical"
    assert result.report.creator_context.source_date == "2026-07-22"
    assert result.report.creator_context.ranking_market_date == "2026-07-22"
    assert result.report.creator_context.ranked_creators[0].creator_id == "creator-1"
    assert result.report.creator_context.ranked_creators[0].rolling_score == 80.0
    assert morning_crawler.dates == ["2026-07-23"]
    assert review_crawler.dates == ["2026-07-22"]
    assert report_repository.index_calls == 1
    assert len(report_repository.reports) == 1
    assert analyzer.inputs["investment_ranking"][0].evidence[0].event_id == "news-1"
    assert analyzer.inputs["news_window"].completion_ratio == pytest.approx(1 / 3)
    assert analyzer.inputs["news_window"].ranking_snapshot_stale is False
    assert analyzer.inputs["creator_context"].status == "available"
    assert snapshot_repository.dates == [
        ("2026-07-23", int(now.timestamp()))
    ]
    assert creator_repository.list_calls == [
        {
            "creator_id": "creator-1",
            "start_at": datetime(2026, 7, 22, 8, 20, tzinfo=CN_TZ),
            "end_at": datetime(2026, 7, 23, 8, 20, 1, tzinfo=CN_TZ),
            "available_at": now,
            "limit": 3,
        }
    ]
    assert creator_repository.latest_calls == []
    assert verification_repository.calls == [
        {"market_date": "2026-07-22", "status": "completed"}
    ]


def test_morning_analysis_service_dry_run_returns_report_without_database_write() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    report_repository = FakeReportRepository()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=report_repository,
        ranking_snapshot_repository=FakeRankingSnapshotRepository(build_snapshot(now)),
        creator_work_repository=FakeCreatorWorkRepository(
            works=[build_creator_work(now)]
        ),
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=FakeAnalyzer(),
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now, persist=False))

    assert result.report is not None
    assert result.report.analysis_date == "2026-07-23"
    assert report_repository.index_calls == 0
    assert report_repository.reports == []


def test_morning_analysis_service_skips_non_trading_day_before_io() -> None:
    snapshot_repository = FakeRankingSnapshotRepository(None)
    report_repository = FakeReportRepository()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-25",
        analysis_date="2026-07-24",
        prev_trade_date="2026-07-23",
        is_current_trade_day=False,
    )
    service = MorningAnalysisService(
        report_repository=report_repository,
        ranking_snapshot_repository=snapshot_repository,
        creator_work_repository=FakeCreatorWorkRepository(),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=FakeAnalyzer(),
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(
        service.run(reference_datetime=datetime(2026, 7, 25, 9, 0, tzinfo=CN_TZ))
    )

    assert result.skipped is True
    assert "不是 A 股交易日" in result.reason
    assert report_repository.index_calls == 0
    assert snapshot_repository.dates == []


def test_morning_analysis_service_requires_published_snapshot() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    report_repository = FakeReportRepository()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=report_repository,
        ranking_snapshot_repository=FakeRankingSnapshotRepository(None),
        creator_work_repository=FakeCreatorWorkRepository(
            works=[build_creator_work(now)]
        ),
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=FakeAnalyzer(),
        trade_date_resolver=lambda value: decision,
    )

    with pytest.raises(RuntimeError, match="缺少.*新闻榜单快照"):
        asyncio.run(service.run(reference_datetime=now))

    assert report_repository.reports == []


def test_morning_analysis_service_marks_stale_snapshot_degraded() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    snapshot = build_snapshot(
        now,
        age_seconds=16 * 60,
        status_counts={"finished": 10},
    )
    analyzer = FakeAnalyzer()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=FakeReportRepository(),
        ranking_snapshot_repository=FakeRankingSnapshotRepository(snapshot),
        creator_work_repository=FakeCreatorWorkRepository(
            works=[build_creator_work(now)]
        ),
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(
        service.run(reference_datetime=now, max_snapshot_age_minutes=15)
    )

    assert result.report is not None
    assert result.report.data_quality == "degraded"
    assert result.report.ranking_snapshot_meta is not None
    assert result.report.ranking_snapshot_meta.is_stale is True
    assert result.report.ranking_snapshot_meta.age_seconds == 16 * 60
    assert analyzer.inputs["news_window"].ranking_snapshot_stale is True


def test_morning_analysis_service_marks_complete_with_fresh_creator_context() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    snapshot = build_snapshot(now, status_counts={"finished": 10})
    analyzer = FakeAnalyzer()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=FakeReportRepository(),
        ranking_snapshot_repository=FakeRankingSnapshotRepository(snapshot),
        creator_work_repository=FakeCreatorWorkRepository(
            works=[build_creator_work(now)]
        ),
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now))

    assert result.report is not None
    assert result.report.data_quality == "complete"
    assert result.report.creator_context.status == "available"
    assert analyzer.inputs["creator_context"].status == "available"


def test_delayed_run_keeps_the_configured_morning_cutoff() -> None:
    cutoff = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
    execution_time = cutoff + timedelta(hours=5)
    creator_repository = FakeCreatorWorkRepository(
        works=[build_creator_work(cutoff)]
    )
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=FakeReportRepository(),
        ranking_snapshot_repository=FakeRankingSnapshotRepository(
            build_snapshot(cutoff)
        ),
        creator_work_repository=creator_repository,
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        creator_enabled=True,
        analysis_hour=9,
        analysis_minute=0,
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=FakeAnalyzer(),
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=execution_time))

    assert result.report is not None
    assert result.report.created_at == execution_time
    assert result.report.ranking_snapshot_meta is not None
    assert result.report.ranking_snapshot_meta.age_seconds == 60
    assert creator_repository.list_calls[0]["end_at"] == datetime(
        2026, 7, 23, 9, 0, 1, tzinfo=CN_TZ
    )
    assert creator_repository.list_calls[0]["available_at"] == cutoff
    assert service.ranking_snapshot_repository.dates == [
        ("2026-07-23", int(cutoff.timestamp()))
    ]


def test_creator_source_uses_previous_day_but_next_morning_availability() -> None:
    cutoff = datetime(2026, 7, 24, 8, 20, tzinfo=CN_TZ)
    first_seen_at = cutoff - timedelta(minutes=30)
    work = build_creator_work(
        cutoff,
        age_hours=11,
        first_seen_at=first_seen_at,
        analyzed_at=first_seen_at,
    )
    creator_repository = FakeCreatorWorkRepository(works=[work])
    verification_repository = FakeCreatorVerificationRepository(
        [build_creator_verification()]
    )
    service = MorningAnalysisService(
        creator_work_repository=creator_repository,
        creator_verification_repository=verification_repository,
        creator_enabled=True,
    )
    publish_start = datetime(2026, 7, 23, 0, 0, tzinfo=CN_TZ)
    publish_end = datetime(2026, 7, 23, 23, 59, 59, tzinfo=CN_TZ)

    context = asyncio.run(
        service._load_creator_context(
            source_date="2026-07-23",
            ranking_market_date="2026-07-23",
            publish_start_ts=int(publish_start.timestamp()),
            publish_end_ts=int(publish_end.timestamp()),
            available_at_ts=int(cutoff.timestamp()),
            creator_limit=5,
            work_limit=3,
        )
    )

    assert context.status == "available"
    assert context.source_date == "2026-07-23"
    assert context.ranking_market_date == "2026-07-23"
    assert context.works[0].published_at.date().isoformat() == "2026-07-23"
    assert creator_repository.list_calls == [
        {
            "creator_id": "creator-1",
            "start_at": publish_start,
            "end_at": publish_end + timedelta(seconds=1),
            "available_at": cutoff,
            "limit": 3,
        }
    ]


def test_creator_context_uses_only_previous_trade_day_top_five_by_rolling_score() -> None:
    cutoff = datetime(2026, 7, 28, 8, 20, tzinfo=CN_TZ)
    publish_start = datetime(2026, 7, 27, 0, 0, tzinfo=CN_TZ)
    publish_end = datetime(2026, 7, 27, 23, 59, 59, tzinfo=CN_TZ)
    scores = [
        ("creator-6", 65.0),
        ("creator-2", 92.0),
        ("creator-4", 75.0),
        ("creator-1", 98.0),
        ("creator-no-score", None),
        ("creator-5", 70.0),
        ("creator-3", 86.0),
    ]
    verification_repository = FakeCreatorVerificationRepository(
        [
            build_creator_verification(
                creator_id,
                creator_name=f"博主{creator_id}",
                rolling_score=score,
                sample_count=5 if score is not None else 0,
            )
            for creator_id, score in scores
        ]
    )
    works = [
        build_creator_work(
            cutoff,
            age_hours=12,
            work_id=f"work-{creator_id}",
            creator_id=creator_id,
            creator_name=f"博主{creator_id}",
        )
        for creator_id, _ in scores
    ]
    creator_repository = FakeCreatorWorkRepository(works=works)
    service = MorningAnalysisService(
        creator_work_repository=creator_repository,
        creator_verification_repository=verification_repository,
    )

    context = asyncio.run(
        service._load_creator_context(
            source_date="2026-07-27",
            ranking_market_date="2026-07-27",
            publish_start_ts=int(publish_start.timestamp()),
            publish_end_ts=int(publish_end.timestamp()),
            available_at_ts=int(cutoff.timestamp()),
            creator_limit=5,
            work_limit=1,
        )
    )

    expected_ids = [
        "creator-1",
        "creator-2",
        "creator-3",
        "creator-4",
        "creator-5",
    ]
    assert context.status == "available"
    assert context.selection_rule == "reliability_adjusted_active_opinions"
    assert [item.creator_id for item in context.ranked_creators] == expected_ids
    assert [item.rank for item in context.ranked_creators] == [1, 2, 3, 4, 5]
    assert [item.creator_id for item in context.works] == expected_ids
    assert all(item.creator_id != "creator-no-score" for item in context.works)
    assert all(item.creator_id != "creator-6" for item in context.works)
    assert verification_repository.calls == [
        {"market_date": "2026-07-27", "status": "completed"}
    ]


def test_creator_context_does_not_query_works_without_verified_scores() -> None:
    cutoff = datetime(2026, 7, 28, 8, 20, tzinfo=CN_TZ)
    verification_repository = FakeCreatorVerificationRepository(
        [
            build_creator_verification(
                "creator-no-score",
                rolling_score=None,
                sample_count=0,
            )
        ]
    )
    creator_repository = FakeCreatorWorkRepository(
        error=AssertionError("没有有效评分时不应查询博主作品")
    )
    service = MorningAnalysisService(
        creator_work_repository=creator_repository,
        creator_verification_repository=verification_repository,
    )

    context = asyncio.run(
        service._load_creator_context(
            source_date="2026-07-27",
            ranking_market_date="2026-07-27",
            publish_start_ts=int(
                datetime(2026, 7, 27, 0, 0, tzinfo=CN_TZ).timestamp()
            ),
            publish_end_ts=int(
                datetime(2026, 7, 27, 23, 59, 59, tzinfo=CN_TZ).timestamp()
            ),
            available_at_ts=int(cutoff.timestamp()),
            creator_limit=5,
            work_limit=3,
        )
    )

    assert context.status == "missing"
    assert "没有可用的博主滚动评分" in context.reason
    assert creator_repository.list_calls == []
    assert creator_repository.latest_calls == []


def test_creator_context_excludes_non_sector_opinions() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    work = build_creator_work(now)
    assert work.analysis is not None
    base_opinion = work.analysis.opinions[0]
    stock_opinion = base_opinion.model_copy(
        update={
            "opinion_id": f"{work.work_key}:stock:1",
            "target_type": "stock",
            "target_name": "贵州茅台",
        }
    )
    work = work.model_copy(
        update={
            "analysis": work.analysis.model_copy(
                update={"opinions": [stock_opinion]}
            )
        }
    )
    service = MorningAnalysisService()
    service.valid_sector_names = frozenset({"半导体"})

    context = service._to_creator_work_context(work)

    assert context.analysis.sector_opinions == []


def test_creator_context_excludes_sector_outside_ths_industry_whitelist() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    work = build_creator_work(now)
    assert work.analysis is not None
    base_opinion = work.analysis.opinions[0]
    unlisted_opinion = base_opinion.model_copy(
        update={
            "opinion_id": f"{work.work_key}:sector:1",
            "target_name": "自定义概念板块",
        }
    )
    work = work.model_copy(
        update={
            "analysis": work.analysis.model_copy(
                update={"opinions": [unlisted_opinion]}
            )
        }
    )
    service = MorningAnalysisService()
    service.valid_sector_names = frozenset({"半导体"})

    context = service._to_creator_work_context(work)

    assert context.analysis.sector_opinions == []


def test_morning_analysis_service_does_not_consume_creator_data_when_disabled() -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    analyzer = FakeAnalyzer()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=FakeReportRepository(),
        ranking_snapshot_repository=FakeRankingSnapshotRepository(
            build_snapshot(now, status_counts={"finished": 10})
        ),
        creator_work_repository=FakeCreatorWorkRepository(
            error=AssertionError("关闭后不应查询抖音作品")
        ),
        creator_enabled=False,
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now))

    assert result.report is not None
    assert result.report.data_quality == "degraded"
    assert result.report.creator_context.status == "missing"
    assert result.report.creator_context.reason == "博主观点功能未启用"


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("missing", "missing"),
        ("invalid", "invalid"),
        ("future_analysis", "invalid"),
        ("fetch_failed", "fetch_failed"),
    ],
)
def test_morning_analysis_service_degrades_unavailable_creator_context(
    scenario: str,
    expected_status: str,
) -> None:
    now = datetime(2026, 7, 23, 8, 20, tzinfo=CN_TZ)
    if scenario == "invalid":
        creator_repository = FakeCreatorWorkRepository(
            works=[
                build_creator_work(
                    now,
                    first_seen_at=now + timedelta(seconds=1),
                )
            ]
        )
    elif scenario == "future_analysis":
        creator_repository = FakeCreatorWorkRepository(
            works=[
                build_creator_work(
                    now,
                    analyzed_at=now + timedelta(seconds=1),
                )
            ]
        )
    elif scenario == "fetch_failed":
        creator_repository = FakeCreatorWorkRepository(error=RuntimeError(" "))
    else:
        creator_repository = FakeCreatorWorkRepository()

    analyzer = FakeAnalyzer()
    decision = MorningTradeDateDecision(
        reference_date="2026-07-23",
        analysis_date="2026-07-23",
        prev_trade_date="2026-07-22",
        is_current_trade_day=True,
    )
    service = MorningAnalysisService(
        report_repository=FakeReportRepository(),
        ranking_snapshot_repository=FakeRankingSnapshotRepository(
            build_snapshot(now, status_counts={"finished": 10})
        ),
        creator_work_repository=creator_repository,
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now))

    assert result.report is not None
    assert result.report.creator_context.status == expected_status
    assert result.report.creator_context.reason
    assert result.report.data_quality == "degraded"
    assert analyzer.inputs["creator_context"].status == expected_status
    if expected_status == "fetch_failed":
        assert result.report.creator_context.reason == "RuntimeError"


def test_creator_context_does_not_fallback_to_older_work() -> None:
    """来源日没有作品时只保留 Top 5 排名，不读取更早日期作品。"""

    cutoff = datetime(2026, 7, 29, 8, 20, tzinfo=CN_TZ)
    creator_repository = FakeCreatorWorkRepository(
        latest=build_creator_work(cutoff, age_hours=120)
    )
    service = MorningAnalysisService(
        creator_work_repository=creator_repository,
        creator_verification_repository=FakeCreatorVerificationRepository(
            [build_creator_verification()]
        ),
    )

    context = asyncio.run(
        service._load_creator_context(
            source_date="2026-07-28",
            ranking_market_date="2026-07-28",
            publish_start_ts=int(
                datetime(2026, 7, 28, 0, 0, tzinfo=CN_TZ).timestamp()
            ),
            publish_end_ts=int(
                datetime(2026, 7, 28, 23, 59, 59, tzinfo=CN_TZ).timestamp()
            ),
            available_at_ts=int(cutoff.timestamp()),
            creator_limit=5,
            work_limit=3,
        )
    )

    assert context.status == "missing"
    assert context.works == []
    assert context.ranked_creators
    assert creator_repository.latest_calls == []


def test_empty_newest_work_does_not_consume_creator_opinion_quota() -> None:
    """验证没有结构化预测的新作品不会挤掉稍早但仍有效的观点。"""

    cutoff = datetime(2026, 7, 29, 8, 20, tzinfo=CN_TZ)
    ranking = MorningAnalysisService._ranked_creator_contexts(
        [build_creator_verification(sample_count=5)],
        limit=1,
        as_of_date=cutoff.date(),
    )[0]
    newest = build_creator_work(cutoff, age_hours=1, work_id="empty")
    assert newest.analysis is not None
    newest = newest.model_copy(
        update={"analysis": newest.analysis.model_copy(update={"opinions": []})}
    )
    meaningful = build_creator_work(cutoff, age_hours=10, work_id="meaningful")
    service = MorningAnalysisService()

    selected = service._select_creator_work_contexts(
        [(newest, ranking), (meaningful, ranking)],
        available_at=cutoff,
        per_creator_limit=1,
        global_limit=30,
    )

    assert [item.work_id for item in selected] == ["douyin:meaningful"]
    assert len(selected[0].analysis.structured_opinions) == 1


def test_sector_suffix_is_normalized_without_mapping_broad_theme() -> None:
    service = MorningAnalysisService()

    assert service._normalize_sector_name("半导体板块") == "半导体"
    assert service._normalize_sector_name("大科技") is None


def test_resolve_morning_trade_dates_uses_current_and_previous_session() -> None:
    class FakeCalendar:
        def is_session(self, value):
            return value == pd.Timestamp("2026-07-23")

        def date_to_session(self, value, direction):
            raise AssertionError("交易日不应回退")

        def previous_session(self, value):
            return pd.Timestamp("2026-07-22")

    result = resolve_morning_trade_dates(
        date(2026, 7, 23),
        calendar=FakeCalendar(),
    )

    assert result.analysis_date == "2026-07-23"
    assert result.prev_trade_date == "2026-07-22"
    assert result.is_current_trade_day is True


def test_resolve_morning_trade_dates_marks_non_session() -> None:
    class FakeCalendar:
        def is_session(self, value):
            return False

        def date_to_session(self, value, direction):
            assert direction == "previous"
            return pd.Timestamp("2026-07-24")

        def previous_session(self, value):
            return pd.Timestamp("2026-07-23")

    result = resolve_morning_trade_dates(
        date(2026, 7, 25),
        calendar=FakeCalendar(),
    )

    assert result.analysis_date == "2026-07-24"
    assert result.prev_trade_date == "2026-07-23"
    assert result.is_current_trade_day is False
