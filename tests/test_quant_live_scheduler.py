from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import app.scheduler.quant_jobs as quant_jobs


CN_TIME = "+08:00"


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, **kwargs):
        self.jobs.append((func, kwargs))
        return type("Job", (), {"id": kwargs["id"]})()


def test_quant_live_jobs_are_registered_with_safe_single_instance_policy() -> None:
    scheduler = FakeScheduler()

    quant_jobs.register_quant_live_jobs(scheduler)

    jobs = {kwargs["id"]: kwargs for _, kwargs in scheduler.jobs}
    assert set(jobs) == {
        quant_jobs.QUANT_LIVE_PREPARE_JOB_ID,
        quant_jobs.QUANT_LIVE_MORNING_JOB_ID,
        quant_jobs.QUANT_LIVE_AFTERNOON_JOB_ID,
        quant_jobs.QUANT_LIVE_STARTUP_JOB_ID,
    }
    assert all(job["max_instances"] == 1 for job in jobs.values())
    assert all(job["coalesce"] is True for job in jobs.values())
    assert all(job["replace_existing"] is True for job in jobs.values())


def test_refresh_outside_market_window_does_not_construct_service(monkeypatch) -> None:
    def fail_if_constructed():
        raise AssertionError("outside window must not construct the service")

    monkeypatch.setattr(quant_jobs, "QuantLiveService", fail_if_constructed)

    result = asyncio.run(
        quant_jobs.refresh_quant_live_job(
            now=datetime.fromisoformat(f"2026-09-03T12:00:00{CN_TIME}")
        )
    )

    assert result == {"status": "skipped", "reason": "outside_refresh_window"}


def test_refresh_failure_is_persisted_without_replacing_last_snapshot(
    monkeypatch,
) -> None:
    recorded = []

    class Results:
        async def record_runtime_error(self, **kwargs):
            recorded.append(kwargs)

    class FailingService:
        def __init__(self):
            self.results = Results()

        async def process(self, *, now):
            raise RuntimeError("量化观察代码超过内存安全上限")

    monkeypatch.setattr(quant_jobs, "QuantLiveService", FailingService)

    with pytest.raises(RuntimeError, match="内存安全上限"):
        asyncio.run(
            quant_jobs.refresh_quant_live_job(
                now=datetime.fromisoformat(f"2026-09-03T10:00:20{CN_TIME}")
            )
        )

    assert recorded[0]["trade_date"] == "2026-09-03"
    assert "RuntimeError" in recorded[0]["error"]


def test_startup_refresh_can_finalize_after_regular_job_window(monkeypatch) -> None:
    processed = []

    class Service:
        def __init__(self):
            self.results = object()

        async def process(self, *, now):
            processed.append(now)
            return {"status": "closed", "trade_date": "2026-09-03"}

    monkeypatch.setattr(quant_jobs, "QuantLiveService", Service)

    result = asyncio.run(
        quant_jobs.refresh_quant_live_job(
            now=datetime.fromisoformat(f"2026-09-03T18:00:00{CN_TIME}"),
            allow_outside_window=True,
        )
    )

    assert result["status"] == "closed"
    assert len(processed) == 1
