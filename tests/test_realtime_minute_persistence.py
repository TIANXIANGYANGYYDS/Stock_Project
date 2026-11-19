"""Recovery from partial Mongo writes must preserve real observations exactly once."""
import asyncio
from dataclasses import replace
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest
from pymongo.errors import AutoReconnect

import app.services.realtime_minute_service as module
from app.services.realtime_minute_service import RealtimeMinuteService, CN_TZ
from tests.test_realtime_minute_service import service_quote


def test_migrated_minute_collection_does_not_rescan_history_on_startup():
    from app.repositories.realtime_minute_bar_repository import RealtimeMinuteBarRepository

    class Collection:
        async def index_information(self):
            return {'uniq_realtime_code_interval_timestamp': {}}

        async def update_many(self, *args):
            raise AssertionError('migrated minute history must not be scanned')

        async def create_index(self, *args, **kwargs):
            pass

    repository = RealtimeMinuteBarRepository.__new__(RealtimeMinuteBarRepository)
    repository.collection = Collection()
    asyncio.run(repository.create_indexes())


class PartialRepository:
    def __init__(self, fail_calls=()):
        self.fail_calls = set(fail_calls)
        self.calls = []
        self.rows = {}

    async def create_indexes(self):
        pass

    async def find_bar_keys(self, keys):
        return [self.rows[(k['code'], k['interval'], k['timestamp'])] for k in keys
                if (k['code'], k['interval'], k['timestamp']) in self.rows]

    async def upsert_bars(self, bars):
        docs = [bar.model_dump() for bar in bars]
        self.calls.append(docs)
        fail = len(self.calls) in self.fail_calls
        for row in docs[:max(1, len(docs)//2)] if fail else docs:
            self.rows[(row['code'], row['interval'], row['timestamp'])] = row
        if fail:
            raise AutoReconnect('injected lost acknowledgement after partial write')
        return len(docs)


def quote(minute=30, second=5, volume=1000, price=10.):
    return service_quote(price=price, volume=volume, amount=volume*price,
                         received_at=datetime(2026, 9, 16, 9, minute, second, tzinfo=CN_TZ))


def test_bounded_minute_flush_resumes_all_unwritten_symbols(monkeypatch):
    monkeypatch.setattr(module, 'REALTIME_PERSIST_STOCK_BATCH_SIZE', 1)
    async def run():
        service = RealtimeMinuteService()
        service.repository = PartialRepository()
        try:
            for minute in (30, 31):
                for i in range(6):
                    service._ingest_quote(replace(quote(minute=minute), code=f'{i:06d}'))
            cutoff = '2026-09-16T09:31:00+08:00'
            await service._flush_before(cutoff, per_symbol=True, next_poll=0.)
            assert sum(k[1] < cutoff for k in service._bars) == 3
            await service._flush_before(cutoff, per_symbol=True, next_poll=0.)
            assert all(k[1] == cutoff for k in service._bars)
            await service._flush_before(None, force=True)
            assert len([k for k in service.repository.rows if k[1] == '1m']) == 12
        finally:
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('error', [TimeoutError, AutoReconnect])
def test_resume_failure_preserves_afternoon_first_minute_and_volume(error):
    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository()
        service.repository = repo
        original = repo.find_bar_keys
        requests = []

        async def interrupted(keys):
            requests.append(keys)
            if len(requests) == 1:
                raise error('temporary read failure')
            return await original(keys)

        repo.find_bar_keys = interrupted
        first_at = datetime(2026, 9, 16, 13, 0, 5, tzinfo=CN_TZ)
        first = service_quote(price=10, volume=1000, amount=10000, received_at=first_at)
        later = replace(first, market_data_time=first_at + timedelta(minutes=1),
                        received_at=first_at + timedelta(minutes=1), volume=1400, amount=14000)
        try:
            assert await service._quotes_after_resume([first], first_at, time(13), time(15)) == []
            ready = await service._quotes_after_resume([later], later.received_at, time(13), time(15))
            assert ready == [first, later]
            assert requests[1] == requests[0]  # Restore the earliest deferred minute.
            for item in ready:
                service._ingest_quote(item)
            await service._flush_before(None, force=True)
            bars = sorted((r for r in repo.rows.values() if r['interval'] == '1m'),
                          key=lambda r: r['timestamp'])
            assert [b['timestamp'][11:16] for b in bars] == ['13:00', '13:01']
            assert bars[0]['first_seen_at'] == first_at
            assert sum(b['volume'] for b in bars) == 400
            assert not service._deferred_resume_quotes
        finally:
            await service.close()
    asyncio.run(run())


def test_resume_partial_success_does_not_delay_or_repeat_restored_symbols(monkeypatch):
    monkeypatch.setattr(module, 'REALTIME_PERSIST_STOCK_BATCH_SIZE', 1)

    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository()
        service.repository = repo
        calls = []

        async def read(keys):
            calls.append(keys[0]['code'])
            if len(calls) == 2:
                raise TimeoutError()
            return []

        repo.find_bar_keys = read
        first, second = quote(), replace(quote(), code='000002')
        try:
            assert await service._quotes_after_resume([first, second], first.received_at,
                                                      time(9, 30), time(11, 30)) == [first]
            assert await service._quotes_after_resume([], first.received_at,
                                                      time(9, 30), time(11, 30)) == [second]
            assert calls == ['000001', '000002', '000002']
        finally:
            await service.close()
    asyncio.run(run())


def test_grouped_resume_query_preserves_exact_key_membership():
    from app.repositories.realtime_minute_bar_repository import RealtimeMinuteBarRepository
    from tests.api.conftest import matches
    keys = [dict(code='000001', interval='1m', timestamp='13:00'),
            dict(code='000002', interval='1m', timestamp='13:00'),
            dict(code='000003', interval='1m', timestamp='13:01')]
    rows = keys + [dict(code='000003', interval='1m', timestamp='13:00'),
                   dict(code='000001', interval='5m', timestamp='13:00')]

    class Collection:
        def find(self, query, projection):
            assert len(query['$or']) == 2
            selected = [row for row in rows if matches(row, query)]
            async def to_list(length): return selected[:length]
            return SimpleNamespace(hint=lambda _: SimpleNamespace(to_list=to_list))

    repo = RealtimeMinuteBarRepository.__new__(RealtimeMinuteBarRepository)
    repo.collection = Collection()
    assert asyncio.run(repo.find_bar_keys(keys)) == keys


def test_afternoon_preparation_accepts_fast_host_clock_and_slow_universe(monkeypatch):
    current = datetime(2026, 9, 16, 13, 0, tzinfo=CN_TZ)
    offset = timedelta(seconds=347)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return current

    async def trade_day(_):
        return SimpleNamespace(is_reference_trade_day=True, reference_trade_date='2026-09-16')

    async def sleep(seconds):
        nonlocal current
        current += timedelta(seconds=seconds)

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'resolve_a_stock_target_trade_date', trade_day)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)

    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository()
        service.repository = repo
        calls = []
        async def noop(*args, **kwargs): pass
        service.snapshots = SimpleNamespace(create_indexes=noop, replay_pending=noop, save_cycle=noop)
        await service.crawler.close()

        class Crawler:
            async def clock_offset(self): return -347
            async def close(self): pass
            async def fetch_quotes(self, codes):
                nonlocal current
                at = current - offset
                calls.append(at)
                item = service_quote(price=10, volume=1000, amount=10000, received_at=at)
                current = current.replace(hour=15, minute=12)
                return [item], dict(requested=1, returned=1, requests=1, fallback_batches=0,
                                    failed_batches=0, elapsed_ms=1)

        service.crawler = Crawler()
        async def universe(_, *, refresh_public=True):
            if refresh_public:
                await asyncio.Event().wait()  # Public API can hang without delaying capture.
            return [{'code': '000001', 'name': 'test'}]
        service._load_universe = universe
        try:
            result = await service.run_session('afternoon')
            assert result['status'] == 'completed'
            assert calls[0].strftime('%H:%M:%S') == '13:00:00'
            assert ('000001', '1m', '2026-09-16T13:00:00+08:00') in repo.rows
        finally:
            await service.close()
    asyncio.run(run())


