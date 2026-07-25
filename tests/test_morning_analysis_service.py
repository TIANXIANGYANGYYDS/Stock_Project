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
from app.models.news_ranking_snapshot import (
    NewsRankingFormulaVersions,
    NewsRankingSnapshot,
    NewsRankingSourceStats,
)
from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinSectorOpinion,
    DouyinTranscript,
    DouyinWorkAnalysis,
    DouyinWorkStatus,
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

    async def list_finished_for_morning(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.works

    async def find_latest_finished_before(self, **kwargs):
        self.latest_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.latest


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
) -> DouyinCreatorWork:
    published_at = now - timedelta(hours=age_hours)
    first_seen_at = first_seen_at or published_at
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
        first_seen_at=first_seen_at,
        fetched_at=first_seen_at,
        status=DouyinWorkStatus(status="finished"),
        transcript=DouyinTranscript(
            text="原始转写文本。",
            provider="test-asr",
            model="test-model",
            transcribed_at=first_seen_at,
        ),
        analysis=DouyinWorkAnalysis(
            summary="博主认为半导体有增量催化。",
            sector_opinions=[
                DouyinSectorOpinion(
                    opinion_id=f"{work_id}:半导体",
                    sector_name="半导体",
                    stance_score=70,
                    reason="产业政策可能形成增量预期。",
                )
            ],
            analysis_version="douyin_creator_analysis_v1",
            analysis_model="test-model",
            analyzed_at=analyzed_at or first_seen_at,
        ),
    )


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
        publish_time="2026-07-23 08:59:00",
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
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
    snapshot_repository = FakeRankingSnapshotRepository(build_snapshot(now))
    report_repository = FakeReportRepository()
    morning_crawler = FakeMorningCrawler()
    review_crawler = FakeReviewCrawler()
    analyzer = FakeAnalyzer()
    creator_repository = FakeCreatorWorkRepository(works=[build_creator_work(now)])
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
        creator_sec_uid="creator-1",
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
    assert result.report.prompt_version == "morning_analysis_v3"
    assert result.report.analysis_model == ""
    assert result.report.thinking_enabled is False
    assert result.report.creator_context.status == "available"
    assert result.report.creator_context.priority == "critical"
    assert result.report.creator_context.source_date == "2026-07-22"
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
            "creator_sec_uid": "creator-1",
            "start_ts": int(datetime(2026, 7, 22, 0, 0, tzinfo=CN_TZ).timestamp()),
            "end_ts": int(datetime(2026, 7, 22, 23, 59, 59, tzinfo=CN_TZ).timestamp()),
            "available_at_ts": int(now.timestamp()),
            "limit": 3,
        }
    ]
    assert creator_repository.latest_calls == []


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
        creator_sec_uid="creator-1",
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
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
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
        creator_sec_uid="creator-1",
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=FakeAnalyzer(),
        trade_date_resolver=lambda value: decision,
    )

    with pytest.raises(RuntimeError, match="缺少.*新闻榜单快照"):
        asyncio.run(service.run(reference_datetime=now))

    assert report_repository.reports == []


def test_morning_analysis_service_marks_stale_snapshot_degraded() -> None:
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
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
        creator_sec_uid="creator-1",
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
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
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
        creator_sec_uid="creator-1",
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
        creator_enabled=True,
        creator_sec_uid="creator-1",
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
    assert creator_repository.list_calls[0]["end_ts"] == int(
        datetime(2026, 7, 22, 23, 59, 59, tzinfo=CN_TZ).timestamp()
    )
    assert creator_repository.list_calls[0]["available_at_ts"] == int(
        cutoff.timestamp()
    )
    assert service.ranking_snapshot_repository.dates == [
        ("2026-07-23", int(cutoff.timestamp()))
    ]


def test_creator_source_uses_previous_day_but_next_morning_availability() -> None:
    cutoff = datetime(2026, 7, 24, 9, 0, tzinfo=CN_TZ)
    first_seen_at = cutoff - timedelta(minutes=30)
    work = build_creator_work(
        cutoff,
        age_hours=11,
        first_seen_at=first_seen_at,
        analyzed_at=first_seen_at,
    )
    creator_repository = FakeCreatorWorkRepository(works=[work])
    service = MorningAnalysisService(
        creator_work_repository=creator_repository,
        creator_enabled=True,
        creator_sec_uid="creator-1",
    )
    publish_start = datetime(2026, 7, 23, 0, 0, tzinfo=CN_TZ)
    publish_end = datetime(2026, 7, 23, 23, 59, 59, tzinfo=CN_TZ)

    context = asyncio.run(
        service._load_creator_context(
            source_date="2026-07-23",
            publish_start_ts=int(publish_start.timestamp()),
            publish_end_ts=int(publish_end.timestamp()),
            available_at_ts=int(cutoff.timestamp()),
            max_age_hours=96,
            limit=3,
        )
    )

    assert context.status == "available"
    assert context.source_date == "2026-07-23"
    assert context.works[0].published_at.date().isoformat() == "2026-07-23"
    assert creator_repository.list_calls == [
        {
            "creator_sec_uid": "creator-1",
            "start_ts": int(publish_start.timestamp()),
            "end_ts": int(publish_end.timestamp()),
            "available_at_ts": int(cutoff.timestamp()),
            "limit": 3,
        }
    ]


def test_morning_analysis_service_does_not_consume_creator_data_when_disabled() -> None:
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
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
        creator_sec_uid="creator-1",
        morning_crawler=FakeMorningCrawler(),
        review_crawler=FakeReviewCrawler(),
        analyzer=analyzer,
        trade_date_resolver=lambda value: decision,
    )

    result = asyncio.run(service.run(reference_datetime=now))

    assert result.report is not None
    assert result.report.data_quality == "degraded"
    assert result.report.creator_context.status == "missing"
    assert result.report.creator_context.reason == "抖音博主观点功能未启用"


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("missing", "missing"),
        ("stale", "stale"),
        ("invalid", "invalid"),
        ("future_analysis", "invalid"),
        ("fetch_failed", "fetch_failed"),
    ],
)
def test_morning_analysis_service_degrades_unavailable_creator_context(
    scenario: str,
    expected_status: str,
) -> None:
    now = datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ)
    if scenario == "stale":
        creator_repository = FakeCreatorWorkRepository(
            latest=build_creator_work(now, age_hours=120)
        )
    elif scenario == "invalid":
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
        creator_sec_uid="creator-1",
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
    if expected_status == "stale":
        assert result.report.creator_context.works
    if expected_status == "fetch_failed":
        assert result.report.creator_context.reason == "RuntimeError"


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
