from __future__ import annotations

from app.scheduler.scheduler_app import build_scheduler


def test_scheduler_registers_only_the_unified_creator_feed() -> None:
    scheduler = build_scheduler()

    job_ids = {job.id for job in scheduler.get_jobs()}

    assert {
        "creator_monitoring_ingestion",
        "morning_market_analysis",
    } <= job_ids
    assert "douyin_creator_ingestion" not in job_ids
