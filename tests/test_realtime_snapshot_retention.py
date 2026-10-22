import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pymongo.errors import ConnectionFailure

from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.realtime_snapshot_repository import (
    AUCTION_COLLECTION, SNAPSHOT_COLLECTION, RealtimeSnapshotRepository,
    snapshot_retained_sessions, snapshot_retention_cutoff,
)
from app.scheduler import realtime_snapshot_retention_jobs as jobs


class Collection:
    def __init__(self, rows=()):
        self.rows = {row['_id']: row for row in rows}
        self.deleted_batches = []

    @staticmethod
    def matches(row, query):
        return (isinstance(row.get('observed_at'), datetime)
                and row['observed_at'] < query['observed_at']['$lt']
                and row['trade_date'] < query['trade_date']['$lt']
                and ('_id' not in query or row['_id'] in query['_id']['$in']))

    async def find_one(self, query, projection, sort):
        rows = [r for r in self.rows.values() if r.get('trade_date', '') <= query['trade_date']['$lte']
                and isinstance(r.get('observed_at'), datetime)
                and r['observed_at'] <= query['observed_at']['$lte']]
        return max(rows, key=lambda r: (r['trade_date'], r['observed_at'])) if rows else None

    def find(self, query, projection):
        assert projection == {'_id': 1}
        rows = [{'_id': row['_id']} for row in self.rows.values() if self.matches(row, query)]
        class Cursor:
            def hint(self, value): return self
            def limit(self, value): return self
            async def to_list(self, length): return rows[:length]
        return Cursor()

    async def delete_many(self, query):
        identities = [key for key, row in self.rows.items() if self.matches(row, query)]
        self.deleted_batches.append(len(identities))
        for key in identities:
            del self.rows[key]
        return SimpleNamespace(deleted_count=len(identities))

    async def update_one(self, query, update, upsert):
        self.rows.setdefault(query['_id'], update['$setOnInsert'])


def at(day, clock='10:00:00'):
    return datetime.fromisoformat(f'{day}T{clock}+08:00')


def row(identity, observed):
    return {'_id': identity, 'trade_date': observed.astimezone(CN_TZ).date().isoformat(),
            'observed_at': observed, 'received_at': observed + timedelta(minutes=5)}


def test_five_trading_sessions_skip_weekend_and_national_holiday():
    assert snapshot_retained_sessions('2026-10-15') == (
        '2026-10-09', '2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15')
    assert snapshot_retained_sessions('2026-10-08') == (
        '2026-09-24', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-08')
    assert snapshot_retention_cutoff('2026-10-15') == at('2026-10-09', '00:00:00')


def test_whole_day_boundary_and_only_raw_collections(tmp_path):
    async def run():
        now = at('2026-10-15')
        cutoff = snapshot_retention_cutoff('2026-10-15')
        originals = [row('expired', at('2026-10-08', '23:59:59.999')),
                     row('boundary', cutoff), row('late_on_oldest_day', at('2026-10-09', '15:00:00')),
                     row('latest', now)]
        db = {name: Collection(originals) for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)}
        # All other collection access would raise KeyError.
        repo = RealtimeSnapshotRepository(db, spool_root=tmp_path)
        result = await repo.delete_expired(calibrated_now=now.astimezone(timezone.utc))
        assert result['deleted'] == {SNAPSHOT_COLLECTION: 1, AUCTION_COLLECTION: 1}
        assert result['retention_trading_days'] == 5
        for collection in db.values():
            assert set(collection.rows) == {'boundary', 'late_on_oldest_day', 'latest'}
        assert (await repo.delete_expired(calibrated_now=now))['deleted'] == {
            SNAPSHOT_COLLECTION: 0, AUCTION_COLLECTION: 0}
    asyncio.run(run())


def test_new_session_starts_only_after_a_batch_arrives_in_either_collection(tmp_path):
    async def run():
        db = {SNAPSHOT_COLLECTION: Collection([row('previous', at('2026-10-15', '15:00:00')),
                                               row('oldest', at('2026-10-09'))]),
              AUCTION_COLLECTION: Collection()}
        repo = RealtimeSnapshotRepository(db, spool_root=tmp_path)
        for now in (at('2026-10-16', '00:01:00'), at('2026-10-16', '09:14:59')):
            result = await repo.delete_expired(calibrated_now=now)
            assert result['latest_trade_date'] == '2026-10-15'
            assert 'oldest' in db[SNAPSHOT_COLLECTION].rows
        db[AUCTION_COLLECTION].rows['today'] = row('today', at('2026-10-16', '09:15:00'))
        # Source observation time, not the five-minutes-fast host, decides availability.
        result = await repo.delete_expired(calibrated_now=at('2026-10-16', '09:14:59'))
        assert result['latest_trade_date'] == '2026-10-15'
        result = await repo.delete_expired(calibrated_now=at('2026-10-16', '09:15:00'))
        assert result['retained_trade_dates'] == [f'2026-10-{d}' for d in (12, 13, 14, 15, 16)]
        assert set(db[SNAPSHOT_COLLECTION].rows) == {'previous'}
        assert 'today' in db[AUCTION_COLLECTION].rows
    asyncio.run(run())


