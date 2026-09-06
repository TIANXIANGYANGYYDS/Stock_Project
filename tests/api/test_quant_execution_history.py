from __future__ import annotations

import copy

import pytest

from tests.api.test_api import make_client
from tests.api.test_quant_public import BASE, PRIVATE, assert_public, database_with_document


def history_database():
    db = database_with_document()
    template = db['quant_daily_results'].rows[0]
    rows = []
    for day in ['2026-08-20', '2026-09-03', '2026-09-04']:
        doc = copy.deepcopy(template)
        doc['trade_date'] = day
        doc['updated_at'] = day + 'T15:00:00+08:00'
        doc['runtime'].update(version=1, evaluated_at=day+'T15:00:00+08:00', data_status='closed')
        doc['recording'].update(start_date='2026-08-20', strategy_version='2.0.0',
            computed_at='2026-09-06T17:00:00+08:00', history_rebased_at='2026-09-06T17:01:00+08:00',
            accounting_rebased_at='2026-09-06T20:00:00+08:00')
        events = []
        for code, action, time in [('000001', 'buy', '10:00'), ('000002', 'buy', '11:00'), ('000001', 'sell', '14:00')]:
            events.append({'event_id': day+'-'+code+'-'+action, 'code':code, 'name':code,
                'action':action, 'status':'filled', 'execution_at':day+'T'+time+':00+08:00',
                'signal_at':day+'T'+time+':00+08:00', 'execution_price':10.12345,
                'shares':1000, 'notional':10123.45, 'commission':1.01, 'stamp_duty':0.,
                'reason':PRIVATE})
        # 未成交信号不能变成已成交标记，即使脏数据混入成交数组。
        events.append({'event_id':day+'-pending','code':'000001','action':'buy',
                       'status':'pending_execution','execution_at':None})
        doc['intraday_trading'] = {'count':len(events), 'items':events}
        rows.append(doc)
    # 其他策略即使日期、代码一致也不能混入。
    other = copy.deepcopy(rows[0]); other['strategy_id'] = 'other-private-strategy'
    db['quant_daily_results'].rows = [*rows, other]
    return db


def test_range_filters_inclusive_dates_and_code_then_paginates_executions():
    db = history_database()
    with make_client(db) as client:
        query = 'code=000001&start_date=2026-08-20&end_date=2026-09-04&page_size=2'
        page1 = client.get(f'{BASE}/executions?{query}').json()
        assert page1['query_mode'] == 'date_range'
        assert page1['trade_date'] is None
        assert page1['total'] == 6
        assert page1['snapshot_id'] == page1['history_version']
        assert [r['action'] for r in page1['items']] == ['sell','buy']
        assert [r['trade_date'] for r in page1['items']] == ['2026-09-04']*2
        assert page1['history']['trade_day_count'] == 3
        assert page1['history']['covered_start_date'] == '2026-08-20'
        assert page1['history']['covered_end_date'] == '2026-09-04'
        version = page1['history_version']
        all_items = []
        for page in [1,2,3]:
            response = client.get(f'{BASE}/strategies/strategy_1/executions?{query}&page={page}&history_version={version}')
            assert response.status_code == 200
            result = response.json()
            assert result['history_version'] == version
            all_items.extend(result['items'])
        assert len({r['event_id'] for r in all_items}) == 6
        assert [r['execution_at'] for r in all_items] == sorted((r['execution_at'] for r in all_items), reverse=True)
        for row in all_items:
            assert row['code'] == '000001'
            assert row['status'] == 'filled'
            assert row['execution_kind'] == 'shadow_simulation'
            assert row['marker_type'] == 'simulated_execution'
            assert row['price_basis'] == 'recorded_execution_price'
            assert row['execution_price'] == 10.12345
            assert row['recording']['history_rebased_at'] == page1['history']['history_rebased_at']
        assert_public(page1)
        assert client.get(f'{BASE}/executions?{query}&page=4&history_version={version}').json()['items'] == []


