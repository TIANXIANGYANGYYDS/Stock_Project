from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.quant.cli import replay_stock
from app.quant.core.models import BacktestConfig, Bar
from app.quant.research.adx_exit import EXIT_GRID, ExitController, ExitVariant, adx_state, liquidation_quote
from app.quant.research.factors import FactorSnapshot


def snapshot(a, b, day='2026-01-05'):
    return FactorSnapshot(day, '2026-01-02', {'adx_14': a, 'adx_14_3_days_ago': b})


@pytest.mark.parametrize('a,b,expected', [(20,19,'strong'),(25,25,'neutral'),(19,19,'weak'),
    (21,22,'weak'),(None,22,'missing'),(21,None,'missing'),(float('nan'),20,'missing')])
def test_three_states_and_missing(a,b,expected):
    assert adx_state(snapshot(a,b),14)[0] == expected


def decide(controller, *, original=False, profit=.1, dif=1., h=1., at='2026-01-05T10:00:00+08:00'):
    return controller.decide(at=at, quote={'estimated_net_return':profit}, dif=dif,
        histogram=h, original_sell=original, entry_at='2026-01-04T10:00:00+08:00')


def test_grid_and_priority_neutral_missing():
    assert len(EXIT_GRID) == 9
    c=ExitController(ExitVariant(14,'E3'),{'2026-01-05':snapshot(22,23)})
    c.on_fill('buy')
    assert decide(c,original=True)['reason']=='original_macd'
    c.snapshots['2026-01-05']=snapshot(30,20)
    assert decide(c,original=True)['action']=='pending'
    c.on_fill('buy');c.snapshots['2026-01-05']=snapshot(25,25)
    assert decide(c)['action']=='hold'
    assert decide(c,original=True)['reason']=='original_macd'
    c.on_fill('buy');c.snapshots.clear()
    assert decide(c)['action']=='hold'


@pytest.mark.parametrize('failure', ['adx','neutral','dif','histogram','profit','missing','missing_macd'])
def test_deferred_exit_survives_day_and_expires_without_another_macd_sell(failure):
    c=ExitController(ExitVariant(14,'E2'),{'2026-01-05':snapshot(30,20)})
    c.on_fill('buy')
    assert decide(c,original=True)['action']=='defer'
    assert decide(c)['action']=='hold'
    c.snapshots['2026-01-06']=snapshot(30,20,'2026-01-06')
    kwargs={'at':'2026-01-06T09:33:00+08:00'}
    if failure=='adx':c.snapshots['2026-01-06']=snapshot(19,20)
    if failure=='neutral':c.snapshots['2026-01-06']=snapshot(25,25)
    if failure=='missing':c.snapshots.clear()
    if failure=='dif':kwargs['dif']=0
    if failure=='histogram':kwargs['h']=0
    if failure=='profit':kwargs['profit']=0
    if failure=='missing_macd':kwargs['h']=None
    row=decide(c,**kwargs)
    assert row['reason']=='deferred_invalid'
    assert row['deferred_from']=='2026-01-05T10:00:00+08:00'
    assert c.state=='EXIT_PENDING'


def test_quote_uses_paid_entry_fees_and_cost_changes_decision():
    config=BacktestConfig(code='000001')
    p={'shares':1000,'entry_notional':10000.,'buy_commission':1.}
    b=Bar('2026-01-05T10:00:00+08:00',10.015,10.02,10.,10.015)
    normal=liquidation_quote(p,b,config)
    double=liquidation_quote(p,b,replace(config,commission_rate=.0002,stamp_duty_rate=.001,slippage_rate=.001))
    assert normal['estimated_net_return']>0>double['estimated_net_return']
    c=ExitController(ExitVariant(14,'E1'),{'2026-01-05':snapshot(19,20)});c.on_fill('buy')
    assert decide(c,profit=normal['estimated_net_return'])['action']=='submit'
    c.on_fill('buy')
    assert decide(c,profit=double['estimated_net_return'])['action']=='hold'


