from __future__ import annotations

import pytest

from app.quant.core.models import Bar
from app.quant.research.adx_comparison import (
    account_contributions, adx_cross_group, checkpoint_account, cohort_metrics,
    cohort_trades, cross_diagnostics, equal_time_weight_diagnostic,
)
from app.quant.research.factors import FactorSnapshot
from app.quant.research.scenarios import ADX_COMPARISON_SCENARIOS


def snapshot(value14=22., old14=20., value21=24., old21=20.):
    return FactorSnapshot('2026-06-22', '2026-06-18', {
        'adx_14': value14, 'adx_14_3_days_ago': old14,
        'adx_21': value21, 'adx_21_3_days_ago': old21,
    })


def closed_trade():
    return {'code': 'A', 'entry_signal_at': '2026-06-22T14:57:00',
            'entry_execution_at': '2026-06-23T09:33:00', 'exit_execution_at': '2026-07-01T09:33:00',
            'net_return': .1, 'net_pnl': 10., 'mae_return': -.05, 'mfe_return': .15,
            'holding_calendar_days': 8, 'holding_trading_days': 7}


def test_cross_groups_are_not_nested_and_missing_is_separate():
    assert len(ADX_COMPARISON_SCENARIOS) == 4
    assert adx_cross_group(snapshot()) == 'both_pass'
    assert adx_cross_group(snapshot(value21=19.)) == 'only_adx14'
    assert adx_cross_group(snapshot(value14=19.)) == 'only_adx21'
    assert adx_cross_group(snapshot(value14=19., value21=19.)) == 'both_reject'
    assert adx_cross_group(snapshot(value14=None)) == 'missing_factor'
    assert adx_cross_group(None) == 'missing_factor'


def test_signal_day_cohort_keeps_natural_exit_after_entry_window():
    row = closed_trade()
    result = {'closed_trade_rows': [row], 'event_rows': [], 'summary': {'end_holding': False}}
    rows = cohort_trades(result, entry_start='2026-06-22', entry_end='2026-06-22',
                         observation_end='2026-07-02', minute_bars={})
    assert len(rows) == 1
    assert rows[0]['exit_execution_at'] == row['exit_execution_at']
    assert rows[0]['net_return'] == .1
    assert rows[0]['outcome'] == 'closed'
    assert cohort_trades(result, entry_start='2026-06-23', entry_end='2026-06-23',
                         observation_end='2026-07-02', minute_bars={}) == []


def test_open_cohort_keeps_marked_value_without_fabricating_close_or_win():
    result = {
        'closed_trade_rows': [],
        'event_rows': [{'code': 'A', 'name': 'A', 'action': 'buy', 'signal_at': '2026-06-22T14:57:00',
                        'execution_at': '2026-06-23T09:33:00', 'execution_price': 10.,
                        'shares': 10, 'notional': 100., 'commission': 1.}],
        'summary': {'end_holding': True, 'end_market_value': 110., 'end_mark_price': 11.},
        'daily_rows': [{'trade_date': '2026-06-23'}, {'trade_date': '2026-06-24'}],
    }
    bars = {'2026-06-23': [Bar('2026-06-23T09:30:00', 10., 30., 1., 10.),
                           Bar('2026-06-23T09:33:00', 10., 12., 9., 11.)]}
    rows = cohort_trades(result, entry_start='2026-06-22', entry_end='2026-06-22',
                         observation_end='2026-06-24', minute_bars=bars)
    row = rows[0]
    assert row['net_return'] is row['exit_execution_at'] is None
    assert row['marked_return'] == pytest.approx(9/101)
    assert row['mae_return'] == pytest.approx(-.1)
    assert row['mfe_return'] == pytest.approx(.2)
    assert row['mark_date'] == '2026-06-24'
    metrics = cohort_metrics(rows)
    assert metrics['closed_count'] == 0
    assert metrics['open_count'] == metrics['entry_trade_count'] == 1
    assert metrics['closed_win_rate'] is None
    assert metrics['all_mean_asof_return'] == pytest.approx(9/101)
    assignments, groups = cross_diagnostics(rows, {'A': {'2026-06-22': snapshot(value14=19.)}})
    assert assignments[0]['cross_group'] == 'only_adx21'
    assert sum(group['entry_trade_count'] for group in groups) == 1


