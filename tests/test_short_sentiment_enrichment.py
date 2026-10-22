"""Publication contracts, causal clocks, and observable price-window behavior."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_db
from app.repositories.market_observation_repository import HOT_COLLECTION
from app.repositories.short_sentiment_repository import ShortSentimentRepository, TIMESERIES
from app.services.short_sentiment.environment_runtime import VERSION
from app.services.short_sentiment.facts import CN
from app.services.short_sentiment.popularity import attach_popularity
from app.services.short_sentiment.price_events import price_events
from app.services.short_sentiment.service import SentimentQueryService
from app.services.short_sentiment.engine import SentimentEngine
from app.services.short_sentiment.themes import ThemeCatalog
from tests.test_short_sentiment import CATALOG, DAY, WriteDatabase, engine, envelope, quote, raw_day


def test_popularity_is_causal_and_rank_breaks_only_board_ties():
    async def run():
        db = WriteDatabase()
        current = engine().process(envelope())
        db[HOT_COLLECTION].rows = [
            {'_id': 'future', 'trade_date': DAY, 'observed_at': datetime(2026, 9, 3, 10, 1, tzinfo=CN),
             'stocks': [{'code': '000001', 'rank': 1, 'heat': 999}]},
            {'_id': 'past', 'trade_date': DAY, 'observed_at': datetime(2026, 9, 3, 9, 59, tzinfo=CN),
             'stocks': [{'code': '000002', 'rank': 2, 'heat': 456.7, 'rank_change': 3}]},
        ]
        await attach_popularity(db, current)
        assert current['meta']['popularity']['snapshot_id'] == 'past'
        a, b = (current['stocks'][c] for c in ('000001', '000002'))
        assert a['popularity']['status'] == 'not_ranked'
        assert a['popularity']['rank'] is None
        assert b['popularity']['rank'] == 2
        theme_id = CATALOG.ids('000002')[0]
        members = SentimentQueryService.theme_members(current, theme_id)['items']
        assert [s['code'] for s in members] == ['000002', '000001']
        a['intraday_streak'] = b['intraday_streak'] + 1
        assert SentimentQueryService.theme_members(current, theme_id)['items'][0]['code'] == '000001'
        a['intraday_streak'] = b['intraday_streak']
        a['board_run']['limit_up_days'] += 1
        assert SentimentQueryService.theme_members(current, theme_id)['items'][0]['code'] == '000001'
        # Snapshot publication freezes the selected heat, including after restart.
        await ShortSentimentRepository(db).publish(current)
        restored = await ShortSentimentRepository(db).latest(DAY)
        assert restored['stocks']['000002']['popularity'] == b['popularity']
        app = create_app(); app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            dashboard = client.get('/api/v1/sentiment/dashboard').json()['data']
            top = next(t for t in dashboard['themes'] if t['theme_id'] == theme_id)['top_stocks']
            assert top[1]['popularity']['heat'] == 456.7
            assert client.get('/api/v1/sentiment/events?event_type=rapid_rise').status_code == 200
            assert client.get('/api/v1/sentiment/timeseries?interval=5m').status_code == 200
    asyncio.run(run())


@pytest.mark.parametrize('offset,status', [(-901, 'stale'), (1, 'missing')])
def test_unavailable_heat_never_ranks_as_zero(offset, status):
    async def run():
        db = WriteDatabase(); snapshot = engine().process(envelope())
        db[HOT_COLLECTION].rows = [{'_id': 'heat', 'trade_date': DAY,
            'observed_at': datetime(2026, 9, 3, 10, tzinfo=CN) + timedelta(seconds=offset),
            'stocks': [{'code': '000001', 'rank': 1, 'heat': 999}]}]
        await attach_popularity(db, snapshot)
        heat = snapshot['stocks']['000001']['popularity']
        assert heat['status'] == status and heat['rank'] is None and heat['heat'] is None
    asyncio.run(run())


def alert_stock(at, price, amount=1_000_000, **kw):
    return {'code': '000001', 'eligible': True, 'price': price, 'amount_yuan': amount,
            'source_time': at.isoformat(), 'provider': 'fixture', 'theme_ids': ['battery'],
            'is_limit_down': False, 'limit_down_price': 9, **kw}


def run_prices(values, *, amounts=None, times=None, runtime=None):
    start = datetime(2026, 9, 3, 10, tzinfo=CN)
    events = []; prior = {}; state = runtime or {}
    for i, price in enumerate(values):
        at = start + timedelta(seconds=times[i] if times else i*5)
        item = alert_stock(at, price, amounts[i] if amounts else 1_000_000)
        changes, state = price_events({'000001': item}, prior, json.loads(json.dumps(state)), at.isoformat())
        events.extend(changes); prior = {'000001': item}
    return events, state, prior


@pytest.mark.parametrize('end,kind', [(10.3, 'rapid_rise'), (9.7, 'rapid_fall')])
def test_price_window_threshold_cooldown_and_json_restart(end, kind):
    values = [10+(end-10)*i/36 for i in range(37)]
    events, state, prior = run_prices(values)
    selected = [e for e in events if e['type'] == kind]
    assert len(selected) == 1
    assert selected[0]['evidence']['change_ratio'] == pytest.approx(end/10-1)
    same, resumed = price_events(prior, prior, state, prior['000001']['source_time'])
    assert same == [] and resumed == state
    # Staying above the threshold cannot re-emit on each quote.
    all_events, _, _ = run_prices([10*1.0009**i for i in range(100)])
    assert len([e for e in all_events if e['type'] == 'rapid_rise']) == 1


@pytest.mark.parametrize('mode', ['gap', 'lunch', 'provider', 'invalid'])
def test_price_event_discontinuities_require_a_new_full_window(mode):
    _, state, prior = run_prices([10.]*37)
    last = datetime.fromisoformat(prior['000001']['source_time'])
    at = last + timedelta(seconds=20 if mode == 'gap' else 5)
    if mode == 'lunch': at = at.replace(hour=13, minute=0)
    item = alert_stock(at, 10.9, provider='other' if mode == 'provider' else 'fixture', eligible=mode != 'invalid')
    events, _ = price_events({'000001': item}, prior, state, at.isoformat())
    assert not events
    assert price_events({'000001': item}, prior, state, at.isoformat(), final=True) == ([], {})


@pytest.mark.parametrize('reverse,kind', [(False, 'quick_rebound'), (True, 'high_dive')])
def test_reversal_events_require_an_actual_opposite_leg(reverse, kind):
    values = [10-.3*i/30 for i in range(31)] + [9.7+.3*i/30 for i in range(1,31)]
    if reverse: values = [20-p for p in values]
    events, _, _ = run_prices(values)
    assert kind in {e['type'] for e in events}
    events, _, _ = run_prices([10+.4*i/60 for i in range(61)])
    assert not {e['type'] for e in events} & {'quick_rebound', 'high_dive'}


def test_amount_spike_and_limit_down_open():
    amounts = [1_000_000+i*100_000 if i <= 48 else 5_800_000+(i-48)*1_000_000 for i in range(61)]
    events, state, prior = run_prices([10.]*61, amounts=amounts)
    spike = next(e for e in events if e['type'] == 'volume_surge')
    assert spike['evidence']['amount_yuan'] == 12_000_000
    prior['000001']['is_limit_down'] = True
    at = datetime.fromisoformat(prior['000001']['source_time']) + timedelta(seconds=5)
    current = alert_stock(at, 9.1, amounts[-1]+1_000_000)
    events, _ = price_events({'000001': current}, prior, state, at.isoformat())
    assert 'open_limit_down' in {e['type'] for e in events}
    current['amount_yuan'] = 10
    assert 'volume_surge' not in {e['type'] for e in price_events({'000001': current}, prior, state, at.isoformat())[0]}


def test_accepted_environments_only_use_completed_minutes_and_survive_restart():
    codes = [f'600{i:03}' for i in range(100, 200)]
    catalog = ThemeCatalog(groups={'试验题材': {'成分股': [{'股票代码': c, '股票名称': c} for c in codes]}})
    raw = raw_day(codes=codes, up=False); raw['scope'] = 'all_a'
    worker = SentimentEngine(catalog, [raw], ['2026-09-02'])
    snapshot = None
    start = datetime(2026, 9, 3, 9, 30, 55, tzinfo=CN)
    for minute in range(15):
        for at in (start+timedelta(minutes=minute), start+timedelta(minutes=minute, seconds=5)):
            # A drop at the boundary must not leak into the preceding minute.
            price = 9.1 if at.second == 0 else 10.2
            stamp = at.strftime('%H:%M:%S')
            snapshot = worker.process(envelope([quote(c, price=11 if i<5 else price, at=stamp) for i,c in enumerate(codes)], at=stamp, expected=codes),
                                      json.loads(json.dumps(snapshot)) if snapshot else None)
    assert snapshot['meta']['rule_version'] == VERSION
    assert snapshot['market']['emotion']['state'] == '微热'
    assert snapshot['market']['emotion']['facts']['M'] == pytest.approx(2.)
    theme = next(t for t in snapshot['themes'] if t['theme_id'] != 'unclassified')
    assert theme['emotion']['state'] == '启动'
    assert theme['emotion']['evaluated_at'] == DAY+'T09:45+08:00'
    previous = deepcopy(snapshot['market']['emotion'])
    saved_checkpoint = json.dumps(snapshot, sort_keys=True)
    more = worker.process(envelope([quote(c, price=9.1, at='09:45:05') for c in codes], at='09:45:05', expected=codes), snapshot)
    assert json.dumps(snapshot, sort_keys=True) == saved_checkpoint
    assert more['market']['emotion'] == previous
    assert not more['environment_updated']
    final = worker.process(envelope([quote(c, price=9.1, at='15:00:00') for c in codes], at='15:00:00', expected=codes), more, final=True)
    assert final['market']['emotion']['state'] == '微热'
    assert final['market']['emotion']['is_carried_to_close']
    assert final['events'] == []
    assert final['environment_runtime']['market'] == more['environment_runtime']['market']
    # A missing window retains the confirmed state and reports unknown support.
    missing = worker.process(envelope([], at='10:00:00', expected=codes), more)
    assert missing['market']['emotion']['state'] == '微热'
    assert missing['market']['emotion']['support'] is None
    assert missing['market']['emotion']['quality'] is False
    async def persist():
        db = WriteDatabase(); repo = ShortSentimentRepository(db)
        await repo.publish(snapshot)
        await repo.publish(more)
        assert len([r for r in db[TIMESERIES].rows if r['interval']=='5m']) == 1
        restored = await repo.checkpoint(DAY)
        assert restored['environment_runtime'] == more['environment_runtime']
    asyncio.run(persist())


def test_high_board_feedback_starts_at_five_not_four():
    from app.services.short_sentiment.metrics import aggregate
    snapshot = engine().process(envelope())
    previous = {'000001': {'eligible': True, 'is_limit_up': True, 'is_touch_up': True, 'streak': 4},
                '000002': {'eligible': True, 'is_limit_up': True, 'is_touch_up': True, 'streak': 5}}
    stats = aggregate(snapshot['stocks'], previous, list(previous), list(previous))
    assert stats['feedback']['yesterday_high_board']['sample_count'] == 1
    assert stats['feedback']['yesterday_high_board']['codes'] == ['000002']


@pytest.mark.parametrize('last_good,expected', [('09:30:40', 10.2), ('09:30:20', None)])
def test_minute_close_keeps_each_fresh_security_across_a_missing_poll(last_good, expected):
    worker = engine()
    state = worker.process(envelope([quote('000001', price=10.2, at=last_good)], at=last_good, expected=['000001']))
    state = worker.process(envelope([], at='09:30:55', expected=['000001']), state)
    # Restart preserves the genuine pre-boundary sample; the current price
    # must never leak backwards into the minute being closed.
    state = worker.process(envelope([quote('000001', price=9.1, at='09:31:00')], at='09:31:00', expected=['000001']), json.loads(json.dumps(state)))
    minute = state['environment_runtime']['minutes'][-1]
    assert minute['prices'].get('000001') == expected


def test_minute_quote_cache_never_crosses_lunch():
    worker = engine()
    state = worker.process(envelope([quote('000001', price=10.2, at='11:29:55')], at='11:29:55', expected=['000001']))
    state = worker.process(envelope([], at='13:01:00', expected=['000001']), state)
    assert state['environment_runtime']['minutes'][-1]['prices'] == {}


def test_market_coverage_is_separate_from_local_board_and_direction_gaps():
    import numpy as np
    from app.services.short_sentiment.market_environment_features import market_points
    codes=[f'600{i:03}' for i in range(190)]+[f'920{i:03}' for i in range(10)]
    sources={c:{'code':c,'ref':10.,'upper':11.,'lower':9.,'close':10.2,'board':'bse' if c.startswith('92') else 'sh_mainboard','excluded':None,'error':None} for c in codes}
    prices=np.full((1,200),10.2);prices[0,-1]=np.nan
    z={'codes':codes,'times':['09:31'],'prices':prices}
    directions=[{'id':'small','name':'small','codes':codes[-10:]}]
    point=market_points(DAY,sources,sources,z,directions)[0]
    assert point['coverage']==.995 and point['quality']
    assert point['board_coverage']['bse']==.9
    assert not point['structure_quality'] and point['active'] is None
    prices[0,-5:]=np.nan
    assert not market_points(DAY,sources,sources,z,directions)[0]['quality']