def fixture(monkeypatch, *, weak_day=6, incomplete_day=None, t1=False, down_day=None):
    daily=[Bar(f'2026-01-{day:02d}',11.,11.,11.,11.) for day in range(1,8)]
    minutes={}
    for day in range(4,8):
        start=datetime(2026,1,day,9,33)
        minutes[f'2026-01-{day:02d}']=[Bar((start+timedelta(minutes=3*i)).isoformat()+'+08:00',11.,11.1,10.9,11.) for i in range(80)]
    if incomplete_day:minutes[f'2026-01-{incomplete_day:02d}']=[]
    config=BacktestConfig(code='000001',warmup_bars=3)
    entry_at=minutes['2026-01-04'][0].trade_date
    event={'signal_id':'fixed','code':'000001','name':'test','action':'buy','signal_at':'2026-01-03T15:00:00+08:00',
           'signal_price':10.,'execution_at':entry_at,'execution_reference_price':10.,'execution_bar_low':10.,
           'execution_bar_high':11.,'daily_price_limit':12.,'slippage_rate':config.slippage_rate,
           'execution_price':10.,'shares':1000,'notional':10000.,'commission':1.,'stamp_duty':0.,
           'cash_before':100000.,'cash_after':89999.,'reason':'fixed actual entry'}
    signal={'signal_id':'fixed','code':'000001','name':'test','action':'buy','signal_at':event['signal_at'],
            'signal_price':10.,'provisional_histogram':-1.,'final_daily_confirmed':True,'final_status':'filled',
            'execution_at':entry_at,'execution_price':10.}
    monkeypatch.setattr(replay_stock,'determine_observation_action',lambda *args:'sell')
    monkeypatch.setattr(replay_stock,'confirm_provisional_histogram',lambda **kw:(True,.1))
    monkeypatch.setattr(replay_stock,'provisional_daily_indicator_from_state',lambda *a,**kw:SimpleNamespace(dif=2.,dea=1.,histogram=2.))
    if down_day:
        original=replay_stock.at_daily_price_limit
        monkeypatch.setattr(replay_stock,'at_daily_price_limit',lambda **kw:kw['action']=='sell' and kw['trade_date']==f'2026-01-{down_day:02d}')
    snaps={f'2026-01-{day:02d}':snapshot(19 if (day==weak_day or t1 and day==4) else 30,20) for day in range(4,8)}
    return dict(code='000001',name='test',daily_bars=daily,minute_bars_by_date=minutes,start_date='2026-01-04',
                end_date='2026-01-07',config=config,fixed_entry={'event':event,'signal':signal}),snaps


def test_delay_keeps_state_across_days_and_missing_session(monkeypatch):
    kwargs,snaps=fixture(monkeypatch,weak_day=7,incomplete_day=6)
    c=ExitController(ExitVariant(14,'E2'),snaps)
    r=replay_stock.replay(**kwargs,exit_controller=c)
    assert r['signal_rows'][1]['signal_at'].startswith('2026-01-05T09:39')
    assert r['signal_rows'][1]['final_status']=='deferred_exit'
    assert r['event_rows'][-1]['signal_at'].startswith('2026-01-07T09:33')
    assert r['event_rows'][-1]['execution_at'].startswith('2026-01-07T09:36')
    assert r['event_rows'][-1]['exit_reason']=='deferred_invalid'
    assert r['event_rows'][0]==kwargs['fixed_entry']['event']
    assert r['summary']['filled_buy_count']==1


def test_t1_pending_intent_cannot_be_revoked_by_strong_adx(monkeypatch):
    kwargs,snaps=fixture(monkeypatch,weak_day=6,t1=True)
    c=ExitController(ExitVariant(14,'E3'),snaps)
    r=replay_stock.replay(**kwargs,exit_controller=c)
    sell=r['event_rows'][-1]
    assert sell['signal_at'].startswith('2026-01-04T09:33')
    assert sell['execution_at'].startswith('2026-01-05T09:33')
    assert sell['exit_reason']=='early_protection'
    assert any(a['status']=='deferred_t_plus_one' for a in r['attempt_rows'])
    assert not any(row['action']=='defer' for row in c.rows)


def test_limit_down_pending_kept_and_actual_fill_can_lose(monkeypatch):
    kwargs,snaps=fixture(monkeypatch,weak_day=6,t1=True,down_day=5)
    # A profitable quote on day 4 cannot guarantee the day 6 actual fill.
    kwargs['minute_bars_by_date']['2026-01-06']=[replace(b,open=9.,high=9.1,low=8.9,close=9.) for b in kwargs['minute_bars_by_date']['2026-01-06']]
    r=replay_stock.replay(**kwargs,exit_controller=ExitController(ExitVariant(14,'E3'),snaps))
    assert r['event_rows'][-1]['execution_at'].startswith('2026-01-06')
    assert r['closed_trade_rows'][0]['net_return']<0
    assert any(a['status']=='deferred_limit_down' for a in r['attempt_rows'])


def test_new_original_signal_during_valid_defer_does_not_restore_pending_order(monkeypatch):
    kwargs,snaps=fixture(monkeypatch,weak_day=7)
    r=replay_stock.replay(**kwargs,exit_controller=ExitController(ExitVariant(14,'E2'),snaps))
    assert r['event_rows'][-1]['execution_at'].startswith('2026-01-07T09:36')
    assert all(row['final_status']=='deferred_exit' for row in r['signal_rows'] if row['signal_at'][:10] in ('2026-01-05','2026-01-06'))
