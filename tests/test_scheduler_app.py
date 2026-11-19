from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.scheduler import scheduler_app as module
from app.scheduler.scheduler_app import JOB_ID, build_scheduler


def test_scheduler_registers_only_the_unified_creator_feed() -> None:
    scheduler = build_scheduler()
    try:
        job_ids = {job.id for job in scheduler.get_jobs()}

        assert {
            "creator_monitoring_ingestion",
            "morning_market_analysis",
        } <= job_ids
        assert "douyin_creator_ingestion" not in job_ids
        assert isinstance(scheduler._jobstores["default"], MongoDBJobStore)
    finally:
        scheduler._jobstores["default"].shutdown()


def test_all_persisted_jobs_are_replaceable_on_restart() -> None:
    scheduler = build_scheduler()
    try:
        replace_by_id = {
            job.id: replace_existing
            for job, _, replace_existing in scheduler._pending_jobs
        }

        assert replace_by_id[JOB_ID] is True
        assert all(replace_by_id.values())
    finally:
        scheduler._jobstores["default"].shutdown()


def test_scheduler_uses_explicit_shanghai_timezone(monkeypatch) -> None:
    monkeypatch.setattr('apscheduler.schedulers.base.get_localzone', lambda: timezone.utc)
    scheduler = build_scheduler()
    try:
        assert str(scheduler.timezone) == 'Asia/Shanghai'
        news_job = next(job for job in scheduler.get_jobs() if job.id == JOB_ID)
        assert news_job.next_run_time.tzinfo is not None
    finally:
        scheduler._jobstores["default"].shutdown()


def test_startup_removes_only_retired_jobs_before_any_execution(monkeypatch):
    calls = []
    stop_events = []

    async def noop(): pass
    async def retired(): calls.append('retired')
    async def active():
        calls.append('active')
        assert scheduler.get_job('stock_fund_flow_5m') is None
        stop_events[0].set()

    scheduler = AsyncIOScheduler(timezone='Asia/Shanghai')
    now = datetime.now(timezone.utc)
    scheduler.add_job(retired, 'date', run_date=now, id='stock_fund_flow_5m')
    scheduler.add_job(active, 'date', run_date=now, id='stock_fund_flow_5s')
    monkeypatch.setattr(module, 'configure_logging', lambda: None)
    monkeypatch.setattr(module, 'ensure_news_indexes', noop)
    monkeypatch.setattr(module, 'refresh_news_ranking_snapshot_on_startup', noop)
    monkeypatch.setattr(module, 'build_scheduler', lambda: scheduler)
    monkeypatch.setattr(module, 'install_signal_handlers', stop_events.append)
    monkeypatch.setattr(module, 'client', SimpleNamespace(close=lambda: None))
    async def heartbeat(stop_event): await stop_event.wait()
    monkeypatch.setattr(module, 'heartbeat', heartbeat)
    monkeypatch.setattr('app.scheduler.fund_flow_jobs.close_fund_flow_service', noop)

    async def run():
        await asyncio.wait_for(module.run_scheduler(), timeout=3)
        assert calls == ['active']
    asyncio.run(run())
