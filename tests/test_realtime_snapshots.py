import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pymongo.errors import AutoReconnect

from app.crawlers.realtime_market_crawler import (
    CN_TZ, QuoteBatchResult, RealtimeMarketCrawler, quote_issue, quote_phase,
)
from app.repositories.realtime_snapshot_repository import (
    RealtimeSnapshotRepository, decode_batch, snapshot_row,
)
from tests.test_realtime_minute_persistence import quote


class Collection:
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.ack_lost = False

    async def update_one(self, key, update, upsert):
        if self.fail:
            raise AutoReconnect('offline')
        self.rows.setdefault(key['_id'], update['$setOnInsert'])
        if self.ack_lost:
            self.ack_lost = False
            raise AutoReconnect('ack lost')


def test_snapshot_spool_survives_restart_and_lost_ack(tmp_path):
    async def run():
        collection = Collection()
        repo = RealtimeSnapshotRepository({repo_name: collection for repo_name in ['stock_realtime_quote_batches']}, spool_root=tmp_path)
        collection.ack_lost = True
        q = quote()
        result = await repo.save_cycle([q], expected_codes=['000001', '000002'],
                                      session='morning', observed_at=q.received_at,
                                      received_at=q.received_at, metrics={})
        assert not result['persisted']
        assert len(list(tmp_path.glob('*.json.gz'))) == 1
        restored = RealtimeSnapshotRepository({'stock_realtime_quote_batches': collection}, spool_root=tmp_path)
        assert await restored.replay_pending() == 1
        assert not list(tmp_path.glob('*.json.gz'))
        assert len(collection.rows) == 1
        document = next(iter(collection.rows.values()))
        assert document['missing_codes'] == ['000002']
        assert decode_batch(document)['quotes'][0]['received_at'] == q.received_at.isoformat()
        corrupted = dict(document, payload=b'corrupted')
        with pytest.raises(ValueError, match='checksum'):
            decode_batch(corrupted)
    asyncio.run(run())


def test_auction_phase_uses_source_clock_and_keeps_raw_book():
    for clock, phase in [('09:15:00', 'opening_auction_cancelable'),
                         ('09:20:00', 'opening_auction_locked'),
                         ('09:25:00', 'opening_result'),
                         ('09:30:00', 'continuous'), ('14:57:00', 'closing_auction')]:
        source = datetime.fromisoformat('2026-09-16T' + clock + '+08:00')
        q = replace(quote(), market_data_time=source, received_at=source+timedelta(minutes=5),
                    server_time=source, bids=((10, 500), (0, 200)), asks=((10, 500), (0, 0)),
                    raw_fields=('original',))
        row = snapshot_row(q)
        assert row['phase'] == phase
        assert row['bids'][1] == [0, 200]
        assert row['raw_fields'] == ['original']
        assert row['quality_issue'] is None
    assert quote_phase(source.replace(hour=12)) == 'outside_session'


def test_invalid_and_stale_quotes_preserved_but_not_treated_as_live():
    q = quote()
    invalid = replace(q, price=float('nan'))
    assert snapshot_row(invalid)['price'] is None
    assert quote_issue(invalid) == 'invalid_values'
    assert quote_issue(replace(q, market_data_time=q.market_data_time-timedelta(minutes=2))) == 'stale_source_time'
    assert quote_issue(replace(q, market_data_time=q.market_data_time+timedelta(minutes=2))) == 'future_source_time'
    # Zero trade price before the auction uncrosses is a valid observation.
    at = q.market_data_time.replace(hour=9, minute=20)
    assert quote_issue(replace(q, price=0, market_data_time=at, received_at=at)) is None
    result_at = at.replace(minute=25)
    assert quote_issue(replace(q, market_data_time=result_at, received_at=result_at.replace(minute=29))) is None


def test_complete_but_stale_primary_uses_fresh_backup_and_archives_both():
    async def run():
        crawler = RealtimeMarketCrawler()
        await crawler.close()
        current = quote()
        old = replace(current, market_data_time=current.market_data_time-timedelta(minutes=2))
        class Provider:
            def __init__(self, value):self.value=value;self.calls=0
            async def fetch_batch(self, codes):
                self.calls+=1
                return QuoteBatchResult('TEST', 1, 1, 200, 1, 1, (self.value,))
        crawler.primary = Provider(old)
        crawler.backup = Provider(current)
        values, metrics = await crawler.fetch_quotes(['000001'])
        assert values == [current]
        assert crawler.last_observations == [old, current]
        assert metrics['fallback_batches'] == 1
        assert metrics['unusable_symbols'] == 0
    asyncio.run(run())


def test_repeated_stale_sources_have_bounded_retry_cost():
    async def run():
        crawler = RealtimeMarketCrawler()
        await crawler.close()
        q = quote()
        q = replace(q, market_data_time=q.market_data_time-timedelta(minutes=2))
        class Provider:
            calls = 0
            async def fetch_batch(self, codes):
                self.calls+=1
                return QuoteBatchResult('TEST', 1, 1, 200, 1, 1, (q,))
        crawler.primary = Provider();crawler.backup = Provider()
        await crawler.fetch_quotes(['000001'])
        await crawler.fetch_quotes(['000001'])
        assert crawler.primary.calls == 2 and crawler.backup.calls == 1
    asyncio.run(run())


