from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)
# APScheduler 中用于幂等替换盘前分析任务的稳定 ID。
MORNING_ANALYSIS_JOB_ID = "morning_market_analysis"
MORNING_ANALYSIS_RETRY_JOB_ID = "morning_market_analysis_retry"
MORNING_ANALYSIS_STARTUP_JOB_ID = "morning_market_analysis_startup_catchup"
# 08:20 首次执行失败时，在早盘仍可用的两个时间点补偿一次。
MORNING_ANALYSIS_RETRY_MINUTES = "40,55"
CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
_morning_analysis_lock: asyncio.Lock | None = None


def _get_morning_analysis_lock() -> asyncio.Lock:
    """Return the process-local lock shared by scheduled and catch-up runs."""

    global _morning_analysis_lock

    if _morning_analysis_lock is None:
        _morning_analysis_lock = asyncio.Lock()
    return _morning_analysis_lock


def _normalize_datetime(value: datetime | None) -> datetime:
    """Normalize an optional reference time to the project's China timezone."""

    if value is None:
        return datetime.now(CN_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TZ)
    return value.astimezone(CN_TZ)


async def _run_morning_analysis(
    *,
    reference_datetime: datetime | None = None,
    persist: bool = True,
) -> Any:
    """
    构造盘前分析服务，并按业务模块固定的数据窗口参数执行一次分析。

    `reference_datetime` 可用于手工历史执行或测试；服务内部仍会判断该日期是否
    为交易日，并以固定的 08:20 作为新闻和博主观点的可用截止点。
    """
    from app.services.morning_analysis_service import (
        MORNING_ANALYSIS_CREATOR_LIMIT,
        MORNING_ANALYSIS_CREATOR_WORK_LIMIT,
        MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        MORNING_ANALYSIS_RANKING_LIMIT,
        MorningAnalysisService,
    )
    service = MorningAnalysisService()
    return await service.run(
        reference_datetime=reference_datetime,
        persist=persist,
        ranking_limit=MORNING_ANALYSIS_RANKING_LIMIT,
        max_snapshot_age_minutes=MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        creator_limit=MORNING_ANALYSIS_CREATOR_LIMIT,
        creator_work_limit=MORNING_ANALYSIS_CREATOR_WORK_LIMIT,
    )


async def _run_morning_analysis_if_missing(
    *,
    reference_datetime: datetime | None = None,
    job_name: str,
) -> Any:
    """Run a current-day catch-up only when its report is not already persisted."""

    from app.repositories.daily_market_analysis_repository import (
        DailyMarketAnalysisRepository,
    )
    from app.services.morning_analysis_policy import (
        MORNING_ANALYSIS_HOUR,
        MORNING_ANALYSIS_MINUTE,
    )
    from app.services.trading_calendar_service import resolve_morning_trade_dates

    reference = _normalize_datetime(reference_datetime)
    cutoff = reference.replace(
        hour=MORNING_ANALYSIS_HOUR,
        minute=MORNING_ANALYSIS_MINUTE,
        second=0,
        microsecond=0,
    )
    if reference < cutoff:
        logger.info("%s skipped reason=before_cutoff", job_name)
        return None

    decision = resolve_morning_trade_dates(reference.date())
    if not decision.is_current_trade_day:
        logger.info(
            "%s skipped reason=non_trading_day reference_date=%s",
            job_name,
            decision.reference_date,
        )
        return None

    repository = DailyMarketAnalysisRepository()
    if await repository.exists({"analysis_date": decision.analysis_date}):
        logger.info(
            "%s skipped reason=report_exists analysis_date=%s",
            job_name,
            decision.analysis_date,
        )
        return None

    return await _run_morning_analysis(reference_datetime=reference)


async def _run_morning_analysis_job(
    *,
    job_name: str,
    only_if_missing: bool,
) -> Any:
    """Serialize all production entry points and preserve their failure signal."""

    lock = _get_morning_analysis_lock()
    if lock.locked():
        logger.warning("%s skipped reason=job_already_running", job_name)
        return None

    async with lock:
        if only_if_missing:
            return await _run_morning_analysis_if_missing(job_name=job_name)
        return await _run_morning_analysis()


async def morning_analysis_job() -> None:
    """
    APScheduler 的盘前分析任务入口，记录跳过、成功或失败状态。

    非交易日或尚未到截止时点会作为正常跳过记录；异常在记录堆栈后重新抛出，
    使调度器能够感知任务失败。
    """
    logger.info("morning_analysis_job start")
    try:
        result = await _run_morning_analysis_job(
            job_name="morning_analysis_job",
            only_if_missing=False,
        )
        if result is None:
            return
        if result.skipped:
            logger.info("morning_analysis_job skipped: %s", result.reason)
        else:
            logger.info(
                "morning_analysis_job completed analysis_date=%s",
                result.report.analysis_date,
            )
    except Exception:
        logger.exception("morning_analysis_job failed")
        raise
    finally:
        logger.info("morning_analysis_job finished")


