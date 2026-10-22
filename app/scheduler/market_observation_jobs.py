from __future__ import annotations

import asyncio
from datetime import datetime
import logging

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.crawlers.realtime_market_crawler import CN_TZ
from app.services.market_observation_service import MarketObservationService, observation_now

logger = logging.getLogger(__name__)
_locks = {}


async def market_observation_job(*, kind, startup=False):
    lock = _locks.setdefault(kind, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        service = MarketObservationService()
        try:
            now, offset = await observation_now()
            # A fast host can already have missed its 16:00 trigger during a
            # restart. Recovery also waits through that small calibrated gap.
            if kind == "billboard" and (not startup or datetime.now(CN_TZ).hour >= 16):
                target = now.replace(hour=16, minute=0, second=0, microsecond=0)
                while 0 < (target - now).total_seconds() <= 600:
                    await asyncio.sleep(min(30, (target - now).total_seconds()))
                    now, offset = await observation_now()
            result = await service.collect(kind, now=now, clock_offset=offset)
            logger.info("market_observation_finished kind=%s result=%s", kind, result)
        except Exception:
            logger.exception("market_observation_failed kind=%s", kind)
            raise
        finally:
            await service.close()


def register_market_observation_jobs(scheduler):
    # Broad host-time window accommodates clock skew; the service enforces
    # actual trading sessions and a unique corrected five-minute slot.
    for kind, trigger, identity in (
        ("hot", CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/5", second=10,
                            timezone="Asia/Shanghai"), "ths_hot_rank_5m"),
        # Seat reports may appear after 16:00. Keep retrying that day's missing
        # snapshot; the service skips an already successful immutable day.
        ("billboard", CronTrigger(day_of_week="mon-fri", hour="16-23", minute="*/15",
                                 timezone="Asia/Shanghai"), "stock_billboard_1600"),
    ):
        scheduler.add_job(market_observation_job, trigger=trigger, kwargs={"kind": kind},
                          id=identity, replace_existing=True, max_instances=1,
                          coalesce=True, misfire_grace_time=60 if kind == "hot" else 1800)
        scheduler.add_job(market_observation_job,
                          trigger=DateTrigger(run_date=datetime.now(CN_TZ)),
                          kwargs={"kind": kind, "startup": True}, id=identity + "_startup",
                          replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
