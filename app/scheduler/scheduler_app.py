from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime

from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.db.mongo import client
from app.scheduler.crawler_jobs import (
    crawl_news_job,
    ensure_news_indexes,
    register_crawler_jobs,
)
from app.scheduler.creator_monitoring_jobs import register_creator_monitoring_jobs
from app.scheduler.morning_analysis_jobs import register_morning_analysis_job
from app.scheduler.news_ranking_jobs import (
    register_news_ranking_job,
    run_news_ranking_snapshot,
)


logger = logging.getLogger(__name__)

JOB_ID = "crawl_news_job"
# APScheduler 任务定义与 next_run_time 的持久化集合；业务数据仍写入各自集合。
SCHEDULER_JOBSTORE_COLLECTION = "apscheduler_jobs"


def configure_logging() -> None:
    """按应用配置初始化根日志格式，并降低底层 HTTP 库的常规日志噪声。

    未识别的日志级别会回退到 ``INFO``；``force=True`` 确保直接运行调度器时
    使用本项目统一格式，而不是继承外部进程残留的 handler。
    """
    settings = get_settings()
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_scheduler() -> AsyncIOScheduler:
    """创建调度器并注册新闻、博主采集和盘前分析的全部周期任务。

    博主采集、内容提取、观点分析和评分统一由 creator monitoring 链路负责；
    任务定义和下一次触发时间写入 MongoDB job store，返回尚未启动的调度器，
    供主循环安装信号处理后统一管理生命周期。
    """
    settings = get_settings()
    scheduler = AsyncIOScheduler(
        jobstores={
            "default": MongoDBJobStore(
                host=settings.mongo_uri,
                database=settings.mongo_db_name,
                collection=SCHEDULER_JOBSTORE_COLLECTION,
            )
        }
    )
    job = scheduler.add_job(
        crawl_news_job,
        trigger="interval",
        minutes=3,
        id=JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(),
    )
    logger.info("registered job id=%s", job.id)
    register_crawler_jobs(scheduler)
    register_news_ranking_job(scheduler)
    register_creator_monitoring_jobs(scheduler)
    register_morning_analysis_job(scheduler)
    return scheduler


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """把 SIGINT/SIGTERM 转换为异步停止事件，供主循环执行有序关闭。

    不支持事件循环信号处理的平台只记录告警，不在这里终止进程或替换其他
    退出机制。
    """
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("signal handlers unavailable, signal=%s", sig.name)


async def refresh_news_ranking_snapshot_on_startup() -> None:
    """调度器启动时尝试生成一份新闻榜单快照。

    快照失败会记录完整异常但不会阻止调度器启动，后续每 5 分钟的周期任务仍可
    重新生成，从而避免一次数据库或计算故障阻断其他定时任务。
    """
    try:
        await run_news_ranking_snapshot()
        logger.info("initial news ranking snapshot completed")
    except Exception:
        logger.exception("initial news ranking snapshot failed")


async def run_scheduler() -> None:
    """初始化依赖、启动所有定时任务，并在收到停止信号后释放资源。

    启动顺序为日志、新闻索引、可选榜单快照和任务注册。退出时只关闭已成功
    启动的调度器，最后始终关闭共享 MongoDB client，避免连接泄漏。
    """
    configure_logging()

    logger.info("scheduler starting")
    await ensure_news_indexes()

    await refresh_news_ranking_snapshot_on_startup()

    scheduler = build_scheduler()
    stop_event = asyncio.Event()
    scheduler_started = False

    try:
        install_signal_handlers(stop_event)
        scheduler.start()
        scheduler_started = True
        logger.info("scheduler started")
        await stop_event.wait()
    finally:
        logger.info("scheduler stopping")
        try:
            if scheduler_started and scheduler.running:
                scheduler.shutdown(wait=True)
        finally:
            client.close()
            logger.info("scheduler stopped")


def main() -> None:
    """以独立进程入口运行异步调度器，并把键盘中断转换为正常停止日志。"""
    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        logger.info("scheduler stopping")


if __name__ == "__main__":
    main()
