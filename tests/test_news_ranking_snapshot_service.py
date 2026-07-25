from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.daily_market_analysis import SectorRankingItem
from app.services.news_ranking_snapshot_service import NewsRankingSnapshotService


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class FakeNewsRepository:
    def __init__(self) -> None:
        self.windows: list[tuple[int, int]] = []
        self.ranking_documents = [
            {"event_id": "finished", "status": {"status": "finished"}}
        ]

    async def list_news_for_ranking_window(self, *, start_ts, end_ts):
        self.windows.append((start_ts, end_ts))
        return [
            *self.ranking_documents,
            {"event_id": "judged", "status": {"status": "sector_judged"}},
            {"event_id": "crawled-1", "status": {"status": "crawled"}},
            {"event_id": "crawled-2", "status": {"status": "crawled"}},
        ]


class FakeSnapshotRepository:
    def __init__(self) -> None:
        self.index_calls = 0
        self.snapshots = []
        self.prune_calls = []

    async def create_indexes(self):
        self.index_calls += 1

    async def upsert_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    async def prune_redundant_day_snapshots(self, **kwargs):
        self.prune_calls.append(kwargs)


class FakeRankingService:
    def __init__(self) -> None:
        self.calls = []

    def build_rankings(self, documents, **kwargs):
        self.calls.append(("both", documents, kwargs))
        investment = [
            SectorRankingItem(
                rank=1,
                sector_name="半导体",
                final_score=80,
                news_count=1,
            )
        ]

        heat = [
            SectorRankingItem(
                rank=1,
                sector_name="通信设备",
                final_score=70,
                news_count=1,
            )
        ]
        return investment, heat, len(documents)


def test_snapshot_service_builds_and_publishes_one_consistent_snapshot() -> None:
    now = datetime(2026, 7, 23, 8, 58, tzinfo=CN_TZ)
    news_repository = FakeNewsRepository()
    snapshot_repository = FakeSnapshotRepository()
    ranking_service = FakeRankingService()
    service = NewsRankingSnapshotService(
        news_repository=news_repository,
        snapshot_repository=snapshot_repository,
        ranking_service=ranking_service,
    )

    snapshot = asyncio.run(
        service.run(reference_datetime=now, window_hours=72, ranking_limit=12)
    )

    end_ts = int(now.timestamp())
    start_ts = end_ts - 72 * 3600
    assert snapshot.snapshot_id == f"2026-07-23_{end_ts}"
    assert snapshot.biz_date == "2026-07-23"
    assert snapshot.window_type == "rolling_72h"
    assert snapshot.window_start_ts == start_ts
    assert snapshot.window_end_ts == end_ts
    assert snapshot.source_stats.total_news_count == 4
    assert snapshot.source_stats.investment_eligible_count == 1
    assert snapshot.source_stats.heat_eligible_count == 1
    assert snapshot.formula_versions.investment == "investment_v3"
    assert snapshot.formula_versions.heat == "heat_v4"
    assert snapshot.investment_ranking[0].sector_name == "半导体"
    assert snapshot.heat_ranking[0].sector_name == "通信设备"
    assert snapshot_repository.index_calls == 1
    assert snapshot_repository.snapshots == [snapshot]
    assert snapshot_repository.prune_calls == []
    assert news_repository.windows == [(start_ts, end_ts)]
    assert ranking_service.calls == [
        (
            "both",
            news_repository.ranking_documents,
            {"as_of_ts": end_ts, "limit": 12},
        )
    ]


@pytest.mark.parametrize(
    ("window_hours", "ranking_limit", "message"),
    [(0, 12, "window_hours"), (72, 0, "ranking_limit")],
)
def test_snapshot_service_rejects_invalid_limits(
    window_hours: int,
    ranking_limit: int,
    message: str,
) -> None:
    service = NewsRankingSnapshotService(
        news_repository=FakeNewsRepository(),
        snapshot_repository=FakeSnapshotRepository(),
        ranking_service=FakeRankingService(),
    )

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            service.run(
                reference_datetime=datetime(2026, 7, 23, tzinfo=CN_TZ),
                window_hours=window_hours,
                ranking_limit=ranking_limit,
            )
        )


def test_snapshot_service_preserves_the_configured_morning_cutoff() -> None:
    now = datetime(2026, 7, 23, 14, 0, tzinfo=CN_TZ)
    snapshot_repository = FakeSnapshotRepository()
    service = NewsRankingSnapshotService(
        news_repository=FakeNewsRepository(),
        snapshot_repository=snapshot_repository,
        ranking_service=FakeRankingService(),
    )

    asyncio.run(
        service.run(
            reference_datetime=now,
            morning_cutoff_hour=9,
            morning_cutoff_minute=0,
        )
    )

    assert snapshot_repository.prune_calls == [
        {
            "biz_date": "2026-07-23",
            "morning_cutoff_ts": int(
                datetime(2026, 7, 23, 9, 0, tzinfo=CN_TZ).timestamp()
            ),
        }
    ]
