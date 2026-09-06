from __future__ import annotations

import pytest

from app.quant.runtime.daily_flow import IndependentAccount, independent_account_summary

D1 = '2026-08-20'
D2 = '2026-08-21'
FIRST = D1 + 'T10:00:00+08:00'


def summary(accounts, holdings=(), *, day=D2, opening=0):
    return independent_account_summary(accounts=accounts, holding_items=holdings,
                                       opening_total_assets=opening, trade_date=day)


def test_unbought_accounts_have_no_capital_cash_or_return():
    result = summary([IndependentAccount('000001', '未买入')])
    for key in ['account_count', 'initial_capital', 'cash_balance', 'market_value', 'total_assets',
                'total_pnl', 'total_return', 'account_day_pnl', 'account_day_return', 'capital_inflow']:
        assert result[key] == 0
    assert result['universe_account_count'] == result['inactive_account_count'] == 1


def test_first_buy_adds_principal_without_turning_it_into_profit():
    account = IndependentAccount('000001', '买入', cash=90000, first_buy_at=FIRST)
    holding = {'code': '000001', 'market_value': 10500, 'unrealized_pnl': 500}
    result = summary([account, IndependentAccount('000002', '观察')], [holding], day=D1)
    assert result['account_count'] == result['new_account_count'] == 1
    assert result['capital_inflow'] == result['initial_capital'] == 100000
    assert result['total_assets'] == 100500
    assert result['account_day_pnl'] == result['total_pnl'] == 500
    assert result['account_day_return_base'] == 100000
    assert result['account_day_return'] == result['total_return'] == .005


def test_new_account_contribution_preserves_existing_accounts_pnl_and_day_base():
    a = IndependentAccount('000001', '已清仓', cash=110000, realized_pnl=10000, first_buy_at=FIRST)
    b = IndependentAccount('000002', '新买入', cash=90000, first_buy_at=D2+'T10:00:00+08:00')
    holding = {'code': '000002', 'market_value': 9500, 'unrealized_pnl': -500}
    result = summary([a, b, IndependentAccount('000003', '未买入')], [holding], opening=110000)
    assert result['account_count'] == 2
    assert result['new_account_count'] == 1
    assert result['initial_capital'] == 200000
    assert result['capital_inflow'] == 100000
    assert result['total_assets'] == 209500
    assert result['total_pnl'] == 9500
    assert result['total_return'] == .0475
    assert result['account_day_pnl'] == -500
    assert result['account_day_return_base'] == 210000
    assert result['account_day_return'] == -500 / 210000


def test_closed_breakeven_account_stays_active_even_with_identical_cash():
    result = summary([IndependentAccount('000001', '已清仓', first_buy_at=FIRST),
                      IndependentAccount('000002', '从未买入')], opening=100000)
    assert result['account_count'] == 1
    assert result['total_assets'] == result['initial_capital'] == 100000
    assert result['capital_inflow'] == result['new_account_count'] == 0
    assert result['account_day_pnl'] == 0


def test_reentered_account_does_not_add_principal_again():
    a = IndependentAccount('000001', '再次买入', cash=10000, realized_pnl=1000, first_buy_at=FIRST)
    result = summary([a], [{'code':'000001','market_value':91000,'unrealized_pnl':0}], opening=101000)
    assert result['capital_inflow'] == result['new_account_count'] == 0
    assert result['initial_capital'] == 100000
    assert result['total_pnl'] == 1000
    assert result['account_day_pnl'] == 0


def test_monetary_activity_without_first_buy_marker_cannot_be_silently_excluded():
    with pytest.raises(ValueError, match='首次买入'):
        summary([IndependentAccount('000001', '缺历史', cash=101000, realized_pnl=1000)])
