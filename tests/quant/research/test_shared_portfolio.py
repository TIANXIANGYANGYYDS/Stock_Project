from copy import deepcopy
from dataclasses import asdict

import pytest

from app.quant.cli.replay_shared_portfolio import select_watchlist, simulate
from app.quant.runtime.daily_macd import DailyMacdState, provisional_daily_indicator_from_state
from app.quant.strategies.provisional_daily_macd_3m.strategy import official_backtest_config
from app.quant.research.shared_portfolio import AccountRules, SharedAccount, fees, first_buy, match, provisional


def bar(day='2026-07-01', start='09:39', end='09:42', price=10., volume=1_000_000):
    return dict(start_at=f'{day}T{start}:00+08:00', end_at=f'{day}T{end}:00+08:00',
                open=price, high=price+.1, low=price-.1, close=price, previous_close=price, volume=volume)


def signal(code='000001', day='2026-07-01', time='09:39'):
    return dict(code=code,name=code,at=f'{day}T{time}:00+08:00',factor_date='2026-06-30',adx_delta=4.)


def test_provisional_matches_production_scaled_state():
    state=DailyMacdState(9.,10.,-.9)
    item=dict(previous_state=asdict(state),previous_close=10.,reference_histogram=-.2)
    b=bar();b['previous_close']=9.5
    dif,hist,ref=provisional(item,b)
    scaled=DailyMacdState(*(x*.95 for x in (state.fast_ema,state.slow_ema,state.dea)))
    official=provisional_daily_indicator_from_state(scaled,trade_date='2026-07-01',day_open=10.,
        high_so_far=10.1,low_so_far=9.9,current_close=10.,config=official_backtest_config(code='000001'))
    assert dif==pytest.approx(official.dif)
    assert hist==pytest.approx(official.histogram)
    assert ref==pytest.approx(-.19)


def test_reserves_shared_cash_and_five_slots_and_uses_lagged_scores():
    a=SharedAccount('low_vol')
    ss=[signal(f'{i:06}') for i in range(1,8)]
    fs={(s['code'],s['factor_date']):{'volatility_20_rank':int(s['code'])/10} for s in ss}
    a.select(list(reversed(ss)),fs,ss[0]['at'])
    assert list(a.pending)==[f'{i:06}' for i in range(1,6)]
    assert sum(o['budget'] for o in a.pending.values())==200000
    assert [d['decision'] for d in a.decisions].count('five_slots_occupied')==2
    # Future rows cannot change a past selection.
    future=deepcopy(fs);future.update({(s['code'],'2026-07-01'):{'volatility_20_rank':0.} for s in ss})
    b=SharedAccount('low_vol');b.select(ss,future,ss[0]['at'])
    assert a.orders==b.orders


def test_fills_conserve_cash_and_t_plus_one_then_original_exit():
    a=SharedAccount('fifo');s=signal();a.select([s],{},s['at']);a.execute(s['code'],bar())
    p=a.positions[s['code']]
    assert p['shares']==3900 and a.cash==pytest.approx(200000-39039-5.39)
    a.submit(s['code'],s['code'],'sell',bar()['end_at'],reason='original_macd')
    a.execute(s['code'],bar(start='09:42',end='09:45'))
    assert a.attempts[-1]['status']=='t_plus_one'
    a.execute(s['code'],bar(day='2026-07-02',price=11.))
    assert not a.positions and len(a.trades)==1
    assert a.assets()-200000==pytest.approx(a.trades[0]['pnl'])
    a.check()


def test_no_optimistic_price_clipping_and_limit_price_rejection():
    order=dict(action='buy',code='000001',name='样本',budget=40000)
    b=bar();b['high']=10.
    assert match(order,b,rules=AccountRules())['status']=='slipped_price_outside_bar'
    b=bar(price=11.);b['previous_close']=10.
    assert match(order,b,rules=AccountRules())['status']=='price_limit'


def test_partial_fill_volume_cap_and_commission_minimum_per_order():
    a=SharedAccount('fifo');s=signal();a.select([s],{},s['at'])
    a.execute(s['code'],bar(volume=10000))
    assert a.positions[s['code']]['shares']==500
    assert a.fills[0]['participation']==.05
    assert a.orders[0]['status']=='partial_filled_cancelled'
    a.submit(s['code'],s['code'],'sell',bar()['end_at'],reason='original_macd')
    a.execute(s['code'],bar(day='2026-07-02',volume=4000))
    a.execute(s['code'],bar(day='2026-07-02',start='09:42',end='09:45',volume=4000))
    a.execute(s['code'],bar(day='2026-07-02',start='09:45',end='09:48',volume=4000))
    sells=[f for f in a.fills if f['action']=='sell']
    assert [f['shares'] for f in sells]==[200,200,100]
    assert sum(f['fees'] for f in sells)==pytest.approx(fees(sum(f['notional'] for f in sells),'sell',AccountRules()))
    a.check()


