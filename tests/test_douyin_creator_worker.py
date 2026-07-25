from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.workers import douyin_creator_analysis_worker as worker_module


def test_worker_rejects_explicit_zero_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        worker_module.DouyinCreatorAnalysisWorker(
            service=SimpleNamespace(),  # type: ignore[arg-type]
            batch_size=0,
        )


def test_direct_worker_entry_runs_fixed_enabled_pipeline(monkeypatch) -> None:
    service = SimpleNamespace()
    calls = []

    async def fake_run_worker_process(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(worker_module, "DouyinCreatorAnalysisService", lambda: service)
    monkeypatch.setattr(worker_module, "run_worker_process", fake_run_worker_process)

    asyncio.run(worker_module.run_worker())

    assert calls[0]["service"] is service
    assert calls[0]["worker"].batch_size == 1
    assert calls[0]["worker"].idle_sleep_seconds == 180
    assert calls[0]["worker"].error_sleep_seconds == 10
