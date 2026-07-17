import asyncio
from types import SimpleNamespace

from app.scheduler import crawler_jobs
from app.services import stock_daily_detail_service as daily_service


def test_registers_main_and_automatic_compensation_jobs() -> None:
    registered = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            registered.append((func, kwargs))
            return SimpleNamespace(id=kwargs["id"])

    crawler_jobs.register_stock_daily_detail_job(FakeScheduler())

    jobs = {kwargs["id"]: kwargs for _, kwargs in registered}
    assert set(jobs) == {
        "sync_stock_daily_detail_1630",
        "sync_stock_daily_detail_startup",
        "sync_stock_daily_detail_audit_1530",
    }
    assert jobs["sync_stock_daily_detail_audit_1530"]["kwargs"] == {
        "target_scope": "previous",
        "max_automatic_compensations": 3,
    }


def test_main_job_compensates_existing_incomplete_run(monkeypatch) -> None:
    calls = []

    async def fake_resolve(reference_yyyymmdd):
        return SimpleNamespace(
            reference_trade_date="2026-07-14",
            target_trade_date="2026-07-14",
            target_yyyymmdd="20260714",
            is_reference_trade_day=True,
        )

    async def fake_has_successful(*args, **kwargs):
        return False

    async def fake_has_incomplete(*args, **kwargs):
        return True

    async def fake_retry(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            run_id="retry-run",
            status="success",
            success_count=10,
            failed_count=0,
        )

    async def unexpected_full_sync(**kwargs):
        raise AssertionError("不应该重新执行全市场同步")

    monkeypatch.setattr(crawler_jobs, "today_yyyymmdd", lambda: "20260714")
    monkeypatch.setattr(daily_service, "resolve_a_stock_target_trade_date", fake_resolve)
    monkeypatch.setattr(
        daily_service,
        "stock_daily_detail_has_successful_sync_run",
        fake_has_successful,
    )
    monkeypatch.setattr(
        daily_service,
        "stock_daily_detail_has_incomplete_sync_run",
        fake_has_incomplete,
    )
    monkeypatch.setattr(
        daily_service,
        "retry_latest_incomplete_stock_daily_detail_run",
        fake_retry,
    )
    monkeypatch.setattr(daily_service, "run_stock_daily_detail_sync", unexpected_full_sync)

    asyncio.run(crawler_jobs._run_stock_daily_detail_job(run_mode="scheduled"))

    assert calls == [
        (
            ("2026-07-14",),
            {"adjust": "qfq", "max_automatic_compensations": 3},
        )
    ]


def test_immediate_compensation_retries_until_remaining_batch_succeeds(
    monkeypatch,
) -> None:
    results = iter(
        [
            SimpleNamespace(
                run_id="retry-1", success_count=8, failed_count=2
            ),
            SimpleNamespace(
                run_id="retry-2", success_count=2, failed_count=0
            ),
        ]
    )
    calls = []

    async def fake_retry(*args, **kwargs):
        calls.append((args, kwargs))
        return next(results)

    monkeypatch.setattr(
        daily_service,
        "retry_latest_incomplete_stock_daily_detail_run",
        fake_retry,
    )

    asyncio.run(
        crawler_jobs._run_immediate_stock_daily_detail_compensations(
            target_trade_date="2026-07-14",
            adjust="qfq",
            max_automatic_compensations=3,
        )
    )

    assert len(calls) == 2


def test_new_partial_run_is_compensated_immediately(monkeypatch) -> None:
    compensation_calls = []

    async def fake_resolve(reference_yyyymmdd):
        return SimpleNamespace(
            reference_trade_date="2026-07-14",
            target_trade_date="2026-07-14",
            target_yyyymmdd="20260714",
            is_reference_trade_day=True,
        )

    async def always_false(*args, **kwargs):
        return False

    async def fake_full_sync(**kwargs):
        return SimpleNamespace(failed_count=5)

    async def fake_immediate_compensation(**kwargs):
        compensation_calls.append(kwargs)

    monkeypatch.setattr(crawler_jobs, "today_yyyymmdd", lambda: "20260714")
    monkeypatch.setattr(daily_service, "resolve_a_stock_target_trade_date", fake_resolve)
    monkeypatch.setattr(
        daily_service,
        "stock_daily_detail_has_successful_sync_run",
        always_false,
    )
    monkeypatch.setattr(
        daily_service,
        "stock_daily_detail_has_incomplete_sync_run",
        always_false,
    )
    monkeypatch.setattr(daily_service, "run_stock_daily_detail_sync", fake_full_sync)
    monkeypatch.setattr(
        crawler_jobs,
        "_run_immediate_stock_daily_detail_compensations",
        fake_immediate_compensation,
    )

    asyncio.run(crawler_jobs._run_stock_daily_detail_job(run_mode="scheduled"))

    assert compensation_calls == [
        {
            "target_trade_date": "2026-07-14",
            "adjust": "qfq",
            "max_automatic_compensations": 3,
        }
    ]
