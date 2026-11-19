"""Non-overlapping 5-second observations, daily close and five-session cleanup."""
import asyncio
from datetime import datetime
import logging

from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.fund_flow_repository import FundFlowRepository
from app.services.fund_flow_service import FundFlowService, POLL_SECONDS
from app.services.market_observation_service import observation_now
from app.services.trading_calendar_service import get_a_share_calendar

logger = logging.getLogger(__name__)
_service = None
_lock = asyncio.Lock()


async def close_fund_flow_service():
    global _service
    if _service is not None:
        await _service.close()
        _service = None


async def fund_flow_job(*, daily=False):
    global _service
    if _lock.locked() and not daily:
        return
    async with _lock:
        if _service is None:
            _service = FundFlowService(repository=FundFlowRepository(background_upload=True))
        now, offset = await observation_now()
        try:
            result = (await _service.collect_daily(now=now, clock_offset=offset) if daily
                      else await _service.sample(now=now, clock_offset=offset))
            if result['status'] != 'skipped':
                logger.info('fund_flow_job_finished daily=%s result=%s', daily, result)
            return result
        except Exception as exc:
            await _service.repo.record_run('fund_flow_daily' if daily else 'fund_flow',
                now.date().isoformat(), {'status': 'failed', 'error_type': type(exc).__name__})
            logger.exception('fund_flow_job_failed daily=%s', daily)
            raise


async def cleanup_fund_flow_job():
    now, offset = await observation_now()
    if not get_a_share_calendar().is_session(now.astimezone(CN_TZ).date()):
        return {'status': 'skipped', 'reason': 'non_trading_day'}
    repository = FundFlowRepository()
    await repository.create_indexes()
    # Daily collection is deliberately absent from repository.raw_collections.
    await repository.replay_pending(limit=3, calibrated_now=now)
    result = await repository.delete_expired(calibrated_now=now)
    logger.info('fund_flow_retention_finished %s', result)
    return result


def register_fund_flow_jobs(scheduler):
    scheduler.add_job(fund_flow_job, trigger='interval', seconds=POLL_SECONDS,
        next_run_time=datetime.now(CN_TZ), id='stock_fund_flow_5s',
        name='资金流向五秒采样', replace_existing=True, max_instances=1,
        coalesce=True, misfire_grace_time=5)
    scheduler.add_job(fund_flow_job, trigger='cron', day_of_week='mon-fri', hour='15-23',
        minute='5,20,35,50', second=20, timezone='Asia/Shanghai',
        kwargs={'daily': True}, id='stock_fund_flow_daily', name='收盘资金流向独立日档',
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=900)
    scheduler.add_job(fund_flow_job, trigger='date', run_date=datetime.now(CN_TZ),
        kwargs={'daily': True}, id='stock_fund_flow_daily_startup',
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
    scheduler.add_job(cleanup_fund_flow_job, trigger='interval', hours=1,
        next_run_time=datetime.now(CN_TZ), id='fund_flow_retention',
        name='资金流向原始快照保留五个交易日', replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=300)