def test_range_action_filter_empty_stock_and_empty_interval_are_not_404():
    with make_client(history_database()) as client:
        query='start_date=2026-09-03&end_date=2026-09-04'
        buys=client.get(f'{BASE}/executions?{query}&code=000001&action=buy').json()
        assert buys['total'] == 2 and all(r['action']=='buy' for r in buys['items'])
        all_codes=client.get(f'{BASE}/executions?{query}').json()
        assert all_codes['total'] == 6
        empty=client.get(f'{BASE}/executions?{query}&code=999999').json()
        assert empty['total'] == 0 and empty['items'] == []
        assert empty['history']['trade_day_count'] == 2
        no_dates=client.get(f'{BASE}/executions?start_date=2026-06-01&end_date=2026-06-30').json()
        assert no_dates['total'] == 0
        assert no_dates['history']['trade_day_count'] == 0
        assert no_dates['history']['covered_start_date'] is None
        assert no_dates['history_version']


@pytest.mark.parametrize('query',[
    'start_date=2026-08-20', 'end_date=2026-09-04',
    'start_date=2026-09-04&end_date=2026-08-20',
    'trade_date=2026-09-04&start_date=2026-08-20&end_date=2026-09-04',
    'start_date=bad&end_date=2026-09-04',
    'start_date=2026-08-20&end_date=2026-09-04&code=ABC',
    'start_date=2026-08-20&end_date=2026-09-04&page_size=201',
])
def test_invalid_range_parameters_return_422(query):
    with make_client(history_database()) as client:
        assert client.get(f'{BASE}/executions?{query}').status_code == 422


@pytest.mark.parametrize('field',['history_rebased_at','computed_at','accounting_rebased_at'])
def test_recomputing_earlier_day_invalidates_range_even_if_latest_day_unchanged(field):
    db=history_database()
    with make_client(db) as client:
        query='code=000001&start_date=2026-08-20&end_date=2026-09-04&page_size=1'
        initial=client.get(f'{BASE}/executions?{query}').json()
        old=initial['history_version']
        db['quant_daily_results'].rows[0]['recording'][field]='2026-09-06T23:00:00+08:00'
        assert client.get(f'{BASE}/executions?{query}&page=2&history_version={old}').status_code==409
        assert client.get(f'{BASE}/executions?{query}&snapshot_id={old}').status_code==409
        fresh=client.get(f'{BASE}/executions?{query}').json()
        assert fresh['history_version']!=old
        # 最新一天的成交源版本不变，区间版本仍应变化。
        assert fresh['items'][0]['snapshot_id']==initial['items'][0]['snapshot_id']


def test_empty_execution_day_participates_in_range_version_and_query_scope_is_bound():
    db=history_database()
    db['quant_daily_results'].rows[0]['intraday_trading']={'count':0,'items':[]}
    with make_client(db) as client:
        query='code=000001&start_date=2026-08-20&end_date=2026-09-04'
        old=client.get(f'{BASE}/executions?{query}').json()['history_version']
        db['quant_daily_results'].rows[0]['runtime']['version']+=1
        assert client.get(f'{BASE}/executions?{query}&history_version={old}').status_code==409
        fresh=client.get(f'{BASE}/executions?{query}').json()['history_version']
        assert client.get(f'{BASE}/executions?{query.replace("000001","000002")}&history_version={fresh}').status_code==409


def test_single_day_default_and_overview_snapshot_remain_compatible():
    with make_client(history_database()) as client:
        overview=client.get(f'{BASE}/overview').json()['data']
        day=client.get(f'{BASE}/executions?code=000001&snapshot_id={overview["snapshot_id"]}').json()
        assert day['query_mode']=='single_day' and day['trade_date']=='2026-09-04'
        assert day['snapshot_id']==overview['snapshot_id']
        assert day['total']==2
        exact=client.get(f'{BASE}/executions?code=000001&trade_date=2026-09-04').json()
        assert day==exact
        ranged=client.get(f'{BASE}/executions?code=000001&start_date=2026-09-04&end_date=2026-09-04').json()
        assert ranged['items']==day['items']
        assert ranged['history_version']==day['history_version']
        assert client.get(f'{BASE}/executions?code=000001&history_version=expired').status_code==409
        assert client.get(f'{BASE}/executions?trade_date=2026-01-01').status_code==404
        assert client.get(f'{BASE}/strategies/missing/executions?start_date=2026-08-20&end_date=2026-09-04').status_code==404
