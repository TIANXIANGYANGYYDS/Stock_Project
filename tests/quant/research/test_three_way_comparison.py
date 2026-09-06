from dataclasses import replace

import pytest

from app.quant.cli.compare_adx14_macd_hold import buy_and_hold
from app.quant.core.models import Bar
from app.quant.strategies.provisional_daily_macd_3m import official_backtest_config


def run_hold(daily, minutes, **config):
    return buy_and_hold(code='000001', name='测试', daily_bars=daily, minutes=minutes,
        start_date='2026-06-22', end_date='2026-06-23',
        config=replace(official_backtest_config(code='000001'), **config))


def test_hold_uses_open_lots_paid_fee_and_marks_without_sale_fee():
    daily = [Bar('2026-06-18', 10, 10, 10, 10), Bar('2026-06-22', 10, 13, 9, 12),
             Bar('2026-06-23', 12, 13, 11, 13)]
    minutes = {'2026-06-22': [Bar('2026-06-22T09:33:00+08:00', 10, 12, 9, 12)]}
    result = run_hold(daily, minutes, slippage_rate=0)
    # 10,000 shares would exceed cash after buy commission: buy 9,900 instead.
    assert result['entry']['shares'] == 9900
    assert result['entry']['commission'] == 9.90
    assert result['entry']['cash_after'] == 990.10
    assert result['account']['final_assets'] == 129690.10
    assert result['account']['realized_return'] == 0
    assert result['account']['closed_trade_count'] == 0
    assert result['account']['unrealized_return'] == pytest.approx(.296901)
    assert len(result['daily_rows']) == 2


def test_hold_retries_limit_up_without_using_bar_close_to_enter():
    daily = [Bar('2026-06-18', 10, 10, 10, 10), Bar('2026-06-22', 11, 11, 10, 10.5),
             Bar('2026-06-23', 10.5, 10.5, 10.5, 10.5)]
    minutes = {'2026-06-22': [Bar('2026-06-22T09:33:00+08:00', 11, 11, 10, 10),
                             Bar('2026-06-22T09:36:00+08:00', 10.5, 10.6, 10.4, 10.5)]}
    result = run_hold(daily, minutes)
    assert result['entry']['entry_bar_end'].endswith('09:36:00+08:00')
    assert result['entry']['execution_price'] == pytest.approx(10.50525)
    assert result['entry']['prior_attempt_count'] == 1


def test_hold_insufficient_cash_remains_in_full_universe():
    daily = [Bar('2026-06-18', 2000, 2000, 2000, 2000), Bar('2026-06-22', 2000, 2000, 2000, 2000)]
    minutes = {'2026-06-22': [Bar('2026-06-22T09:33:00+08:00', 2000, 2000, 2000, 2000)]}
    result = run_hold(daily, minutes)
    assert result['entry'] is None
    assert result['account']['final_assets'] == 100000
    assert result['account']['filled_buy_count'] == 0
    assert result['account']['total_return'] == 0


def test_hold_waits_for_available_day_and_never_reads_after_end():
    daily = [Bar('2026-06-18', 10, 10, 10, 10), Bar('2026-06-22', 10, 10, 10, 10),
             Bar('2026-06-23', 10, 10, 10, 10), Bar('2026-06-24', 20, 20, 20, 20)]
    minutes = {'2026-06-23': [Bar('2026-06-23T09:33:00+08:00', 10, 10, 10, 10)]}
    result = run_hold(daily, minutes)
    assert result['entry']['delayed_from_start']
    assert result['daily_rows'][0]['total_assets'] == 100000
    assert result['account']['last_valuation_date'] == '2026-06-23'
    assert result['account']['final_assets'] == 99990.10