async def morning_analysis_retry_job() -> None:
    """Retry a missing current-day report after a transient 08:20 failure."""

    logger.info("morning_analysis_retry_job start")
    try:
        result = await _run_morning_analysis_job(
            job_name="morning_analysis_retry_job",
            only_if_missing=True,
        )
        if result is not None and not result.skipped:
            logger.info(
                "morning_analysis_retry_job completed analysis_date=%s",
                result.report.analysis_date,
            )
    except Exception:
        logger.exception("morning_analysis_retry_job failed")
        raise
    finally:
        logger.info("morning_analysis_retry_job finished")


async def morning_analysis_startup_catchup_job() -> None:
    """Recover a missed current-day report after a scheduler restart."""

    logger.info("morning_analysis_startup_catchup_job start")
    try:
        result = await _run_morning_analysis_job(
            job_name="morning_analysis_startup_catchup_job",
            only_if_missing=True,
        )
        if result is not None and not result.skipped:
            logger.info(
                "morning_analysis_startup_catchup_job completed analysis_date=%s",
                result.report.analysis_date,
            )
    except Exception:
        logger.exception("morning_analysis_startup_catchup_job failed")
        raise
    finally:
        logger.info("morning_analysis_startup_catchup_job finished")


def register_morning_analysis_job(scheduler: AsyncIOScheduler) -> None:
    """
    按固定的中国时区盘前时刻注册工作日 cron 任务。

    稳定任务 ID 与 `replace_existing` 保证重复初始化不会创建副本；单实例和
    coalesce 防止延迟补跑与当前任务重叠。实际是否为 A 股交易日由服务再次判断。
    """
    from app.services.morning_analysis_service import (
        MORNING_ANALYSIS_HOUR,
        MORNING_ANALYSIS_MINUTE,
    )

    job = scheduler.add_job(
        morning_analysis_job,
        trigger=CronTrigger(
            hour=MORNING_ANALYSIS_HOUR,
            minute=MORNING_ANALYSIS_MINUTE,
            day_of_week="mon-fri",
            timezone="Asia/Shanghai",
        ),
        id=MORNING_ANALYSIS_JOB_ID,
        name="生成每日盘前市场分析",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    logger.info("registered job id=%s", job.id)

    retry_job = scheduler.add_job(
        morning_analysis_retry_job,
        trigger=CronTrigger(
            hour=MORNING_ANALYSIS_HOUR,
            minute=MORNING_ANALYSIS_RETRY_MINUTES,
            day_of_week="mon-fri",
            timezone="Asia/Shanghai",
        ),
        id=MORNING_ANALYSIS_RETRY_JOB_ID,
        name="补偿重试盘前市场分析",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    logger.info("registered job id=%s", retry_job.id)

    startup_job = scheduler.add_job(
        morning_analysis_startup_catchup_job,
        trigger=DateTrigger(
            run_date=datetime.now(CN_TZ),
            timezone="Asia/Shanghai",
        ),
        id=MORNING_ANALYSIS_STARTUP_JOB_ID,
        name="启动补偿盘前市场分析",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    logger.info("registered job id=%s", startup_job.id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="手工生成指定交易日的盘前市场分析")
    parser.add_argument("--date", help="分析日期，格式 YYYY-MM-DD；默认使用今天")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完整执行取数和分析但不写入 MongoDB",
    )
    parser.add_argument(
        "--print-analysis",
        action="store_true",
        help="在完成后输出最终风险结论和五条行业主线 JSON",
    )
    args = parser.parse_args()

    reference_datetime = None
    if args.date:
        from app.services.morning_analysis_service import (
            MORNING_ANALYSIS_HOUR,
            MORNING_ANALYSIS_MINUTE,
        )

        reference_datetime = datetime.strptime(args.date, "%Y-%m-%d").replace(
            hour=MORNING_ANALYSIS_HOUR,
            minute=MORNING_ANALYSIS_MINUTE,
        )
    run_result = asyncio.run(
        _run_morning_analysis(
            reference_datetime=reference_datetime,
            persist=not args.dry_run,
        )
    )
    if run_result.skipped:
        print(f"skipped: {run_result.reason}")
    else:
        mode = "dry-run" if args.dry_run else "completed"
        print(f"{mode}: {run_result.report.analysis_date}")
        if args.print_analysis:
            print(
                json.dumps(
                    {
                        "analysis_date": run_result.report.analysis_date,
                        "data_quality": run_result.report.data_quality,
                        "source_analysis_memos": (
                            run_result.report.source_analysis_memos
                        ),
                        "scenario_analysis_memos": (
                            run_result.report.scenario_analysis_memos
                        ),
                        "analysis": run_result.report.analysis.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