def test_partial_write_retries_identical_payload_without_double_aggregation(monkeypatch):
    monkeypatch.setattr(module, 'REALTIME_PERSIST_RETRY_SECONDS', 0)

    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository({1})
        service.repository = repo
        try:
            service._ingest_quote(quote())
            service._ingest_quote(quote(second=10, volume=1300, price=10.1))
            service._ingest_quote(quote(minute=31, volume=1600, price=10.2))
            await service._flush_before('2026-09-16T09:32:00+08:00')
            assert repo.calls[0] == repo.calls[1]
            assert not service._bars
            aggregate = repo.rows[('000001', '5m', '2026-09-16T09:30:00+08:00')]
            assert aggregate['volume'] == 600
            assert aggregate['close'] == 10.2
            assert len([r for r in repo.rows.values() if r['interval']=='1m']) == 2
        finally:
            await service.close()
    asyncio.run(run())


def test_exhausted_retry_keeps_unacknowledged_chunk_and_rolls_back_aggregates(monkeypatch):
    monkeypatch.setattr(module, 'REALTIME_PERSIST_RETRY_SECONDS', 0)

    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository({2,3,4})
        service.repository = repo
        try:
            service._ingest_quote(quote())
            service._ingest_quote(quote(second=10, volume=1300))
            await service._flush_before('2026-09-16T09:31:00+08:00')
            service._ingest_quote(quote(minute=31, volume=1600))
            with pytest.raises(AutoReconnect):
                await service._flush_before('2026-09-16T09:32:00+08:00')
            assert set(service._bars) == {('000001','2026-09-16T09:31:00+08:00')}
            assert service._aggregate_bars[('000001','5m','2026-09-16T09:30:00+08:00')].volume == 300
            # New genuine observations may arrive before persistence recovers.
            service._ingest_quote(quote(minute=31, second=30, volume=1700))
            service._ingest_quote(quote(minute=32, volume=1900))
            await service._flush_before(None, force=True)
            assert not service._bars and not service._aggregate_bars
            assert repo.rows[('000001','5m','2026-09-16T09:30:00+08:00')]['volume'] == 900
            assert sum(r['volume'] for r in repo.rows.values() if r['interval']=='1m') == 900
        finally:
            await service.close()
    asyncio.run(run())


