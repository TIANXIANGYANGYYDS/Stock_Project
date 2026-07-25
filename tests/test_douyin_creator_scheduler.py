from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.scheduler import douyin_creator_jobs


def test_register_douyin_job_uses_fixed_interval() -> None:
    calls = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(id=kwargs["id"])

    douyin_creator_jobs.register_douyin_creator_job(FakeScheduler())
    assert calls[0]["id"] == "douyin_creator_ingestion"
    assert calls[0]["minutes"] == 15
    assert calls[0]["max_instances"] == 1


def test_douyin_job_propagates_failure_to_scheduler(monkeypatch) -> None:
    async def fail():
        raise RuntimeError("crawl failed")

    monkeypatch.setattr(douyin_creator_jobs, "run_douyin_creator_ingestion", fail)

    with pytest.raises(RuntimeError, match="crawl failed"):
        asyncio.run(douyin_creator_jobs.douyin_creator_ingestion_job())