def test_calendar_window_does_not_treat_missing_capture_day_as_a_holiday(tmp_path):
    async def run():
        repo = RealtimeSnapshotRepository({
            SNAPSHOT_COLLECTION: Collection([row('old', at('2026-10-08')), row('latest', at('2026-10-15'))]),
            AUCTION_COLLECTION: Collection()}, spool_root=tmp_path)
        result = await repo.delete_expired(calibrated_now=at('2026-10-15'))
        assert result['deleted'][SNAPSHOT_COLLECTION] == 1
        assert result['retained_trade_dates'][0] == '2026-10-09'
    asyncio.run(run())


def test_cleanup_batches_large_history_and_survives_repeat(tmp_path):
    async def run():
        first = Collection([row(str(i), at('2026-10-08')) for i in range(205)]
                           + [row('latest', at('2026-10-15'))])
        repo = RealtimeSnapshotRepository({SNAPSHOT_COLLECTION: first, AUCTION_COLLECTION: Collection()},
                                          spool_root=tmp_path)
        result = await repo.delete_expired(calibrated_now=at('2026-10-15'))
        assert result['deleted'][SNAPSHOT_COLLECTION] == 205
        assert first.deleted_batches == [100, 100, 5]
        assert set(first.rows) == {'latest'}
        assert (await repo.delete_expired(calibrated_now=at('2026-10-15')))['deleted'][SNAPSHOT_COLLECTION] == 0
    asyncio.run(run())


def envelope(identity, observed):
    return {'version': 2, 'batch_id': identity, 'observed_at': observed.isoformat(),
            'received_at': (observed+timedelta(minutes=5)).isoformat(),
            'clock_offset_seconds': -300, 'session': 'morning',
            'expected_codes': [], 'metrics': {}, 'quotes': []}


def test_pending_cleanup_uses_observation_time_and_keeps_invalid_and_unfinished_files(tmp_path):
    async def run():
        now = at('2026-10-15')
        cutoff = snapshot_retention_cutoff('2026-10-15')
        repo = RealtimeSnapshotRepository({SNAPSHOT_COLLECTION: Collection([row('latest', now)]),
                                          AUCTION_COLLECTION: Collection()}, spool_root=tmp_path)
        repo._spool(envelope('old', cutoff-timedelta(seconds=1)))
        repo._spool(envelope('boundary', cutoff))
        (tmp_path/'invalid.json.gz').write_bytes(b'invalid')
        (tmp_path/'unfinished.json.tmp').write_bytes(b'pending write')
        result = await repo.delete_expired(calibrated_now=now)
        assert result['pending_files_deleted'] == 1
        assert {p.name for p in tmp_path.iterdir()} == {
            'boundary.json.gz', 'invalid.json.gz', 'unfinished.json.tmp'}
    asyncio.run(run())


def test_pending_replay_never_reinserts_expired_quote_or_auction(tmp_path):
    async def run():
        now = at('2026-10-15')
        db = {SNAPSHOT_COLLECTION: Collection([row('latest', now)]), AUCTION_COLLECTION: Collection()}
        repo = RealtimeSnapshotRepository(db, spool_root=tmp_path)
        repo._spool(envelope('old_quote', at('2026-10-08')))
        repo._spool(envelope('old_auction', at('2026-10-08', '09:20:00')))
        repo._spool(envelope('keep', at('2026-10-09')))
        await repo.replay_pending(limit=10, calibrated_now=now)
        assert set(db[SNAPSHOT_COLLECTION].rows) == {'keep', 'latest'}
        assert not db[AUCTION_COLLECTION].rows
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


def test_first_capture_of_new_day_expires_old_pending_only_with_verified_clock(tmp_path):
    async def run():
        for verified in (False, True):
            db = {SNAPSHOT_COLLECTION: Collection(), AUCTION_COLLECTION: Collection()}
            repo = RealtimeSnapshotRepository(db, spool_root=tmp_path/str(verified))
            repo._spool(envelope('old', at('2026-10-08')))
            now = at('2026-10-15')
            result = await repo.save_cycle([], expected_codes=[], session='morning', observed_at=now,
                                          received_at=now+timedelta(minutes=5), metrics={}, clock_verified=verified)
            assert result['persisted']
            assert ('old' in db[SNAPSHOT_COLLECTION].rows) == (not verified)
    asyncio.run(run())


