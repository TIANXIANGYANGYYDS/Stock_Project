import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest

from app.repositories.fund_flow_repository import (
    FundFlowRepository, DAILY_COLLECTION, daily_rows, decode_fund_snapshot,
)
from app.repositories.market_observation_repository import FUND_FLOW_COLLECTION, RUN_COLLECTION
from app.services.fund_flow_service import FundFlowService
from app.services import fund_flow_service as service_module
from tests.test_realtime_snapshot_retention import Collection, at, row
from tests.test_realtime_snapshots import Collection as DurableCollection


def data(day='2026-11-06', clock='15:06:00'):
    observed = at(day, clock)
    return {'trade_date': day, 'observed_at': observed, 'fetched_at': observed,
            'fetch_started_at': observed-timedelta(seconds=3), 'fetch_elapsed_seconds': 3,
            'source': 'EASTMONEY', 'metric': 'intraday_cumulative_order_size_net_flow',
            'requested_count': 2, 'requested_codes': ['000001', '600519'], 'record_count': 2,
            'usable_count': 2, 'collection_status': 'success', 'complete': True,
            'records': [{'code': code, 'usable': True, 'quality_issues': [],
                         'observed_at': observed-timedelta(seconds=i), 'quote_time': at(day, '15:00:00'),
                         'flows': {'main': {'net_amount_yuan': amount, 'net_ratio_pct': 5.0}},
                         'fund_flow_time': None, 'raw': {'f62': amount}}
                        for i, (code, amount) in enumerate([('000001', 100), ('600519', -20)])]}


def test_compressed_spool_preserves_each_symbol_time_and_replays_ack_loss(tmp_path):
    async def run():
        collection = DurableCollection(); collection.ack_lost = True
        repo = FundFlowRepository({FUND_FLOW_COLLECTION: collection}, spool_root=tmp_path)
        source = data()
        result = await repo.save_observation(source)
        assert not result['persisted'] and len(list(tmp_path.glob('*.json.gz'))) == 1
        assert len(collection.rows) == 1
        await repo.replay_pending()
        assert len(collection.rows) == 1 and not list(tmp_path.glob('*.json.gz'))
        document = next(iter(collection.rows.values()))
        assert 'records' not in document and document['record_count'] == 2
        projected = dict(document)
        projected['quote_chunks'] = {'19': document['quote_chunks']['19']}
        projected['chunk_sha256'] = {'19': document['chunk_sha256']['19']}
        decoded = decode_fund_snapshot(projected, '600519')
        assert len(decoded['records']) == 1
        assert decoded['records'][0]['observed_at'] == source['records'][1]['observed_at'].isoformat()
        assert decoded['records'][0]['flows']['main']['net_amount_yuan'] == -20
        assert decode_fund_snapshot(source, '000001')['records'][0]['code'] == '000001'
        projected['quote_chunks']['19'] = b'corrupt'
        with pytest.raises(ValueError, match='checksum'):
            decode_fund_snapshot(projected, '600519')
    asyncio.run(run())


def test_raw_retention_is_five_sessions_and_cannot_touch_daily_or_quote_collections(tmp_path):
    async def run():
        raw = Collection([row('expired', at('2026-10-08')), row('oldest', at('2026-10-09')),
                          row('current', at('2026-10-15'))])
        # Any unexpected collection access fails; daily and quote data cannot be deleted.
        repo = FundFlowRepository({FUND_FLOW_COLLECTION: raw}, spool_root=tmp_path)
        expired_data = data('2026-10-08')
        await repo.save_observation(expired_data)
        result = await repo.delete_expired(calibrated_now=at('2026-10-15', '16:00:00'))
        assert result['retained_trade_dates'] == ['2026-10-09','2026-10-12','2026-10-13','2026-10-14','2026-10-15']
        assert result['deleted'] == {FUND_FLOW_COLLECTION: 2}
        assert set(raw.rows) == {'oldest', 'current'}
        assert (await repo.delete_expired(calibrated_now=at('2026-10-15', '16:00:00')))['deleted'][FUND_FLOW_COLLECTION] == 0
    asyncio.run(run())


def test_daily_uses_post_close_value_without_summing_or_accepting_old_quotes():
    source = data()
    result = daily_rows(source, '2026-11-06')
    assert [r['flows']['main']['net_amount_yuan'] for r in result] == [100, -20]
    assert all(r['finality'] == 'post_close_observation' and not r['is_exchange_final'] for r in result)
    source['records'][1]['quote_time'] = at('2026-11-06', '14:59:59')
    assert len(daily_rows(source, '2026-11-06')) == 1
    source['records'][0]['usable'] = False
    assert not daily_rows(source, '2026-11-06')
    for invalid in [data(clock='14:59:59'), data('2026-11-05')]:
        with pytest.raises(ValueError, match='post_close'):
            daily_rows(invalid, '2026-11-06')


def test_daily_retry_is_idempotent_and_retained_independently(tmp_path):
    class Daily:
        def __init__(self): self.rows = {}
        async def bulk_write(self, ops, ordered):
            for op in ops:
                key = (op._filter['trade_date'], op._filter['code'], op._filter['source'])
                self.rows.setdefault(key, deepcopy(op._doc['$setOnInsert']))
        async def count_documents(self, filters):
            return len(self.rows)
    async def run():
        daily = Daily()
        repo = FundFlowRepository({FUND_FLOW_COLLECTION: Collection(), DAILY_COLLECTION: daily}, spool_root=tmp_path)
        partial = data(); partial['records'][1]['usable'] = False
        assert (await repo.save_daily(partial, '2026-11-06'))['status'] == 'partial'
        assert (await repo.save_daily(data(), '2026-11-06'))['stored_count'] == 2
        changed = data(); changed['records'][0]['flows']['main']['net_amount_yuan'] = 999
        await repo.save_daily(changed, '2026-11-06')
        assert daily.rows[('2026-11-06','000001','EASTMONEY')]['flows']['main']['net_amount_yuan'] == 100
        assert len(daily.rows) == 2
    asyncio.run(run())


