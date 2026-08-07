from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

from app.scheduler import morning_analysis_jobs


def test_register_morning_analysis_job_uses_shanghai_time() -> None:
    registered = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            registered.append((func, kwargs))
            return SimpleNamespace(id=kwargs["id"])

    morning_analysis_jobs.register_morning_analysis_job(FakeScheduler())

    assert len(registered) == 3
    options_by_id = {options["id"]: options for _, options in registered}
    options = options_by_id["morning_market_analysis"]
    assert options["id"] == "morning_market_analysis"
    assert options["replace_existing"] is True
    assert options["max_instances"] == 1
    assert options["coalesce"] is True
    assert str(options["trigger"].timezone) == "Asia/Shanghai"
    assert str(options["trigger"]) == (
        "cron[day_of_week='mon-fri', hour='8', minute='20']"
    )
    retry_options = options_by_id["morning_market_analysis_retry"]
    assert str(retry_options["trigger"]) == (
        "cron[day_of_week='mon-fri', hour='8', minute='40,55']"
    )
    startup_options = options_by_id["morning_market_analysis_startup_catchup"]
    assert startup_options["trigger"].run_date.tzinfo is not None


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


def test_morning_analysis_catchup_skips_existing_report(monkeypatch) -> None:
    from app.repositories import daily_market_analysis_repository
    from app.services import trading_calendar_service

    class FakeRepository:
        async def exists(self, filters):
            assert filters == {"analysis_date": "2026-07-23"}
            return True

    monkeypatch.setattr(
        daily_market_analysis_repository,
        "DailyMarketAnalysisRepository",
        lambda: FakeRepository(),
    )
    monkeypatch.setattr(
        trading_calendar_service,
        "resolve_morning_trade_dates",
        lambda value: SimpleNamespace(
            reference_date=value.isoformat(),
            analysis_date="2026-07-23",
            is_current_trade_day=True,
        ),
    )

    async def fail_if_called(**kwargs):
        raise AssertionError("existing report must not trigger a rerun")

    monkeypatch.setattr(morning_analysis_jobs, "_run_morning_analysis", fail_if_called)
    result = asyncio.run(
        morning_analysis_jobs._run_morning_analysis_if_missing(
            reference_datetime=datetime(
                2026,
                7,
                23,
                8,
                55,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            job_name="test_catchup",
        )
    )

    assert result is None


def test_morning_analysis_catchup_runs_missing_current_day_report(monkeypatch) -> None:
    from app.repositories import daily_market_analysis_repository
    from app.services import trading_calendar_service

    class FakeRepository:
        async def exists(self, filters):
            assert filters == {"analysis_date": "2026-07-23"}
            return False

    monkeypatch.setattr(
        daily_market_analysis_repository,
        "DailyMarketAnalysisRepository",
        lambda: FakeRepository(),
    )
    monkeypatch.setattr(
        trading_calendar_service,
        "resolve_morning_trade_dates",
        lambda value: SimpleNamespace(
            reference_date=value.isoformat(),
            analysis_date="2026-07-23",
            is_current_trade_day=True,
        ),
    )
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return "report"

    monkeypatch.setattr(morning_analysis_jobs, "_run_morning_analysis", fake_run)
    reference = datetime(
        2026,
        7,
        23,
        8,
        55,
        tzinfo=timezone(timedelta(hours=8)),
    )
    result = asyncio.run(
        morning_analysis_jobs._run_morning_analysis_if_missing(
            reference_datetime=reference,
            job_name="test_catchup",
        )
    )

    assert result == "report"
    assert calls == [{"reference_datetime": reference}]
