from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.quant.runtime.live as live
from app.quant.core.models import Bar
from app.quant.runtime.daily_flow import (
    IndependentAccount, PreselectionItem, SellCandidateItem, apply_trade_signal,
    create_daily_flow, daily_flow_document,
)
from app.quant.runtime.daily_macd import DailyMacdState
from app.quant.research.factors import FactorBar, calculate_factor_snapshots
from app.quant.strategies.provisional_daily_macd_3m.adx import daily_adx_snapshot


CODE = '000001'
D1, D2, D3 = '2026-09-03', '2026-09-04', '2026-09-07'


def bars(day=D2, prices=(11., 11., 11., 11.), previous=11.):
    start = datetime.fromisoformat(day + 'T09:30:00+08:00')
    return tuple(live.LiveThreeMinuteBar(
        start_at=(start+timedelta(minutes=3*i)).isoformat(),
        end_at=(start+timedelta(minutes=3*(i+1))).isoformat(),
        open=p, high=p+.03, low=p-.03, close=p, previous_close=previous)
        for i, p in enumerate(prices))


def spec(day=D2, action='sell', adx=30., previous_adx=25.):
    return live.LiveObservationSpec(CODE, '测试', action, '2026-09-01', D1,
        11., 2. if action != 'buy' else -2., DailyMacdState(12., 11., .2),
        adx=adx, adx_3_days_ago=previous_adx, factor_completed_date=D1,
        factor_comparison_date='2026-08-31')


def bought():
    flow = create_daily_flow(trade_date=D1, selection_date='2026-09-02',
        generated_at=D1+'T09:20:00+08:00',
        candidates=[PreselectionItem(CODE, '测试', '买入', 10.)],
        accounts=[IndependentAccount(CODE, '测试'), IndependentAccount('000002', '独立账户')])
    return apply_trade_signal(flow, action='buy', code=CODE, signal_at=D1+'T09:39:00+08:00',
        signal_price=10., previous_close=10., execution_at=D1+'T09:39:00+08:00',
        execution_reference_price=10., reason='entry')


def carry(flow, day, *, candidate=True):
    return create_daily_flow(trade_date=day, selection_date=flow.trade_date,
        generated_at=day+'T09:20:00+08:00', candidates=[], holdings=flow.holdings,
        accounts=flow.accounts,
        sell_candidates=[SellCandidateItem(CODE, '测试', '原卖点观察', 11.)] if candidate else [])


def fixed_macd(monkeypatch, *, positive=True):
    monkeypatch.setattr(live, 'provisional_daily_indicator_from_state', lambda *a, **k:
        SimpleNamespace(dif=1. if positive else 0., dea=.5, histogram=1.))
    monkeypatch.setattr(live, 'confirm_provisional_histogram', lambda **k: (True, .1))


def run(flow, observation, minute_bars, **kwargs):
    return live.replay_live_day(opening_flow=flow, observation_specs=[observation],
        opening_pending_signals=kwargs.pop('pending', []), bars_by_code={CODE: minute_bars},
        expected_bar_count=len(minute_bars), close_market=True, **kwargs)


@pytest.mark.parametrize('adx,old,accepted', [(20,19,True),(25,25,False),(19,18,False),(21,22,False),(None,20,False)])
def test_live_adx_gates_only_confirmed_macd_buys(monkeypatch, adx, old, accepted):
    fixed_macd(monkeypatch)
    flow = create_daily_flow(trade_date=D2, selection_date=D1, generated_at=D2+'T09:20:00+08:00',
        candidates=[PreselectionItem(CODE, '测试', 'MACD', 11.)], accounts=[IndependentAccount(CODE, '测试')])
    result = run(flow, spec(action='buy', adx=adx, previous_adx=old), bars())
    assert len(result['signals']) == 1
    assert result['signals'][0]['signal_at'].endswith('09:39:00+08:00')
    assert bool(result['flow'].holdings) == accepted
    assert result['signals'][0]['status'] == ('filled' if accepted else 'rejected_adx')


def test_deferred_exit_survives_roundtrip_and_expires_without_new_sell(monkeypatch):
    fixed_macd(monkeypatch)
    opening = carry(bought(), D2)
    first = run(opening, spec(), bars())
    assert first['signals'] == ()
    assert first['exit_states'][CODE]['state'] == 'DEFERRED_EXIT'
    assert first['exit_states'][CODE]['deferred_from'] == D2+'T09:39:00+08:00'
    next_open = carry(first['flow'], D3, candidate=False)
    restored = live.opening_flow_from_document(live.opening_flow_document(next_open))
    observation = replace(spec(day=D3, action='hold', adx=19.), observation_date=D2, factor_completed_date=D2)
    restored_spec = live.observation_spec_from_document(live.observation_spec_document(observation))
    kwargs = {'opening_exit_states': first['exit_states']}
    result = run(restored, restored_spec, bars(D3), **kwargs)
    repeated = run(restored, restored_spec, bars(D3), **kwargs)
    assert result == repeated
    assert result['signals'][0]['exit_reason'] == 'deferred_invalid'
    assert result['signals'][0]['confirmation_count'] == 0
    assert result['flow'].executions[0].execution_at == D3+'T09:33:00+08:00'
    assert result['flow'].holdings == ()
    summary = daily_flow_document(result['flow'])['summary']
    assert summary['initial_capital'] == 100000
    assert summary['account_count'] == 1
    assert summary['inactive_account_count'] == 1
    assert summary['realized_pnl'] > 0
    assert summary['unrealized_pnl'] == 0
    assert result['flow'].accounts[1].cash == 100000
    assert result['flow'].accounts[0].cash == pytest.approx(100000 + summary['realized_pnl'])