def test_account_group_contributions_keep_total_denominator_and_empty_states():
    pairs = [
        {'baseline_filled_buy_count': 1, 'candidate_filled_buy_count': 1, 'baseline_end_holding': True,
         'candidate_end_holding': True, 'return_delta': .3},
        {'baseline_filled_buy_count': 1, 'candidate_filled_buy_count': 0, 'baseline_end_holding': False,
         'candidate_end_holding': False, 'return_delta': -.1},
        {'baseline_filled_buy_count': 0, 'candidate_filled_buy_count': 0, 'baseline_end_holding': False,
         'candidate_end_holding': False, 'return_delta': 0.},
    ]
    rows = account_contributions(pairs)
    for dimension in ('ever_bought', 'end_holding'):
        group = [row for row in rows if row['dimension'] == dimension]
        assert len(group) == 4
        assert sum(row['account_count'] for row in group) == 3
        assert sum(row['return_delta_contribution'] for row in group) == pytest.approx(.2/3)
        assert all(row['denominator'] == 3 for row in group)
    both = next(row for row in rows if row['dimension'] == 'ever_bought' and row['baseline_state'] and row['candidate_state'])
    assert both['return_delta_contribution'] == pytest.approx(.1)
    assert both['within_group_mean_return_delta'] == .3


def test_checkpoint_uses_that_days_realized_and_open_state_not_future_exit():
    result = {
        'daily_rows': [{'trade_date': '2026-06-30', 'total_assets': 105., 'shares_at_close': 1,
                        'realized_pnl_cumulative': 2., 'unrealized_pnl': 3.},
                       {'trade_date': '2026-07-01', 'total_assets': 110., 'shares_at_close': 0,
                        'realized_pnl_cumulative': 10., 'unrealized_pnl': 0.}],
        'event_rows': [{'action': 'buy', 'execution_at': '2026-06-23T09:33:00'}],
        'closed_trade_rows': [closed_trade()],
    }
    row = checkpoint_account(result, code='A', name='A', initial_cash=100., checkpoint='2026-06-30')
    assert row['end_holding'] is True
    assert row['closed_trade_count'] == 0
    assert row['mean_net_return'] is None
    assert row['realized_return'] == .02
    assert row['unrealized_return'] == .03
    assert row['total_return'] == pytest.approx(.05)


def test_time_weighting_uses_common_baseline_counts_not_group_counts():
    rows = [
        {'scenario': 'X', 'fold': fold, 'group': group, 'trade_count': count, 'mean_net_return': value}
        for fold, counts, values in ((1, (1, 9), (-.1, 0.)), (2, (8, 2), (.2, .1)), (3, (1, 1), (.1, .1)))
        for group, count, value in zip(('retained', 'rejected'), counts, values)
    ]
    result = equal_time_weight_diagnostic(rows)[0]
    assert result['baseline_trade_count'] == 22
    assert result['pooled_mean_difference'] > 0
    assert result['equal_time_weight_difference'] == pytest.approx(0)


def test_disagreement_day_weighting_exposes_composition_and_does_not_drop_empty_days():
    from app.quant.research.adx_comparison import entry_day_sensitivity

    rows = []
    for day, group, count, value in (
        ('2026-06-22', 'only_adx14', 9, 0.), ('2026-06-22', 'only_adx21', 1, -.1),
        ('2026-06-23', 'only_adx14', 1, .1), ('2026-06-23', 'only_adx21', 9, .2),
    ):
        rows.extend({**closed_trade(), 'entry_signal_at': day + 'T10:00:00',
                     'cost_mode': 'normal', 'cross_group': group, 'outcome': 'closed',
                     'net_return': value, 'net_pnl': value * 100, 'asof_return': value}
                    for _ in range(count))
    daily, sensitivity = entry_day_sensitivity(rows)
    result = next(row for row in sensitivity if row['measure'] == 'closed')
    assert len(daily) == 10
    assert result['pooled_difference'] == pytest.approx(.16)
    assert result['same_entry_day_weight_difference'] == pytest.approx(0.)
    assert result['common_sample_day_count'] == 2
    only_one_day_paired = [row for row in rows if not (
        row['entry_signal_at'].startswith('2026-06-23') and row['cross_group'] == 'only_adx14')]
    _, sensitivity = entry_day_sensitivity(only_one_day_paired)
    assert sensitivity[0]['same_entry_day_weight_difference'] is None
    assert sensitivity[0]['common_sample_day_count'] == 1
