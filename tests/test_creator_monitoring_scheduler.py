from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.scheduler import creator_monitoring_jobs


class FakeScheduler:
    def __init__(self) -> None:
        self.calls = []

    def add_job(self, func, **kwargs):
        self.calls.append({"func": func, **kwargs})
        return SimpleNamespace(id=kwargs["id"])


def test_register_creator_monitoring_jobs_adds_ingestion_and_verifications() -> None:
    """验证调度包含采集、Cookie 到期检查和两次收盘验证。"""

    scheduler = FakeScheduler()
    creator_monitoring_jobs.register_creator_monitoring_jobs(scheduler)

    assert [item["id"] for item in scheduler.calls] == [
        "creator_monitoring_ingestion",
        "douyin_session_cookie_expiry_check",
        "creator_daily_verification",
        "creator_daily_verification_retry",
    ]
    assert str(scheduler.calls[0]["trigger"]) == "cron[hour='*', minute='0']"
    assert all(item["max_instances"] == 1 for item in scheduler.calls)
    assert all(item["coalesce"] is True for item in scheduler.calls)
    assert scheduler.calls[1]["func"] is (
        creator_monitoring_jobs.check_douyin_session_cookie_expiry
    )
    assert str(scheduler.calls[1]["trigger"]) == "cron[hour='9', minute='5']"
    assert scheduler.calls[2]["func"] is (
        creator_monitoring_jobs.creator_daily_verification_job
    )
    assert scheduler.calls[3]["func"] is (
        creator_monitoring_jobs.creator_daily_verification_job
    )
    assert str(scheduler.calls[2]["trigger"]) == (
        "cron[day_of_week='mon-fri', hour='15', minute='40']"
    )
    assert str(scheduler.calls[3]["trigger"]) == (
        "cron[day_of_week='mon-fri', hour='16', minute='30']"
    )


def test_cookie_expiry_check_warns_without_logging_cookie(
    monkeypatch, caplog
) -> None:
    """验证临期告警包含处置时间但绝不包含 Cookie 值。"""

    cookie = (
        "sessionid=opaque-session; sid_guard="
        + quote("opaque|1785413378|5184000|expiry-label")
    )
    secret = SimpleNamespace(get_secret_value=lambda: cookie)
    monkeypatch.setattr(
        creator_monitoring_jobs,
        "get_settings",
        lambda: SimpleNamespace(douyin_session_cookie=secret),
    )

    with caplog.at_level(logging.WARNING):
        creator_monitoring_jobs.check_douyin_session_cookie_expiry(
            reference_datetime=datetime(
                2026,
                9,
                23,
                12,
                tzinfo=creator_monitoring_jobs.CN_TZ,
            )
        )

    assert "douyin_session_cookie_expiring" in caplog.text
    assert "2026-09-28 20:09:38" in caplog.text
    assert "opaque" not in caplog.text


def test_creator_ingestion_job_propagates_failure(monkeypatch) -> None:
    async def fail():
        raise RuntimeError("crawl failed")

    monkeypatch.setattr(creator_monitoring_jobs, "run_creator_ingestion", fail)
    with pytest.raises(RuntimeError, match="crawl failed"):
        asyncio.run(creator_monitoring_jobs.creator_ingestion_job())


def test_creator_ingestion_job_logs_partial_and_detail_failures(
    monkeypatch, caplog
) -> None:
    async def completed():
        return SimpleNamespace(
            results=(object(), object()),
            inserted_count=3,
            failed_account_count=1,
            partial_account_count=4,
            detail_failed_count=12,
        )

    monkeypatch.setattr(creator_monitoring_jobs, "run_creator_ingestion", completed)
    with caplog.at_level(logging.INFO):
        asyncio.run(creator_monitoring_jobs.creator_ingestion_job())

    assert "partial_accounts=4" in caplog.text
    assert "detail_failed=12" in caplog.text


def test_scheduler_reference_datetime_requires_timezone() -> None:
    with pytest.raises(ValueError, match="时区"):
        creator_monitoring_jobs._reference_datetime(datetime(2026, 7, 24, 19))
