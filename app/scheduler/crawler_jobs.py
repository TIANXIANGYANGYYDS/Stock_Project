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
    """返回当前进程共用的股票日线同步任务锁。

    锁在首次使用时才创建，供定时主任务、启动补数和失败补偿
    共用，防止同一 scheduler 进程内并发写入同一交易日数据。

    返回值：
        当前 asyncio 运行环境中复用的进程内互斥锁。
    """

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

    参数：
        source_results:
            NewsIngestionService 返回的每个来源抓取结果。

    返回值：
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
    """串行启动补数和定时日线同步任务。

    已有任务持有进程内锁时直接记录跳过，否则持锁调用实际
    同步逻辑。该入口不负责跨进程互斥。

    参数：
        run_mode: 任务触发模式，由底层逻辑区分 scheduled 和 startup 口径。
    """

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
    """串行执行指定日期范围的日线网络失败补偿。

    当主同步或其他补偿任务正在运行时跳过本次调度；获得锁后
    只重试持久化运行记录中仍未成功的网络错误。

    参数：
        target_scope: 补偿目标范围，仅支持 ``today`` 或 ``previous``。
        max_automatic_compensations: 允许该运行记录自动补偿的次数上限。
    """

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
    """解析补偿目标交易日，并重试最新的未完成日线运行记录。

    ``today`` 以当前北京日期为参考，``previous`` 先回退一个自然日，
    再由 A 股交易日历解析实际目标日。服务层没有可重试记录时
    记录跳过；重试异常会被捕获并写入调度日志。

    参数：
        target_scope: 待解析的目标范围，必须为 ``today`` 或 ``previous``。
        max_automatic_compensations: 传给服务层的自动补偿次数上限。

    异常：
        ValueError: target_scope 不是受支持的范围。
    """

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
        每天 15:30 执行，只同步目标交易日。

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
            STOCK_DAILY_STARTUP_CONCURRENCY,
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
        # 日常 15:30 主任务维持原吞吐；服务恢复时降低并发，避免启动峰值打满内存。
        concurrency = (
            STOCK_DAILY_STARTUP_CONCURRENCY
            if run_mode == "startup"
            else STOCK_DAILY_DEFAULT_CONCURRENCY
        )

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
    """立即逐轮补偿当日股票明细同步中仍可重试的网络失败项。

    每轮只交给服务层处理最近未完成批次的剩余失败股票；达到自动补偿上限，
    或服务层返回无可重试项时停止。
    """

    from app.services.stock_daily_detail_service import (
        retry_latest_incomplete_stock_daily_detail_run,
    )

    for attempt in range(1, max_automatic_compensations + 1):
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
    注册 15:30 股票详细日线同步任务。
    """

    job = scheduler.add_job(
        sync_stock_daily_detail_job,
        trigger=CronTrigger(
            hour=15,
            minute=30,
            timezone="Asia/Shanghai",
        ),
        kwargs={"run_mode": "scheduled"},
        id="sync_stock_daily_detail_1530",
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
        (15, 20, "previous", STOCK_DAILY_TOTAL_MAX_COMPENSATIONS, "audit_1520"),
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
    - 股票详细日线每天 15:30 同步任务；
    - 主批次失败后立即补偿剩余网络失败项；
    - 每天 15:20 上一交易日缺口审计。

    新闻 3 分钟抓取任务仍在 scheduler_app.build_scheduler 中注册，因为它是现有主
    任务，这里只追加新的爬虫类任务。
    """

    register_stock_daily_detail_job(scheduler)
