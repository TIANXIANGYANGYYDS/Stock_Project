from __future__ import annotations

from apscheduler.jobstores.mongodb import MongoDBJobStore

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
