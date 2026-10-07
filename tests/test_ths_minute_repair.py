from datetime import datetime
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.manually_execute_script.repair_realtime_minutes_ths import normalize, insert_missing, minute_starts
from app.crawlers.realtime_market_crawler import RealtimeMarketCrawler, QuoteBatchResult
from tests.test_realtime_minute_persistence import quote


def row(clock='09:31:00', **kw):
    return dict(code='000001', key=f'2026-09-17T{clock}+08:00', open=10., high=10.1, low=9.9,
                close=10., volume=100., amount=1000., **kw)


def normalized(value):
    return normalize(value, day='2026-09-17', name=None,
                     retrieved_at=datetime.fromisoformat('2026-09-19T10:00:00+08:00'),
                     run_id='test', response_sha256='abc')


def test_auction_lunch_and_future_are_not_shifted_into_missing_minutes():
    assert len(minute_starts('2026-09-17')) == 240
    assert normalized(row('09:30:00')) is None
    assert normalized(row('13:00:00')) is None
    assert normalized(row('11:31:00')) is None
    assert normalized(dict(row(),key='2026-09-18T09:31:00+08:00')) is None
    assert normalized(row())['timestamp'] == '2026-09-17T09:30:00+08:00'
    assert normalized(row('11:30:00'))['timestamp'].endswith('11:29:00+08:00')
    assert normalized(row('13:01:00'))['timestamp'].endswith('13:00:00+08:00')
    assert normalized(row('15:00:00'))['timestamp'].endswith('14:59:00+08:00')
    assert normalized(row())['first_seen_at'].day == 19


def test_zero_volume_is_observation_but_invalid_prices_are_not_accepted():
    assert normalized(dict(row(),volume=0,amount=0))['volume'] == 0
    for changes in ({'close':float('nan')},{'low':10.2},{'volume':-1},{'close':0}):
        with pytest.raises(ValueError):
            normalized(dict(row(),**changes))


def test_backfill_is_idempotent_and_never_overwrites_existing_observations():
    old=normalized(row()); old['provider']='TENCENT';old['close']=10.05
    rows={old['timestamp']:old.copy()}
    class Collection:
        def bulk_write(self, operations, ordered):
            count=0
            for op in operations:
                assert set(op._doc) == {'$setOnInsert'}
                key=op._filter['timestamp']
                if key not in rows:
                    rows[key]=op._doc['$setOnInsert'];count+=1
            return SimpleNamespace(upserted_count=count)
    docs=[normalized(row()),normalized(row('09:32:00'))]
    assert insert_missing(Collection(),docs)==1
    assert insert_missing(Collection(),docs)==0
    assert rows[old['timestamp']]==old


def test_two_incomplete_sources_keep_valid_quotes_and_only_retry_missing_symbols():
    async def run():
        crawler=RealtimeMarketCrawler();await crawler.close()
        base=quote()
        class Provider:
            def __init__(self, codes):self.codes=codes;self.calls=[]
            async def fetch_batch(self,codes):
                self.calls.append(codes)
                quotes=tuple(replace(base,code=c) for c in codes if c in self.codes)
                return QuoteBatchResult('TEST',len(codes),len(quotes),200,1,1,quotes)
        crawler.primary=Provider({'000001'})
        crawler.backup=Provider({'000002'})
        values,metrics=await crawler.fetch_quotes(['000001','000002','000003'])
        assert [q.code for q in values]==['000001','000002']
        assert crawler.backup.calls==[['000002','000003']]
        assert metrics['missing_symbols']==1 and metrics['failed_batches']==1
    asyncio.run(run())


def test_http_200_with_wrong_historical_window_is_rejected():
    from app.manually_execute_script.validate_stock_history_against_ths import fetch_ths_direct_bars
    target=int(datetime.fromisoformat('2026-09-17T15:00:00+08:00').timestamp()*1000)
    class Session:
        def post(self,*args,**kwargs):
            payload={'status_code':0,'data':{'quote_data':[{'code':'000001',
                'data_fields':['1','7','8','9','11','13','19'],
                'value':[[target+86400000,10,10,10,10,0,0]]}]}}
            return SimpleNamespace(status_code=200,json=lambda:payload)
    rows,audit=fetch_ths_direct_bars(Session(),headers={},code='000001',market='33',
        time_period='min_1',end_time_ms=target,count=241,allow_partial=True,
        max_attempts=2,retry_delay=0)
    assert rows is None and audit['rejected_windows']==2


def test_recovery_watchdog_does_not_start_a_second_active_session(monkeypatch):
    from app.scheduler import crawler_jobs
    lock=asyncio.Lock()
    monkeypatch.setattr(crawler_jobs,'_realtime_minute_job_lock',lock)
    async def run():
        async with lock:
            # It must return before constructing a crawler or touching Mongo.
            await asyncio.wait_for(crawler_jobs.realtime_minute_session_job(session='morning'),1)
    asyncio.run(run())


def test_long_suspension_stays_in_universe_without_introducing_future_listing():
    from app.manually_execute_script.repair_realtime_minutes_ths import prior_universe
    class Collection:
        def distinct(self,field):return ['002084','000001','301999']
        def find_one(self,query,projection,sort,hint):
            assert query['trade_date']=={'$lt':'2026-08-20'}
            return {'code':query['code'],'name':'历史已知'} if query['code']!='301999' else None
    assert set(prior_universe(Collection(),'2026-08-20'))=={'002084','000001'}


def test_rollup_requires_all_constituents_and_preserves_existing(monkeypatch):
    from app.manually_execute_script import repair_realtime_minutes_ths as module
    rows = [normalized(row(f'09:{minute:02}:00')) for minute in range(31, 36)]
    inserted = []
    class Collection:
        def find(self, *args):return rows
    monkeypatch.setattr(module, 'insert_missing', lambda collection, docs: inserted.extend(docs) or len(docs))
    assert module.insert_missing_aggregates(Collection(), '000001', '2026-09-17') == 1
    assert inserted[0]['interval'] == '5m'
    assert inserted[0]['volume'] == 500
    assert inserted[0]['constituent_count'] == 5
    rows.pop(2)
    inserted.clear()
    assert module.insert_missing_aggregates(Collection(), '000001', '2026-09-17') == 0


def test_inventory_refresh_includes_newly_arrived_daily_codes(tmp_path, monkeypatch):
    from app.manually_execute_script import repair_realtime_minutes_ths as module
    day = '2026-09-30'
    module.write_json(tmp_path / f'inventory_{day}.json', {'summary': {}, 'gaps': []})
    class Cursor(list):
        def hint(self, *args):return self
    class Daily:
        def distinct(self, *args):return [day]
        def find(self, *args):return Cursor([{'code': '301716', 'name': '新股'}])
    class Minutes:
        def distinct(self, *args):return []
        def aggregate(self, *args, **kwargs):return []
    class DB:
        stock_daily_detail = Daily()
        def __getitem__(self, key):return Minutes()
    monkeypatch.setattr(module, 'prior_universe', lambda *args: {})
    tasks = module.inventory(DB(), tmp_path, day, day)
    assert tasks[0]['code'] == '301716'
    assert len(tasks[0]['missing']) == 240