def test_star_minimum_order_and_unaffordable_stock():
    order=dict(action='buy',code='688001',name='科创',budget=40000)
    expensive=bar(price=300.);expensive['high']=301.
    assert match(order,expensive,rules=AccountRules())['status']=='unaffordable_lot'
    r=match(order,bar(price=150.),rules=AccountRules())
    assert r['shares']>=200 and r['shares']%100!=0


def test_reinvests_decision_time_nav_and_keeps_existing_positions():
    a=SharedAccount('fifo');s=signal();a.select([s],{},s['at']);a.execute(s['code'],bar())
    a.positions[s['code']]['mark']=12.
    nav=a.assets();s2=signal('000002');a.select([s2],{},s2['at'])
    assert a.pending['000002']['budget']==pytest.approx(round(nav*.2,2))
    assert a.positions['000001']['shares']==3900


def test_new_signal_does_not_fill_in_its_confirmation_bar():
    a=SharedAccount('fifo');s=signal();a.select([s],{},s['at'])
    with pytest.raises(AssertionError):
        a.execute(s['code'],bar(start='09:36',end='09:39'))


def test_missing_next_bar_cancels_buy_but_preserves_t_plus_one_sell_intent():
    class Market:
        def bars(self,code,day):
            return {}
        def inputs(self,code):
            return {}
    s=signal()
    account,_=simulate(market=Market(),dates=['2026-07-01'],signals=[s],factors={},policy='fifo',rules=AccountRules())
    assert account.orders[0]['status']=='missing_next_bar_cancelled'
    assert account.cash==200000 and not account.fills


def test_preopen_watchlist_includes_nontriggering_watches_without_peeking():
    a=SharedAccount('adx')
    watches=[dict(signal(f'{i:06}'),adx_delta=10-i) for i in range(1,8)]
    allowed=select_watchlist(a,watches,{})
    assert allowed=={f'{i:06}' for i in range(1,6)}
    # Only the lowest-ranked watch triggers today. It still cannot enter:
    # knowing that the other five will not trigger must not change the pool.
    a.select([watches[-1]],{},watches[-1]['at'],allowed_codes=allowed)
    assert not a.pending and a.decisions[-1]['decision']=='outside_preopen_top5'


def test_preopen_pool_excludes_existing_holdings_without_forced_rotation():
    a=SharedAccount('adx');s=signal();a.select([s],{},s['at']);a.execute(s['code'],bar())
    watches=[dict(signal(f'{i:06}'),adx_delta=10-i) for i in range(1,8)]
    allowed=select_watchlist(a,watches,{})
    assert '000001' not in allowed and '000006' in allowed
    assert a.positions['000001']['shares']==3900 and not a.pending


def test_sell_limit_down_intent_persists_and_eventually_fills():
    a=SharedAccount('fifo');s=signal();a.select([s],{},s['at']);a.execute(s['code'],bar())
    a.submit(s['code'],s['code'],'sell',bar()['end_at'],reason='original_macd')
    b=bar(day='2026-07-02',price=9.);b['previous_close']=10.
    a.execute(s['code'],b)
    assert a.attempts[-1]['status']=='price_limit' and s['code'] in a.pending
    a.execute(s['code'],bar(day='2026-07-03',price=9.2))
    assert not a.pending and not a.positions


def test_buy_requires_three_completed_bars_and_ignores_future_prices():
    item=dict(code='000001',name='测试',action='buy',adx=30.,adx_3_days_ago=25.,
        factor_completed_date='2026-06-30',previous_close=10.,reference_histogram=-.2,
        previous_state=asdict(DailyMacdState(9.,10.,-.9)))
    bs=[bar(start=start,end=end) for start,end in [('09:30','09:33'),('09:33','09:36'),('09:36','09:39')]]
    assert first_buy(item,bs[:2]) is None
    s=first_buy(item,bs)
    assert s['at'].endswith('09:39:00+08:00')
    assert first_buy(item,bs+[bar(start='09:39',end='09:42',price=1000.)])==s


def test_zero_volume_quotes_cannot_refresh_held_valuation_or_trigger_exit():
    b1=bar();b2=bar(day='2026-07-02',price=100.,volume=0)
    class Market:
        def bars(self,code,day):
            b=b1 if day=='2026-07-01' else b2
            return {b['end_at']:b}
        def inputs(self,code):
            return {}
    a,_=simulate(market=Market(),dates=['2026-07-01','2026-07-02'],signals=[signal()],factors={},policy='fifo',rules=AccountRules())
    assert a.curve[-1]['assets']==a.curve[-2]['assets']
    assert a.curve[-1]['stale_market_value']==39000.
    assert len(a.fills)==1