def test_failed_later_batch_does_not_discard_other_unwritten_minutes(monkeypatch):
    monkeypatch.setattr(module, 'REALTIME_PERSIST_STOCK_BATCH_SIZE', 1)
    monkeypatch.setattr(module, 'REALTIME_PERSIST_RETRY_SECONDS', 0)

    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository({2,3,4})
        service.repository = repo
        try:
            for code in ['000001','000002','000003']:
                service._ingest_quote(replace(quote(),code=code))
            with pytest.raises(AutoReconnect):
                await service._flush_before(None,force=True)
            assert {c for c,_ in service._bars} == {'000002','000003'}
            await service._flush_before(None,force=True)
            assert not service._bars
            assert {r['code'] for r in repo.rows.values() if r['interval']=='1m'} == {'000001','000002','000003'}
        finally:
            await service.close()
    asyncio.run(run())


def test_cancellation_restores_pending_observations_without_duplicate_volume():
    async def run():
        service = RealtimeMinuteService()
        repo = PartialRepository()
        service.repository = repo
        original = repo.upsert_bars

        async def cancelled(bars):
            await original(bars)
            raise asyncio.CancelledError()

        try:
            service._ingest_quote(quote())
            service._ingest_quote(quote(second=10, volume=1300))
            repo.upsert_bars = cancelled
            with pytest.raises(asyncio.CancelledError):
                await service._flush_before(None, force=True)
            assert len(service._bars) == 1 and not service._aggregate_bars
            repo.upsert_bars = original
            await service._flush_before(None, force=True)
            assert repo.rows[('000001','5m','2026-09-16T09:30:00+08:00')]['volume'] == 300
        finally:
            await service.close()
    asyncio.run(run())


def test_session_continues_polling_after_transient_write_retries_exhaust(monkeypatch):
    monkeypatch.setattr(module,'REALTIME_PERSIST_RETRY_SECONDS',0)
    current = datetime(2026,9,16,9,30,tzinfo=CN_TZ)

    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return current

    async def trade_day(_):
        return SimpleNamespace(is_reference_trade_day=True,reference_trade_date='2026-09-16')

    async def sleep(_):
        pass

    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(module,'resolve_a_stock_target_trade_date',trade_day)
    monkeypatch.setattr(module.asyncio,'sleep',sleep)

    async def run():
        nonlocal current
        service=RealtimeMinuteService()
        class Snapshots:
            async def create_indexes(self):pass
            async def replay_pending(self, **kwargs):pass
            async def save_cycle(self, *args, **kwargs):pass
        service.snapshots=Snapshots()
        await service.crawler.close()
        repo=PartialRepository({1,2,3});service.repository=repo
        class Crawler:
            calls=0
            async def fetch_quotes(self,codes):
                nonlocal current
                self.calls+=1
                q=quote(minute=29+self.calls,volume=700+300*self.calls)
                current=q.received_at if self.calls<3 else datetime(2026,9,16,11,36,tzinfo=CN_TZ)
                return [q],dict(requested=1,returned=1,requests=1,fallback_batches=0,failed_batches=0,elapsed_ms=1)
            async def close(self):pass
        service.crawler=Crawler()
        async def universe(_, **kwargs):return [{'code':'000001','name':'test'}]
        service._load_universe=universe
        try:
            result=await service.run_session('morning')
            assert result['status']=='completed' and result['cycles']==3
            assert {r['timestamp'] for r in repo.rows.values() if r['interval']=='1m'} == {
                '2026-09-16T09:30:00+08:00','2026-09-16T09:31:00+08:00','2026-09-16T09:32:00+08:00'}
            assert not service._bars
        finally:
            await service.close()
    asyncio.run(run())


