"""Independent seconds sampling and post-close cumulative fund-flow records."""
from datetime import timedelta
import os
from pathlib import Path
import time

from app.crawlers.fund_flow_crawler import FundFlowCrawler
from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.fund_flow_repository import FundFlowRepository, DAILY_RUN_KIND
from app.services.trading_calendar_service import get_a_share_calendar

POLL_SECONDS = 5


def server_pressure():
    """Leave headroom for the other services; no additional package required."""
    memory = Path('/proc/meminfo')
    if memory.exists():
        values = {line.split(':')[0]: int(line.split()[1])*1024
                  for line in memory.read_text().splitlines() if line.startswith(('MemTotal:', 'MemAvailable:'))}
        if values['MemAvailable'] < max(2*1024**3, values['MemTotal']*0.15):
            return 'low_available_memory'
    if hasattr(os, 'getloadavg') and os.getloadavg()[0] >= (os.cpu_count() or 1)*0.8:
        return 'high_system_load'
    return None


def in_fund_flow_session(now):
    clock = now.astimezone(CN_TZ).strftime('%H:%M:%S')
    return '09:30:00' <= clock <= '11:30:05' or '13:00:00' <= clock <= '15:00:05'


def closed_fund_flow_day(now):
    calendar = get_a_share_calendar()
    local = now.astimezone(CN_TZ)
    session = calendar.date_to_session(local.date(), direction='previous')
    if calendar.is_session(local.date()) and local.strftime('%H:%M') < '15:05':
        session = calendar.previous_session(session)
    return session.strftime('%Y-%m-%d')


class FundFlowService:
    def __init__(self, repository=None, crawler=None):
        self.repo = repository or FundFlowRepository()
        self.crawler = crawler or FundFlowCrawler()
        self._initialized = False
        self._last_cycle = None

    async def close(self):
        await self.crawler.close()
        if isinstance(self.repo, FundFlowRepository):
            await self.repo.finish_upload()

    async def initialize(self):
        if not self._initialized:
            await self.repo.create_indexes()
            self._initialized = True

    async def sample(self, *, now, clock_offset=0, manual=False, codes=None):
        local = now.astimezone(CN_TZ)
        if not get_a_share_calendar().is_session(local.date()):
            return {'status': 'skipped', 'reason': 'non_trading_day'}
        if not manual and not in_fund_flow_session(local):
            self._last_cycle = None
            clock = local.strftime('%H:%M')
            if ('09:25' <= clock < '09:30' or '12:55' <= clock < '13:00') and not server_pressure():
                await self.initialize()
                await self.crawler._codes(local.date().isoformat())
            return {'status': 'skipped', 'reason': 'outside_session'}
        pressure = server_pressure()
        if pressure:
            result = {'status': 'missed', 'reason': pressure, 'target_poll_seconds': POLL_SECONDS}
            await self.repo.record_run('fund_flow', local.date().isoformat(), result)
            return result
        await self.initialize()
        day = local.date().isoformat()
        started = time.monotonic()
        data = await self.crawler.fetch(day, clock_offset=clock_offset, codes=codes,
                                       require_usable=False, timeout_seconds=4, max_attempts=1)
        observed = data['fetched_at'] + timedelta(seconds=clock_offset)
        if observed.astimezone(CN_TZ).date().isoformat() != day:
            raise ValueError('fund_flow_fetch_crossed_day')
        gap = None if self._last_cycle is None else started-self._last_cycle
        self._last_cycle = started
        data.update(trade_date=day, observed_at=observed, target_poll_seconds=POLL_SECONDS,
                    observation_gap_seconds=gap, clock_offset_seconds=clock_offset,
                    run_mode='manual' if manual else 'scheduled',
                    cadence_overrun=data['fetch_elapsed_seconds'] > POLL_SECONDS
                        or (gap is not None and gap > POLL_SECONDS*1.5))
        persisted = await self.repo.save_observation(data)
        result = {'status': data['collection_status'], 'count': data['record_count'],
                  'requested_count': data['requested_count'],
                  'missing_count': data['requested_count'] - data['record_count'],
                  'unusable_count': data['record_count'] - data['usable_count'],
                  'response_complete': data['record_count'] == data['requested_count'],
                  'target_poll_seconds': POLL_SECONDS,
                  'usable_count': data['usable_count'], 'elapsed_seconds': time.monotonic()-started,
                  'observation_gap_seconds': gap,
                  'cadence_overrun': data['cadence_overrun'] or time.monotonic()-started > POLL_SECONDS,
                  **persisted}
        if result['status'] == 'partial':
            result['reason'] = 'missing_responses' if result['missing_count'] else 'unusable_quotes'
        # One changing diagnostic per day, not a new indexed run every 5 seconds.
        await self.repo.record_run('fund_flow', day, result)
        return result

    async def collect_daily(self, *, now, clock_offset=0):
        await self.initialize()
        day = closed_fund_flow_day(now)
        if await self.repo.daily_complete(day):
            return {'status': 'skipped', 'reason': 'already_collected', 'trade_date': day}
        pressure = server_pressure()
        if pressure:
            return {'status': 'deferred', 'reason': pressure, 'trade_date': day}
        data = None
        fetch_error = None
        if day == now.astimezone(CN_TZ).date().isoformat():
            try:
                data = await self.crawler.fetch(day, clock_offset=clock_offset)
                data['observed_at'] = data['fetched_at'] + timedelta(seconds=clock_offset)
            except Exception as exc:
                fetch_error = type(exc).__name__
        if data is None:
            # Restart/weekend recovery uses an actual same-day post-close capture.
            data = await self.repo.latest_after_close(day)
        if data is None:
            result = {'status': 'unavailable', 'complete': False, 'trade_date': day,
                      'reason': 'no_post_close_source', 'fetch_error': fetch_error}
        else:
            result = await self.repo.save_daily(data, day)
            result['fetch_error'] = fetch_error
        await self.repo.record_run(DAILY_RUN_KIND, day, result)
        return result