@pytest.mark.parametrize('verified', [False, True])
def test_database_outage_does_not_disable_local_retention(tmp_path, verified):
    class OfflineCollection:
        async def update_one(self, *args, **kwargs):
            raise ConnectionFailure('database unavailable')

    async def run():
        repo = RealtimeSnapshotRepository(
            {name: OfflineCollection() for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)},
            spool_root=tmp_path)
        expired = repo._spool(envelope('old_quote', at('2026-10-08')))
        expired_auction = repo._spool(envelope('old_auction', at('2026-10-08', '09:20:00')))
        boundary = repo._spool(envelope('boundary', at('2026-10-09', '00:00:00')))
        for day in ('2026-10-15', '2026-10-15', '2026-10-16'):
            now = at(day)
            result = await repo.save_cycle([], expected_codes=[], session='morning', observed_at=now,
                                          received_at=now+timedelta(minutes=5), metrics={}, clock_verified=verified)
            assert not result['persisted']
            assert (tmp_path/result['pending_file']).exists()
            assert expired.exists() is (not verified)
            assert expired_auction.exists() is (not verified)
            assert boundary.exists() is (not verified or day == '2026-10-15')
    asyncio.run(run())


def test_pending_retention_scans_once_per_window_and_retries_scan_failure(tmp_path, monkeypatch):
    async def run():
        repo = RealtimeSnapshotRepository(
            {name: Collection() for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)}, spool_root=tmp_path)
        calls = []
        def prune(cutoff):
            calls.append(cutoff)
            if len(calls) == 1:
                raise OSError('temporary directory scan failure')
            return 0
        monkeypatch.setattr(repo, '_delete_expired_pending', prune)
        for day in ('2026-10-15', '2026-10-15', '2026-10-15', '2026-10-16'):
            now = at(day)
            result = await repo.save_cycle([], expected_codes=[], session='morning', observed_at=now,
                                          received_at=now+timedelta(minutes=5), metrics={}, clock_verified=True)
            assert result['persisted']
        assert calls == [snapshot_retention_cutoff(d) for d in ('2026-10-15', '2026-10-15', '2026-10-16')]
    asyncio.run(run())


@pytest.mark.parametrize('now', [datetime(2026, 10, 16), at('2050-10-16')])
def test_bad_clock_or_uncovered_calendar_does_not_touch_data(tmp_path, now):
    async def run():
        repo = RealtimeSnapshotRepository({SNAPSHOT_COLLECTION: object()}, spool_root=tmp_path)
        with pytest.raises(ValueError):
            await repo.delete_expired(calibrated_now=now)
    asyncio.run(run())


def test_empty_database_does_not_delete_pending_files(tmp_path):
    async def run():
        repo = RealtimeSnapshotRepository({SNAPSHOT_COLLECTION: Collection(), AUCTION_COLLECTION: Collection()},
                                          spool_root=tmp_path)
        pending = repo._spool(envelope('old', at('2026-10-08')))
        assert (await repo.delete_expired(calibrated_now=at('2026-10-15')))['reason'] == 'no_observations'
        assert pending.exists()
    asyncio.run(run())


@pytest.mark.parametrize('now', [None, at('2026-10-17'), at('2026-10-01')])
def test_unverified_clock_weekend_and_holiday_do_not_access_database(monkeypatch, now):
    async def calibrated():
        if now is None: raise RuntimeError('clock_unverified')
        return now, -305.0
    def forbidden(): raise AssertionError('must not access data')
    monkeypatch.setattr(jobs, 'observation_now', calibrated)
    monkeypatch.setattr(jobs, 'RealtimeSnapshotRepository', forbidden)
    if now is None:
        with pytest.raises(RuntimeError, match='clock_unverified'):
            asyncio.run(jobs.cleanup_realtime_snapshots_job())
    else:
        assert asyncio.run(jobs.cleanup_realtime_snapshots_job())['reason'] == 'non_trading_day'


def test_job_passes_corrected_clock_instead_of_host_clock(monkeypatch):
    corrected = at('2026-10-16')
    async def calibrated(): return corrected, -305.0
    class Repository:
        async def create_indexes(self): pass
        async def delete_expired(self, *, calibrated_now):
            assert calibrated_now is corrected
            return {'status': 'success'}
    monkeypatch.setattr(jobs, 'observation_now', calibrated)
    monkeypatch.setattr(jobs, 'RealtimeSnapshotRepository', Repository)
    assert asyncio.run(jobs.cleanup_realtime_snapshots_job())['clock_offset_seconds'] == -305


def test_registration_is_persistent_hourly_and_wired_into_crawler_scheduler(monkeypatch):
    from app.scheduler import crawler_jobs, market_observation_jobs
    monkeypatch.setattr(crawler_jobs, 'register_stock_daily_detail_job', lambda s: None)
    monkeypatch.setattr(crawler_jobs, 'register_realtime_minute_jobs', lambda s: None)
    monkeypatch.setattr(market_observation_jobs, 'register_market_observation_jobs', lambda s: None)
    scheduler = AsyncIOScheduler()
    crawler_jobs.register_crawler_jobs(scheduler)
    job = scheduler.get_job(jobs.JOB_ID)
    assert job.func is jobs.cleanup_realtime_snapshots_job
    assert job.max_instances == 1 and job.coalesce
    assert job.next_run_time.tzinfo is not None
    assert job.trigger.interval == timedelta(hours=1)