def test_snapshot_api_filters_symbol_and_bounds_window(tmp_path):
    from app.api.routers.stocks import list_stock_snapshots
    from datetime import date
    from fastapi import HTTPException
    async def run():
        saved = Collection()
        repo = RealtimeSnapshotRepository({'stock_realtime_quote_batches': saved}, spool_root=tmp_path)
        q = quote()
        await repo.save_cycle([q, replace(q, code='000002')], expected_codes=['000001', '000002'],
                             session='morning', observed_at=q.received_at,
                             received_at=q.received_at, metrics={})
        class Cursor:
            def sort(self, fields):return self
            def limit(self, value):return self
            async def __aiter__(self):
                for row in saved.rows.values():yield row
        class Query:
            def find(self, filters):
                assert filters['trade_date'] == '2026-09-16'
                return Cursor()
        db = {'stock_realtime_quote_batches': Query()}
        result = await list_stock_snapshots('000001', date(2026,9,16), '09:30:00', '09:31:00', 200, db)
        assert len(result['data']) == 1 and result['data'][0]['code'] == '000001'
        assert result['batches'] == 1
        with pytest.raises(HTTPException):
            await list_stock_snapshots('000001', date(2026,9,16), '09:00:00', '15:00:00', 200, db)
    asyncio.run(run())


def test_clock_calibration_requires_two_agreeing_independent_sources():
    from email.utils import format_datetime
    async def run():
        crawler = RealtimeMarketCrawler()
        await crawler.close()
        class Client:
            async def get(self, url, **kwargs):
                at = datetime.now(CN_TZ)-timedelta(seconds=290)
                return SimpleNamespace(status_code=200, headers={'date':format_datetime(at)})
        crawler.primary = SimpleNamespace(client=Client())
        offset = await crawler.clock_offset()
        assert -292 < offset < -288
    asyncio.run(run())


def test_unavailable_symbol_during_fallback_cooldown_does_not_abort_market():
    async def run():
        crawler = RealtimeMarketCrawler()
        await crawler.close()
        class Provider:
            async def fetch_batch(self, codes):
                return QuoteBatchResult('TEST', 1, 0, 200, 1, 1, ())
        crawler.primary = Provider();crawler.backup = Provider()
        await crawler.fetch_quotes(['000001'])
        rows, metrics = await crawler.fetch_quotes(['000001'])
        assert rows == [] and metrics['missing_symbols'] == 1
        assert metrics['failed_batches'] == 1
    asyncio.run(run())


def test_auction_quote_without_last_trade_details_still_preserves_book():
    from app.crawlers.realtime_market_crawler import TencentQuoteProvider
    async def run():
        provider = TencentQuoteProvider()
        fields = [''] * 40
        fields[1:7] = ['股票', '000001', '', '10', '', '0']
        fields[9:29] = ['10', '50'] * 10
        fields[30] = '20260916092000'
        at = datetime(2026,9,16,9,20,tzinfo=CN_TZ)
        try:
            rows = provider._parse('v_sz000001="'+'~'.join(fields)+'";', at, {'000001'})
            assert len(rows) == 1 and rows[0].bids[0] == (10, 5000)
            assert rows[0].amount == 0 and quote_issue(rows[0]) is None
            assert rows[0].raw_fields[35] == ''
            assert rows[0].amount_basis == 'rounded_ten_thousand_cny'
        finally:
            await provider.close()
    asyncio.run(run())


def test_session_archives_auction_but_only_aggregates_continuous_trades(monkeypatch):
    import app.services.realtime_minute_service as module
    from tests.test_realtime_minute_persistence import PartialRepository
    current = datetime(2026,9,16,9,15,tzinfo=CN_TZ)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):return current
    async def trade_day(_):
        return SimpleNamespace(is_reference_trade_day=True, reference_trade_date='2026-09-16')
    async def sleep(_):pass
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'resolve_a_stock_target_trade_date', trade_day)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    async def run():
        nonlocal current
        service = module.RealtimeMinuteService()
        await service.crawler.close()
        repo = PartialRepository();service.repository = repo
        archived = []
        class Snapshots:
            async def create_indexes(self):pass
            async def replay_pending(self, **kwargs):pass
            async def save_cycle(self, quotes, **kwargs):archived.extend(quotes)
        class Crawler:
            calls = 0
            async def fetch_quotes(self, codes):
                nonlocal current
                minute, volume = [(15,0),(20,0),(25,5000),(30,5200),(31,5400)][self.calls]
                at = current.replace(hour=9, minute=minute)
                q = replace(quote(), market_data_time=at, received_at=at, volume=volume, amount=volume*10)
                self.calls += 1
                current = at if self.calls < 5 else at.replace(hour=11, minute=36)
                return [q],dict(requested=1,returned=1,requests=1,fallback_batches=0,failed_batches=0,elapsed_ms=1)
            async def close(self):pass
        async def universe(_):return [{'code':'000001','name':'test'}]
        service.crawler=Crawler();service.snapshots=Snapshots();service._load_universe=universe
        try:
            await service.run_session('morning')
            assert len(archived) == 5
            minutes=[row for row in repo.rows.values() if row['interval']=='1m']
            assert len(minutes) == 2
            assert min(row['timestamp'] for row in minutes).endswith('09:30:00+08:00')
            assert sum(row['volume'] for row in minutes) == 400
        finally:await service.close()
    asyncio.run(run())
