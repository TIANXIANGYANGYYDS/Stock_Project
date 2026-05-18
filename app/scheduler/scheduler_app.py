from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.db.mongo import client
from app.scheduler.crawler_jobs import crawl_news_job, ensure_news_indexes


logger = logging.getLogger(__name__)

JOB_ID = "crawl_news_job"


def configure_logging() -> None:
    settings = get_settings()
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    job = scheduler.add_job(
        crawl_news_job,
        trigger="interval",
        minutes=3,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(),
    )
    logger.info("registered job id=%s", job.id)
    return scheduler


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("signal handlers unavailable, signal=%s", sig.name)


async def run_scheduler() -> None:
    configure_logging()

    logger.info("scheduler starting")
    await ensure_news_indexes()

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
    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        logger.info("scheduler stopping")


if __name__ == "__main__":
    main()