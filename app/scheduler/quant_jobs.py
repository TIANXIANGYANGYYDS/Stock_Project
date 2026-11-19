"""正式量化影子盘的盘前准备、盘中刷新和启动恢复任务。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.services.quant_live_service import QuantLiveService
from app.services.market_observation_service import observation_now


logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))

QUANT_LIVE_PREPARE_JOB_ID = "quant_live_prepare_0920"
QUANT_LIVE_MORNING_JOB_ID = "quant_live_refresh_morning"
QUANT_LIVE_AFTERNOON_JOB_ID = "quant_live_refresh_afternoon"
QUANT_LIVE_STARTUP_JOB_ID = "quant_live_startup_resume"
QUANT_LIVE_RECOVERY_JOB_ID = "quant_live_after_close_recovery"

MORNING_WINDOW = (time(9, 30), time(11, 35))
AFTERNOON_WINDOW = (time(13, 0), time(15, 10, 59))

_quant_job_lock: asyncio.Lock | None = None


def _get_quant_job_lock() -> asyncio.Lock:
    """按当前事件循环延迟创建锁，兼容测试和进程内循环重建。"""

    global _quant_job_lock
    running_loop = asyncio.get_running_loop()
    lock_loop = getattr(_quant_job_lock, "_loop", None)
    if _quant_job_lock is None or lock_loop not in (None, running_loop):
        _quant_job_lock = asyncio.Lock()
    return _quant_job_lock


def _inside_refresh_window(value: time) -> bool:
    return (
        MORNING_WINDOW[0] <= value <= MORNING_WINDOW[1]
        or AFTERNOON_WINDOW[0] <= value <= AFTERNOON_WINDOW[1]
    )


async def prepare_quant_live_job() -> dict[str, Any]:
    """幂等冻结当天观察池；同进程内不与盘中重放并发。"""

    job_lock = _get_quant_job_lock()
    if job_lock.locked():
        return {"status": "skipped", "reason": "quant_job_already_running"}
    async with job_lock:
        evaluated, _ = await observation_now()
        service = QuantLiveService()
        try:
            await service.catch_up_completed_days(before_date=evaluated.date())
            result = await service.prepare(reference_date=evaluated.date())
        except Exception as exc:
            try:
                await service.results.record_runtime_error(
                    trade_date=evaluated.date().isoformat(),
                    evaluated_at=evaluated.isoformat(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("failed to persist quant live prepare error")
            logger.exception("quant live prepare failed")
            raise
        logger.info(
            "quant live prepare finished trade_date=%s status=%s",
            result.get("trade_date"),
            result.get("status"),
        )
        return result


async def refresh_quant_live_job(
    *,
    now: datetime | None = None,
    allow_outside_window: bool = False,
) -> dict[str, Any]:
    """在交易时段刷新快照；异常时保留旧快照并写入可观测错误。"""

    evaluated = now if now is not None else (await observation_now())[0]
    if evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=CN_TZ)
    else:
        evaluated = evaluated.astimezone(CN_TZ)
    if not allow_outside_window and not _inside_refresh_window(evaluated.time()):
        return {"status": "skipped", "reason": "outside_refresh_window"}
    job_lock = _get_quant_job_lock()
    if job_lock.locked():
        return {"status": "skipped", "reason": "quant_job_already_running"}

    service = QuantLiveService()
    async with job_lock:
        try:
            await service.catch_up_completed_days(before_date=evaluated.date())
            result = await service.process(now=evaluated)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                await service.results.record_runtime_error(
                    trade_date=evaluated.date().isoformat(),
                    evaluated_at=evaluated.isoformat(),
                    error=error,
                )
            except Exception:
                logger.exception("failed to persist quant live runtime error")
            logger.exception("quant live refresh failed")
            raise
        logger.info(
            "quant live refresh finished trade_date=%s status=%s version=%s",
            result.get("trade_date"),
            result.get("status"),
            result.get("runtime", {}).get("version"),
        )
        return result


async def resume_quant_live_job() -> dict[str, Any]:
    """调度器重启时恢复当天开盘状态，并在需要时补算最新快照。"""

    now, _ = await observation_now()
    if now.time() < time(9, 20):
        return {"status": "skipped", "reason": "before_prepare_window"}
    prepared = await prepare_quant_live_job()
    if prepared.get("status") == "skipped":
        return prepared
    if now.time() >= time(9, 30):
        return await refresh_quant_live_job(
            allow_outside_window=True,
        )
    return {"status": "prepared"}


async def recover_completed_quant_job() -> dict[str, Any]:
    """Publish a validated recovery chain after backfill, then resume normally."""
    import sys
    from pathlib import Path
    from app.services.trading_calendar_service import get_a_share_calendar

    now, _ = await observation_now()
    # Never replace prior accounts underneath a running trading session.
    if time(9, 20) <= now.time() < time(15, 20):
        return await resume_quant_live_job()
    cutoff = now.date() if now.time() >= time(15, 20) else now.date() - timedelta(days=1)
    sessions = get_a_share_calendar().sessions_in_range(cutoff - timedelta(days=30), cutoff)
    end = sessions[-1].strftime('%Y-%m-%d')
    lock = _get_quant_job_lock()
    if lock.locked():
        return {'status': 'skipped', 'reason': 'quant_job_already_running'}
    async with lock:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-m', 'app.quant.cli.recover_partial_days',
            '--end-date', end, '--apply', cwd=str(Path(__file__).resolve().parents[2]),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        if process.returncode:
            logger.error('quant_partial_recovery_failed %s', stderr.decode(errors='replace')[-2000:])
            raise RuntimeError(f'Quant recovery exited with status {process.returncode}')
        logger.info('quant_partial_recovery_finished %s', stdout.decode(errors='replace')[-2000:])
    return await resume_quant_live_job()


def register_quant_live_jobs(scheduler: AsyncIOScheduler) -> None:
    """注册稳定任务 ID，避免重启后产生重复量化任务。"""

    definitions = (
        (
            prepare_quant_live_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=9,
                minute=20,
                timezone="Asia/Shanghai",
            ),
            QUANT_LIVE_PREPARE_JOB_ID,
            "准备每日量化观察池",
        ),
        (
            refresh_quant_live_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="9-11",
                minute="*",
                second=20,
                timezone="Asia/Shanghai",
            ),
            QUANT_LIVE_MORNING_JOB_ID,
            "刷新上午量化影子盘",
        ),
        (
            refresh_quant_live_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="13-15",
                minute="*",
                second=20,
                timezone="Asia/Shanghai",
            ),
            QUANT_LIVE_AFTERNOON_JOB_ID,
            "刷新下午量化影子盘",
        ),
    )
    for func, trigger, job_id, name in definitions:
        job = scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            name=name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        logger.info("registered job id=%s", job.id)

    startup_job = scheduler.add_job(
        recover_completed_quant_job,
        trigger=DateTrigger(
            run_date=datetime.now(CN_TZ),
            timezone="Asia/Shanghai",
        ),
        id=QUANT_LIVE_STARTUP_JOB_ID,
        name="启动恢复每日量化影子盘",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    logger.info("registered job id=%s", startup_job.id)
    recovery_job = scheduler.add_job(
        recover_completed_quant_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour="16-22", minute=15,
                            timezone="Asia/Shanghai"),
        id=QUANT_LIVE_RECOVERY_JOB_ID,
        name="日线补数后恢复量化账本",
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=600,
    )
    logger.info("registered job id=%s", recovery_job.id)