class Repo:
    def __init__(self): self.docs = []; self.runs = []; self.done = False; self.closed = None
    async def create_indexes(self): pass
    async def save_observation(self, value):
        self.docs.append(deepcopy(value)); return {'persisted': True, 'compressed_bytes': 100}
    async def record_run(self, kind, target, result): self.runs.append((kind, target, result))
    async def daily_complete(self, day): return self.done
    async def latest_after_close(self, day): return self.closed
    async def save_daily(self, value, day):
        self.done = True
        return {'status': 'success', 'complete': True, 'eligible_count': len(daily_rows(value, day))}


def test_slow_upload_does_not_hold_next_sample_and_wal_survives_restart(tmp_path):
    async def run():
        started, resume = asyncio.Event(), asyncio.Event()
        class SlowCollection(DurableCollection):
            async def update_one(self, *args, **kwargs):
                started.set()
                await resume.wait()
                return await super().update_one(*args, **kwargs)

        coll = SlowCollection()
        repo = FundFlowRepository({FUND_FLOW_COLLECTION: coll}, spool_root=tmp_path, background_upload=True)
        first = await repo.save_observation(data())
        await asyncio.wait_for(started.wait(), 1)
        second = await asyncio.wait_for(repo.save_observation(data(clock='15:06:05')), 1)
        assert all(r['spooled'] and r['persisted'] is None for r in (first, second))
        assert len(list(tmp_path.glob('*.json.gz'))) == 2
        coll.fail = True
        resume.set()
        await repo.finish_upload()
        assert len(list(tmp_path.glob('*.json.gz'))) == 2
        coll.fail = False
        restarted = FundFlowRepository({FUND_FLOW_COLLECTION: coll}, spool_root=tmp_path)
        assert await restarted.replay_pending() == 2
        assert set(coll.rows) == {first['batch_id'], second['batch_id']}
        assert not list(tmp_path.glob('*.json.gz'))
    asyncio.run(run())


def test_fixed_cadence_preserves_incomplete_cycle_and_never_switches_to_minutes(monkeypatch):
    monkeypatch.setattr(service_module, 'server_pressure', lambda: None)
    async def run():
        class Crawler:
            async def fetch(self, day, **kwargs):
                assert kwargs['timeout_seconds'] == 4 and kwargs['max_attempts'] == 1
                result = data(clock='10:00:03')
                result.update(usable_count=0, collection_status='partial', complete=False)
                return result
        repo = Repo(); service = FundFlowService(repo, Crawler())
        for _ in range(4):
            result = await service.sample(now=at('2026-11-06', '10:00:00'))
            assert result['target_poll_seconds'] == 5 and result['status'] == 'partial'
        assert len(repo.docs) == 4
        assert all(d['target_poll_seconds'] == 5 for d in repo.docs)
        assert (await service.sample(now=at('2026-11-06', '12:00:00')))['reason'] == 'outside_session'
    asyncio.run(run())


def test_resource_pressure_records_missing_cycle_without_fetching_or_changing_interval(monkeypatch):
    monkeypatch.setattr(service_module, 'server_pressure', lambda: 'low_available_memory')
    async def run():
        repo = Repo(); service = FundFlowService(repo, object())
        result = await service.sample(now=at('2026-11-06', '10:00:00'))
        assert result == {'status': 'missed', 'reason': 'low_available_memory', 'target_poll_seconds': 5}
        assert not repo.docs and repo.runs[-1][2]['status'] == 'missed'
    asyncio.run(run())


def test_daily_recovers_from_raw_after_weekend_restart_and_never_uses_intraday_only(monkeypatch):
    monkeypatch.setattr(service_module, 'server_pressure', lambda: None)
    async def run():
        repo = Repo(); service = FundFlowService(repo, object())
        result = await service.collect_daily(now=at('2026-11-07'))
        assert result['status'] == 'unavailable' and not result['complete']
        repo.closed = data()
        result = await service.collect_daily(now=at('2026-11-07'))
        assert result['eligible_count'] == 2 and result['complete']
        assert (await service.collect_daily(now=at('2026-11-07')))['reason'] == 'already_collected'
    asyncio.run(run())


def test_proxy_pool_acquisition_cancellation_can_retry(monkeypatch):
    from app.crawlers.proxy_provider import AsyncDailiProxyPool, ProxyEndpoint
    async def run():
        pool = AsyncDailiProxyPool(minutes=3, pool_size=2)
        ready = asyncio.Event()
        async def blocked(count): ready.set(); await asyncio.Event().wait()
        monkeypatch.setattr(pool, '_fetch_proxy_endpoints_async', blocked)
        task = asyncio.create_task(pool.get_requests_proxies()); await ready.wait(); task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not pool._fetching
        async def working(count): return [ProxyEndpoint('127.0.0.1', 9000)]
        monkeypatch.setattr(pool, '_fetch_proxy_endpoints_async', working)
        proxies = await asyncio.wait_for(pool.get_requests_proxies(), timeout=1)
        await pool.on_success_for(proxies); await pool.close()
        assert pool.stats.lease_count == 1
    asyncio.run(run())