@pytest.mark.parametrize('kind', ['nonpositive_dif', 'missing_adx', 'nonpositive_profit'])
def test_delay_ends_when_any_permission_fails(monkeypatch, kind):
    fixed_macd(monkeypatch, positive=kind != 'nonpositive_dif')
    observation = spec(action='hold', adx=None if kind=='missing_adx' else 30.)
    minute_bars = bars(prices=(9.99,)*4, previous=10.) if kind=='nonpositive_profit' else bars()
    result = run(carry(bought(), D2, candidate=False), observation, minute_bars,
        opening_exit_states={CODE:{'state':'DEFERRED_EXIT','deferred_from':D1+'T14:00:00+08:00'}})
    assert result['signals'][0]['exit_reason'] == 'deferred_invalid'
    assert len(result['flow'].executions) == 1


def test_submitted_exit_stays_pending_when_adx_recovers_and_limit_down_blocks(monkeypatch):
    fixed_macd(monkeypatch)
    import app.quant.runtime.daily_flow as daily_flow
    original = daily_flow.at_daily_price_limit
    monkeypatch.setattr(daily_flow, 'at_daily_price_limit', lambda **kw: True if kw['action']=='sell' else original(**kw))
    first = run(carry(bought(), D2, candidate=False), spec(action='hold', adx=19.), bars(),
        opening_exit_states={CODE:{'state':'DEFERRED_EXIT','deferred_from':D1+'T14:00:00+08:00'}})
    assert first['pending_signals'][0]['status'] == 'deferred_limit_down'
    assert first['exit_states'][CODE]['state'] == 'EXIT_PENDING'
    monkeypatch.setattr(daily_flow, 'at_daily_price_limit', original)
    second = run(carry(first['flow'], D3), spec(day=D3, action='hold', adx=40.), bars(D3),
        pending=first['pending_signals'], opening_exit_states=first['exit_states'])
    assert len(second['flow'].executions) == 1
    assert second['signals'][0]['signal_at'] == first['signals'][0]['signal_at']


def test_t1_preserves_exit_instruction_and_fills_next_day(monkeypatch):
    fixed_macd(monkeypatch)
    opening = carry(bought(), D1)
    pending = {'code':CODE,'name':'测试','action':'sell','signal_id':'t1-sell',
               'signal_at':D1+'T09:39:00+08:00','signal_price':11.,'status':'pending_execution'}
    first = run(opening, spec(day=D1, action='hold'), bars(D1, prices=(11.,)*6), pending=[pending])
    assert not first['flow'].executions
    assert first['pending_signals'][0]['status'] == 'deferred_t1'
    second = run(carry(first['flow'], D2), spec(action='hold'), bars(), pending=first['pending_signals'],
                 opening_exit_states=first['exit_states'])
    assert second['flow'].executions[0].execution_at == D2+'T09:30:00+08:00'


def test_e2_does_not_exit_weak_trend_without_original_sell(monkeypatch):
    fixed_macd(monkeypatch)
    result = run(carry(bought(), D2, candidate=False), spec(action='hold', adx=10.), bars())
    assert not result['signals']
    assert result['flow'].holdings


def test_cash_compounds_per_stock_after_sale_and_survives_serialization(monkeypatch):
    fixed_macd(monkeypatch)
    sold = run(carry(bought(), D2), spec(adx=19.), bars())['flow']
    opening = create_daily_flow(trade_date=D3, selection_date=D2, generated_at=D3+'T09:20:00+08:00',
        candidates=[PreselectionItem(CODE, '测试', '买入', 10.)], accounts=sold.accounts)
    opening = live.opening_flow_from_document(live.opening_flow_document(opening))
    result = run(opening, spec(day=D3, action='buy'), bars(D3, prices=(10.,)*4, previous=10.))
    assert result['flow'].holdings[0].shares > bought().holdings[0].shares
    assert result['flow'].accounts[1].cash == 100000
    assert result['flow'].accounts[0].realized_pnl == sold.accounts[0].realized_pnl
    summary = daily_flow_document(result['flow'])['summary']
    assert summary['total_assets'] > 100000
    assert summary['initial_capital'] == 100000
    assert summary['capital_inflow'] == 0
    assert result['flow'].accounts[0].first_buy_at == bought().accounts[0].first_buy_at


def test_live_does_not_read_future_bars(monkeypatch):
    fixed_macd(monkeypatch)
    result = live.replay_live_day(opening_flow=carry(bought(), D2), observation_specs=[spec(adx=19.)],
        opening_pending_signals=[], bars_by_code={CODE:bars()}, expected_bar_count=2, close_market=False)
    assert not result['signals']
    assert result['last_complete_bar_at'] == D2+'T09:36:00+08:00'


def test_adx_matches_research_and_missing_market_endpoint_does_not_shift():
    dates = [(date(2025,11,5)+timedelta(days=i)).isoformat() for i in range(180)]
    history = [Bar(day, 10+i*.05, 10.4+i*.06, 9.8+i*.04, 10.1+i*.05) for i,day in enumerate(dates[:-1])]
    factor_bars = [FactorBar(b.trade_date,b.high,b.low,b.close,0.) for b in history]
    expected = calculate_factor_snapshots(factor_bars, market_dates=dates, signal_dates=[dates[-1]])[dates[-1]]
    actual = daily_adx_snapshot(bars=history, trade_date=dates[-1], completed_date=dates[-2], comparison_date=dates[-5])
    assert actual.value('adx_14') == expected.value('adx_14')
    assert actual.value('adx_14_3_days_ago') == expected.value('adx_14_3_days_ago')
    missing = daily_adx_snapshot(bars=[b for b in history if b.trade_date != dates[-5]],
        trade_date=dates[-1], completed_date=dates[-2], comparison_date=dates[-5])
    assert missing.value('adx_14_3_days_ago') is None
