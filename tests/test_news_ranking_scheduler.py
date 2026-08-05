from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace

from app.scheduler import news_ranking_jobs
from app.scheduler import scheduler_app
from app.services.news_ranking_snapshot_service import (
    NEWS_RANKING_LIMIT,
    NEWS_RANKING_WINDOW_HOURS,
)


def test_news_ranking_uses_fixed_business_constants() -> None:
    assert news_ranking_jobs.NEWS_RANKING_INTERVAL_MINUTES == 5
    assert NEWS_RANKING_WINDOW_HOURS == 72
    assert NEWS_RANKING_LIMIT == 12


def test_register_news_ranking_job_starts_after_interval_and_runs_serially(
) -> None:
    registered = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            registered.append((func, kwargs))
            return SimpleNamespace(id=kwargs["id"])

    news_ranking_jobs.register_news_ranking_job(FakeScheduler())

    assert len(registered) == 1
    func, options = registered[0]
    assert func is news_ranking_jobs.news_ranking_job
    assert options["trigger"] == "interval"
    assert options["minutes"] == 5
    assert options["id"] == news_ranking_jobs.NEWS_RANKING_JOB_ID
    assert options["replace_existing"] is True
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert "next_run_time" not in options


def test_manual_ranking_helper_passes_fixed_values_and_reference_time(monkeypatch) -> None:
    calls = []

    class FakeSnapshotService:
        async def run(self, **kwargs):
            calls.append(kwargs)
            return "snapshot"

    fake_module = ModuleType("app.services.news_ranking_snapshot_service")
    fake_module.NewsRankingSnapshotService = FakeSnapshotService
    fake_module.NEWS_RANKING_WINDOW_HOURS = 72
    fake_module.NEWS_RANKING_LIMIT = 12
    monkeypatch.setitem(
        sys.modules,
        "app.services.news_ranking_snapshot_service",
        fake_module,
    )
    reference_datetime = datetime(2026, 7, 23, 8, 55)

    result = asyncio.run(
        news_ranking_jobs.run_news_ranking_snapshot(
            reference_datetime=reference_datetime,
        )
    )

    assert result == "snapshot"
    assert calls == [
        {
            "reference_datetime": reference_datetime,
            "window_hours": 72,
            "ranking_limit": 12,
                "morning_cutoff_hour": 8,
                "morning_cutoff_minute": 20,
        }
    ]


def test_scheduler_waits_for_initial_ranking_snapshot(monkeypatch) -> None:
    calls = []

    async def fake_run_news_ranking_snapshot():
        calls.append("ranking")

    monkeypatch.setattr(
        scheduler_app,
        "run_news_ranking_snapshot",
        fake_run_news_ranking_snapshot,
    )

    asyncio.run(scheduler_app.refresh_news_ranking_snapshot_on_startup())

    assert calls == ["ranking"]
