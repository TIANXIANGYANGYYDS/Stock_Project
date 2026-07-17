from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.services import NewsIngestionService


logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))

_indexes_ready = False
_indexes_lock: Optional[asyncio.Lock] = None
_stock_daily_job_lock: Optional[asyncio.Lock] = None


def _get_indexes_lock() -> asyncio.Lock:
    """
    获取新闻索引初始化锁。

    APScheduler 运行在 asyncio 事件循环里，索引初始化可能被多个启动路径同时触发。
    这个锁用于保护 _indexes_ready，确保新闻集合索引只初始化一次。
    """

    global _indexes_lock

    if _indexes_lock is None:
        _indexes_lock = asyncio.Lock()

    return _indexes_lock


def _get_stock_daily_job_lock() -> asyncio.Lock:
    global _stock_daily_job_lock

    if _stock_daily_job_lock is None:
        _stock_daily_job_lock = asyncio.Lock()
    return _stock_daily_job_lock


async def ensure_news_indexes() -> None:
    """
    确保新闻集合索引已创建。
    """
    global _indexes_ready

    if _indexes_ready:
        return

    async with _get_indexes_lock():
        if _indexes_ready:
            return

        service = NewsIngestionService()
        await service.ensure_indexes()
        _indexes_ready = True
        logger.info("news indexes ensured")


def _serialize_source_results(source_results: List[Any]) -> List[Dict[str, Any]]:
    """
    把新闻抓取结果中的 source_results 转成可安全打日志的 dict 列表。

    Args:
        source_results:
            NewsIngestionService 返回的每个来源抓取结果。

    Returns:
        只包含 source、fetched_count、error_message 的列表，避免日志里直接打印复杂对象。
    """

    return [
        {
            "source": item.source,
            "fetched_count": item.fetched_count,
            "error_message": item.error_message,
        }
        for item in source_results
    ]


async def crawl_news_job() -> None:
    """
    定时爬虫任务。
    只负责调用 NewsIngestionService 抓取、清洗、去重、入库。
    不允许调用 LLM。
    不允许启动 worker。
    不允许投递队列。
    """
    logger.info("crawl_news_job start")

    try:
        service = NewsIngestionService()
        result = await service.ingest_latest_news()

        logger.info(
            (
                "crawl_news_job stats "
                "total_fetched_count=%s unique_count=%s inserted_count=%s "
                "existing_count=%s duplicate_count=%s"
            ),
            result.total_fetched_count,
            result.unique_count,
            result.inserted_count,
            result.existing_count,
            result.duplicate_count,
        )
        logger.info(
            "crawl_news_job source_results=%s",
            _serialize_source_results(result.source_results),
        )
    except Exception:
        logger.exception("crawl_news_job failed")
    finally:
        logger.info("crawl_news_job finished")


def today_yyyymmdd() -> str:
    """
    返回今天的北京时间日期，格式 YYYYMMDD。

    用于日线同步默认 end_date。
    """

    return datetime.now(CN_TZ).strftime("%Y%m%d")


