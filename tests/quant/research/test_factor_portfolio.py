import numpy as np
import pytest

from app.quant.research.factor_portfolio import PortfolioCosts, factor_groups, simulate_portfolio, target_schedule


def test_equal_values_are_not_split_by_stock_code():
    values = np.array([0., 0., 0., 1., 1., np.nan])
    assert factor_groups(values, np.ones(6, dtype=bool)).tolist() == [1, 1, 1, 5, 5, 0]
    assert not factor_groups(np.ones(20), np.ones(20, dtype=bool)).any()


def test_missing_factor_does_not_become_a_zero_or_liquidation():
    values = np.array([[1., 2., 3., 4., 5.], [np.nan] * 5])
    targets, audit = target_schedule(values, np.ones_like(values, dtype=bool), group=5, rebalance_sessions=1, minimum_stocks=1)
    assert list(targets) == [0]
    assert targets[0].tolist() == [0., 0., 0., 0., 1.]
    assert audit[1]['usable'] is False


def test_fixed_cash_next_open_costs_and_unrealized_marks():
    opens = np.array([[10.], [12.]])
    closes = np.array([[11.], [13.]])
    r = simulate_portfolio(dates=['2026-07-01','2026-07-02'],open_prices=opens,close_prices=closes,
                           initial_marks=np.array([9.]),can_buy=np.ones_like(opens,dtype=bool),
                           can_sell=np.ones_like(opens,dtype=bool),targets={0:np.array([1.])})
    assert r.nav.iloc[-1] == pytest.approx(1.3 / 1.0006)
    assert r.daily_return.iloc[0] == pytest.approx(1.1 / 1.0006 - 1)
    assert np.prod(1+r.daily_return) == pytest.approx(r.nav.iloc[-1])
    assert (r.cash_weight >= 0).all()


def test_blocked_sell_and_missing_quote_preserve_position():
    opens=np.array([[10.],[np.nan],[9.]])
    closes=np.array([[10.],[np.nan],[9.]])
    valid=np.isfinite(opens)
    r=simulate_portfolio(dates=['2026-07-01','2026-07-02','2026-07-03'],open_prices=opens,close_prices=closes,
                         initial_marks=np.array([10.]),can_buy=valid,can_sell=valid,
                         targets={0:np.array([1.]),1:np.array([0.]),2:np.array([0.])},
                         costs=PortfolioCosts(0,0,0))
    assert r.holding_count.tolist()==[1,1,0]
    assert r.stale_mark_weight.iloc[1]==1
    assert r.nav.iloc[-1]==pytest.approx(.9)
    assert r.blocked_sells.iloc[1]==1


def test_constant_price_roundtrip_charges_both_sides_once():
    a=np.array([[10.],[10.]])
    r=simulate_portfolio(dates=['a','b'],open_prices=a,close_prices=a,initial_marks=np.array([10.]),
                         can_buy=np.ones_like(a,dtype=bool),can_sell=np.ones_like(a,dtype=bool),
                         targets={0:np.array([1.]),1:np.array([0.])})
    assert r.nav.iloc[-1]==pytest.approx((1-.0011)/(1+.0006))
