"""Five-trading-session raw quote retention using a verified clock."""
from datetime import datetime
import logging

from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.realtime_snapshot_repository import RealtimeSnapshotRepository
from app.services.market_observation_service import observation_now
from app.services.trading_calendar_service import get_a_share_calendar

logger = logging.getLogger(__name__)
JOB_ID = 'realtime_snapshot_retention'


async def cleanup_realtime_snapshots_job():
    # If the independent clocks cannot be verified, fail before touching data.
    now, offset = await observation_now()
    if not get_a_share_calendar().is_session(now.astimezone(CN_TZ).date()):
        return {'status': 'skipped', 'reason': 'non_trading_day'}
    repository = RealtimeSnapshotRepository()
    await repository.create_indexes()
    result = await repository.delete_expired(calibrated_now=now)
    result.update(calibrated_now=now.isoformat(), clock_offset_seconds=offset)
    logger.info('realtime_snapshot_retention_finished %s', result)
    return result


def register_realtime_snapshot_retention_job(scheduler):
    # Recheck on startup and hourly; holidays/weekends do no cleanup. Five
    # sessions are counted from the latest captured trading day, never hours.
    scheduler.add_job(cleanup_realtime_snapshots_job, trigger='interval', hours=1,
                      next_run_time=datetime.now(CN_TZ), id=JOB_ID,
                      name='秒级原始行情保留五个交易日', replace_existing=True,
                      max_instances=1, coalesce=True, misfire_grace_time=300)