def previous_day_yyyymmdd(value: str) -> str:
    """
    返回指定 YYYYMMDD 日期的前一个自然日。

    startup 在交易日 16:00 前不能抓当天日线，但仍应该补上一个交易日。先退一个
    自然日，再交给 A 股交易日历解析，可以自然覆盖周末和节假日。
    """

    return (datetime.strptime(value, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def is_after_startup_min_time(min_time: str) -> bool:
    """
    判断当前北京时间是否已经到达启动同步允许时间。

    min_time 格式为 HH:MM，例如 "16:00"。这个保护只用于交易日当天的 startup
    job，避免上午重启 scheduler 时误抓尚未形成的当日日线。
    """

    now = datetime.now(CN_TZ).time()
    min_dt = datetime.strptime(min_time, "%H:%M").time()
    return now >= min_dt


async def sync_stock_daily_detail_job(*, run_mode: str = "scheduled") -> None:
    """Serialize startup and scheduled stock sync jobs within this process."""

    lock = _get_stock_daily_job_lock()
    if lock.locked():
        logger.warning(
            "sync_stock_daily_detail_job_skipped run_mode=%s reason=job_already_running",
            run_mode,
        )
        return

    async with lock:
        await _run_stock_daily_detail_job(run_mode=run_mode)


async def sync_stock_daily_detail_compensation_job(
    *,
    target_scope: str,
    max_automatic_compensations: int,
) -> None:
    """Retry only remaining network failures without overlapping the main job."""

    lock = _get_stock_daily_job_lock()
    if lock.locked():
        logger.warning(
            "sync_stock_daily_detail_compensation_skipped target_scope=%s "
            "reason=job_already_running",
            target_scope,
        )
        return

    async with lock:
        await _run_stock_daily_detail_compensation_job(
            target_scope=target_scope,
            max_automatic_compensations=max_automatic_compensations,
        )


async def _run_stock_daily_detail_compensation_job(
    *,
    target_scope: str,
    max_automatic_compensations: int,
) -> None:
    from app.services.stock_daily_detail_service import (
        STOCK_DAILY_DEFAULT_ADJUST,
        resolve_a_stock_target_trade_date,
        retry_latest_incomplete_stock_daily_detail_run,
    )

    if target_scope == "today":
        reference_yyyymmdd = today_yyyymmdd()
    elif target_scope == "previous":
        reference_yyyymmdd = previous_day_yyyymmdd(today_yyyymmdd())
    else:
        raise ValueError(f"unsupported compensation target_scope: {target_scope}")

    decision = await resolve_a_stock_target_trade_date(reference_yyyymmdd)
    logger.info(
        "sync_stock_daily_detail_compensation_start target_scope=%s "
        "target_trade_date=%s max_automatic_compensations=%s",
        target_scope,
        decision.target_trade_date,
        max_automatic_compensations,
    )
    try:
        result = await retry_latest_incomplete_stock_daily_detail_run(
            decision.target_trade_date,
            adjust=STOCK_DAILY_DEFAULT_ADJUST,
            max_automatic_compensations=max_automatic_compensations,
        )
        if result is None:
            logger.info(
                "sync_stock_daily_detail_compensation_skipped target_scope=%s "
                "target_trade_date=%s reason=no_retryable_incomplete_run",
                target_scope,
                decision.target_trade_date,
            )
            return
        logger.info(
            "sync_stock_daily_detail_compensation_finished target_scope=%s "
            "target_trade_date=%s run_id=%s status=%s success=%s failed=%s",
            target_scope,
            decision.target_trade_date,
            result.run_id,
            result.status,
            result.success_count,
            result.failed_count,
        )
    except Exception:
        logger.exception(
            "sync_stock_daily_detail_compensation_failed target_scope=%s "
            "target_trade_date=%s",
            target_scope,
            decision.target_trade_date,
        )


async def _run_stock_daily_detail_job(*, run_mode: str) -> None:
    """
    执行股票详细日线同步。

    run_mode=scheduled:
        每天 16:30 执行，只同步目标交易日。

    run_mode=startup:
        scheduler 启动后立即执行一次。若参考日期是交易日且当前时间早于
        STOCK_DAILY_STARTUP_MIN_TIME，则回退到上一个交易日检查或补数据；
        若参考日期不是交易日，也会回退到上一个交易日。

    两种模式都会基于 stock_daily_detail_sync_runs 判断目标交易日是否已有成功
    批次；成功则跳过。
    """

    try:
        from app.services.stock_daily_detail_service import (
            STOCK_DAILY_DEFAULT_ADJUST,
            STOCK_DAILY_DEFAULT_CONCURRENCY,
            STOCK_DAILY_DEFAULT_END_DATE,
            STOCK_DAILY_DEFAULT_LIMIT,
            STOCK_DAILY_DEFAULT_ONLY_CODE,
            STOCK_DAILY_TOTAL_MAX_COMPENSATIONS,
            STOCK_DAILY_STARTUP_MIN_TIME,
            resolve_a_stock_target_trade_date,
            run_stock_daily_detail_sync,
            stock_daily_detail_has_successful_sync_run,
            stock_daily_detail_has_incomplete_sync_run,
        )

        reference_yyyymmdd = STOCK_DAILY_DEFAULT_END_DATE or today_yyyymmdd()
        adjust = STOCK_DAILY_DEFAULT_ADJUST
        only_code = STOCK_DAILY_DEFAULT_ONLY_CODE
        limit = STOCK_DAILY_DEFAULT_LIMIT
        concurrency = STOCK_DAILY_DEFAULT_CONCURRENCY

        trade_date_decision = await resolve_a_stock_target_trade_date(
            reference_yyyymmdd
        )

        if (
            run_mode == "startup"
            and trade_date_decision.is_reference_trade_day
            and not is_after_startup_min_time(STOCK_DAILY_STARTUP_MIN_TIME)
        ):
            logger.info(
                (
                    "sync_stock_daily_detail_job_fallback run_mode=%s "
                    "reference_trade_date=%s min_time=%s reason=before_startup_min_time"
                ),
                run_mode,
                trade_date_decision.reference_trade_date,
                STOCK_DAILY_STARTUP_MIN_TIME,
            )
            trade_date_decision = await resolve_a_stock_target_trade_date(
                previous_day_yyyymmdd(reference_yyyymmdd)
            )

        start_date = trade_date_decision.target_yyyymmdd
        end_date = trade_date_decision.target_yyyymmdd

        logger.info(
            (
                "sync_stock_daily_detail_job_start run_mode=%s reference_date=%s "
                "target_trade_date=%s is_reference_trade_day=%s start_date=%s "
                "end_date=%s adjust=%s limit=%s only_code=%s concurrency=%s"
            ),
            run_mode,
            trade_date_decision.reference_trade_date,
            trade_date_decision.target_trade_date,
            trade_date_decision.is_reference_trade_day,
            start_date,
            end_date,
            adjust,
            limit,
            only_code,
            concurrency,
        )

        has_successful_run = await stock_daily_detail_has_successful_sync_run(
            trade_date_decision.target_trade_date,
            adjust,
            only_code,
            limit,
        )

        if has_successful_run:
            logger.info(
                (
                    "sync_stock_daily_detail_job_skipped target_trade_date=%s "
                    "adjust=%s only_code=%s limit=%s reason=successful_run_exists"
                ),
                trade_date_decision.target_trade_date,
                adjust,
                only_code,
                limit,
            )
            return

        has_incomplete_run = await stock_daily_detail_has_incomplete_sync_run(
            trade_date_decision.target_trade_date,
            adjust,
        )
        if has_incomplete_run:
            await _run_immediate_stock_daily_detail_compensations(
                target_trade_date=trade_date_decision.target_trade_date,
                adjust=adjust,
                max_automatic_compensations=STOCK_DAILY_TOTAL_MAX_COMPENSATIONS,
            )
            return

        result = await run_stock_daily_detail_sync(
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            limit=limit,
            only_code=only_code,
            run_mode=run_mode,
            target_trade_date=trade_date_decision.target_trade_date,
            concurrency=concurrency,
        )
        if result.failed_count:
            await _run_immediate_stock_daily_detail_compensations(
                target_trade_date=trade_date_decision.target_trade_date,
                adjust=adjust,
                max_automatic_compensations=STOCK_DAILY_TOTAL_MAX_COMPENSATIONS,
            )
    except Exception:
        logger.exception("sync_stock_daily_detail_job_failed")
    finally:
        logger.info("sync_stock_daily_detail_job_finished")


async def _run_immediate_stock_daily_detail_compensations(
    *,
    target_trade_date: str,
    adjust: str,
    max_automatic_compensations: int,
) -> None:
    """Immediately retry each remaining network-failure batch."""

    from app.services.stock_daily_detail_service import (
        STOCK_DAILY_BROWSER_ERROR_EXTRA_COMPENSATIONS,
        retry_latest_incomplete_stock_daily_detail_run,
    )

    max_immediate_rounds = (
        max_automatic_compensations
        + STOCK_DAILY_BROWSER_ERROR_EXTRA_COMPENSATIONS
    )
    for attempt in range(1, max_immediate_rounds + 1):
        result = await retry_latest_incomplete_stock_daily_detail_run(
            target_trade_date,
            adjust=adjust,
            max_automatic_compensations=max_automatic_compensations,
        )
        if result is None:
            logger.info(
                "sync_stock_daily_detail_immediate_compensation_stopped "
                "target_trade_date=%s attempt=%s reason=no_retryable_failure_or_exhausted",
                target_trade_date,
                attempt,
            )
            return

        logger.info(
            "sync_stock_daily_detail_immediate_compensation_finished "
            "target_trade_date=%s attempt=%s run_id=%s success=%s failed=%s",
            target_trade_date,
            attempt,
            result.run_id,
            result.success_count,
            result.failed_count,
        )
        if result.failed_count == 0:
            return


def register_stock_daily_detail_job(
    scheduler: AsyncIOScheduler,
) -> None:
    """
    注册 16:30 股票详细日线同步任务。
    """

    job = scheduler.add_job(
        sync_stock_daily_detail_job,
        trigger=CronTrigger(
            hour=16,
            minute=30,
            timezone="Asia/Shanghai",
        ),
        kwargs={"run_mode": "scheduled"},
        id="sync_stock_daily_detail_1630",
        name="同步股票详细日线数据",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
    )
    logger.info("registered job id=%s", job.id)

    startup_job = scheduler.add_job(
        sync_stock_daily_detail_job,
        trigger=DateTrigger(
            run_date=datetime.now(CN_TZ),
            timezone="Asia/Shanghai",
        ),
        kwargs={"run_mode": "startup"},
        id="sync_stock_daily_detail_startup",
        name="启动时同步今日股票详细日线数据",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 30,
    )
    logger.info("registered job id=%s", startup_job.id)

    from app.services.stock_daily_detail_service import (
        STOCK_DAILY_TOTAL_MAX_COMPENSATIONS,
    )

    compensation_jobs = (
        (15, 30, "previous", STOCK_DAILY_TOTAL_MAX_COMPENSATIONS, "audit_1530"),
    )
    for hour, minute, target_scope, max_compensations, suffix in compensation_jobs:
        compensation_job = scheduler.add_job(
            sync_stock_daily_detail_compensation_job,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                timezone="Asia/Shanghai",
            ),
            kwargs={
                "target_scope": target_scope,
                "max_automatic_compensations": max_compensations,
            },
            id=f"sync_stock_daily_detail_{suffix}",
            name="补偿股票详细日线失败数据",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60 * 30,
        )
        logger.info("registered job id=%s", compensation_job.id)


def register_crawler_jobs(
    scheduler: AsyncIOScheduler,
) -> None:
    """
    爬虫类定时任务统一注册入口。

    目前注册：
    - 股票详细日线启动立即同步任务；
    - 股票详细日线每天 16:30 同步任务；
    - 主批次失败后立即补偿剩余网络失败项；
    - 每天 15:30 上一交易日缺口审计。

    新闻 3 分钟抓取任务仍在 scheduler_app.build_scheduler 中注册，因为它是现有主
    任务，这里只追加新的爬虫类任务。
    """

    register_stock_daily_detail_job(scheduler)
