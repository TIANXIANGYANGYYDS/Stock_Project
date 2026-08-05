from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace

from app.scheduler import morning_analysis_jobs


def test_register_morning_analysis_job_uses_shanghai_time() -> None:
    registered = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            registered.append((func, kwargs))
            return SimpleNamespace(id=kwargs["id"])

    morning_analysis_jobs.register_morning_analysis_job(FakeScheduler())

    assert len(registered) == 1
    _, options = registered[0]
    assert options["id"] == "morning_market_analysis"
    assert options["replace_existing"] is True
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert str(options["trigger"].timezone) == "Asia/Shanghai"
    assert str(options["trigger"]) == (
        "cron[day_of_week='mon-fri', hour='8', minute='20']"
    )


def test_run_morning_analysis_passes_snapshot_age_limit(monkeypatch) -> None:
    calls = []

    class FakeMorningAnalysisService:
        async def run(self, **kwargs):
            calls.append(kwargs)
            return "report"

    fake_module = ModuleType("app.services.morning_analysis_service")
    fake_module.MorningAnalysisService = FakeMorningAnalysisService
    fake_module.MORNING_ANALYSIS_RANKING_LIMIT = 12
    fake_module.MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES = 15
    fake_module.MORNING_ANALYSIS_CREATOR_LIMIT = 5
    fake_module.MORNING_ANALYSIS_CREATOR_WORK_LIMIT = 3
    monkeypatch.setitem(
        sys.modules,
        "app.services.morning_analysis_service",
        fake_module,
    )
    reference_datetime = datetime(2026, 7, 23, 8, 20)

    result = asyncio.run(
        morning_analysis_jobs._run_morning_analysis(
            reference_datetime=reference_datetime,
        )
    )

    assert result == "report"
    assert calls == [
            {
                "reference_datetime": reference_datetime,
                "persist": True,
                "ranking_limit": 12,
            "max_snapshot_age_minutes": 15,
            "creator_limit": 5,
            "creator_work_limit": 3,
        }
    ]