def test_slow_symbol_is_not_finalized_by_faster_symbol_and_old_quote_cannot_reopen_it():
    async def run():
        service=RealtimeMinuteService(); repo=PartialRepository();service.repository=repo
        try:
            service._ingest_quote(quote(second=5, volume=1000, price=10))
            service._ingest_quote(replace(quote(minute=31,volume=5000),code='600001'))
            await service._flush_before('2026-09-16T09:31:00+08:00',per_symbol=True)
            assert ('000001','2026-09-16T09:30:00+08:00') in service._bars
            assert not repo.rows
            service._ingest_quote(quote(second=55,volume=1400,price=10.5))
            service._ingest_quote(quote(minute=31,volume=1500,price=10.4))
            await service._flush_before('2026-09-16T09:31:00+08:00',per_symbol=True)
            old=repo.rows[('000001','1m','2026-09-16T09:30:00+08:00')].copy()
            assert old['open']==10 and old['high']==10.5 and old['volume']==400
            service._ingest_quote(quote(second=58,volume=1450,price=10.2))
            await service._flush_before(None,force=True)
            assert repo.rows[('000001','1m','2026-09-16T09:30:00+08:00')]==old
            assert repo.rows[('000001','5m','2026-09-16T09:30:00+08:00')]['volume']==500
        finally: await service.close()
    asyncio.run(run())


def test_halted_symbol_does_not_prevent_other_aggregate_buckets_from_expiring():
    async def run():
        service=RealtimeMinuteService();service.repository=PartialRepository()
        try:
            service._ingest_quote(quote())
            for minute in (30,31,35):
                service._ingest_quote(replace(quote(minute=minute,volume=minute*100),code='600001'))
                await service._flush_before(f'2026-09-16T09:{minute}:00+08:00',per_symbol=True)
            assert ('600001','5m','2026-09-16T09:30:00+08:00') not in service._aggregate_bars
            assert ('000001','2026-09-16T09:30:00+08:00') in service._bars
        finally: await service.close()
    asyncio.run(run())


def test_receipt_time_is_not_backdated_by_source_clock_estimate():
    async def run():
        service=RealtimeMinuteService()
        try:
            q=quote()
            q=replace(q,received_at=q.received_at.replace(minute=33))
            service._observe_source_clock(q)
            service._ingest_quote(q)
            bar=next(iter(service._bars.values()))
            assert service._clock_offset_seconds()==-180
            assert bar.first_seen_at==q.received_at
            assert bar.timestamp.endswith('09:30:00+08:00')
        finally: await service.close()
    asyncio.run(run())


def test_close_packets_keep_last_minute_until_session_final_flush():
    async def run():
        service=RealtimeMinuteService();repo=PartialRepository();service.repository=repo
        try:
            q=quote();q=replace(q,market_data_time=datetime(2026,9,16,15,0,0,tzinfo=CN_TZ))
            end_bucket=datetime(2026,9,16,14,59,59,tzinfo=CN_TZ)
            service._ingest_quote(q,bar_time=end_bucket)
            await service._flush_before('2026-09-16T15:00:00+08:00',per_symbol=True)
            assert not repo.rows
            service._ingest_quote(replace(q,market_data_time=q.market_data_time.replace(second=30),
                                         volume=q.volume+500,amount=q.amount+5500,price=11),bar_time=end_bucket)
            await service._flush_before('2026-09-16T15:00:00+08:00',per_symbol=True)
            assert not repo.rows
            await service._flush_before(None,force=True)
            bar=repo.rows[('000001','1m','2026-09-16T14:59:00+08:00')]
            assert bar['open']==10 and bar['close']==11 and bar['volume']==500
        finally: await service.close()
    asyncio.run(run())


def test_watchdog_restart_keeps_existing_minute_and_restores_aggregate_volume():
    async def run():
        first=RealtimeMinuteService();second=RealtimeMinuteService();repo=PartialRepository()
        first.repository=repo;second.repository=repo
        try:
            first._ingest_quote(quote())
            first._ingest_quote(quote(second=20,volume=1300,price=10.2))
            await first._flush_before('2026-09-16T09:31:00+08:00')
            old=repo.rows[('000001','1m','2026-09-16T09:30:00+08:00')].copy()
            # A watchdog restart receives a still-current quote for an
            # already persisted minute. Do not overwrite or double count it.
            q=quote(second=40,volume=1400,price=10.4)
            await second._restore_resume_buckets([q],q.received_at,module.MORNING[0],module.MORNING[1])
            second._ingest_quote(q)
            assert not second._bars
            second._ingest_quote(quote(minute=31,volume=1500,price=10.5))
            await second._flush_before(None,force=True)
            assert repo.rows[('000001','1m','2026-09-16T09:30:00+08:00')]==old
            aggregate=repo.rows[('000001','5m','2026-09-16T09:30:00+08:00')]
            assert aggregate['volume']==400 and aggregate['open']==10 and aggregate['close']==10.5
        finally:await first.close();await second.close()
    asyncio.run(run())
