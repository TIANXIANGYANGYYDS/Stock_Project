from __future__ import annotations

import json

import pytest

from app.quant.research.adx_left_tail import (
    FINALIST_SCENARIO_KEYS, closed_risk_metrics, daily_weighted_risk, disagreement_reasons,
)


def trade(day='2026-06-22', group='only_adx14', result=-.2, outcome='closed'):
    values = {'adx_14': 25., 'adx_14_3_days_ago': 23., 'adx_21': 19., 'adx_21_3_days_ago': 18.}
    if group == 'only_adx21':
        values = {'adx_14': 25., 'adx_14_3_days_ago': 25., 'adx_21': 22., 'adx_21_3_days_ago': 21.}
    return {
        'code': '000001', 'cost_mode': 'normal', 'entry_signal_at': day + 'T10:00:00',
        'factor_completed_date': '2026-06-18', 'cross_group': group, 'factor_values': json.dumps(values),
        'outcome': outcome, 'net_return': result if outcome == 'closed' else None,
        'net_pnl': result * 1000 if outcome == 'closed' else None,
        'mae_return': -.2, 'mfe_return': .1, 'holding_trading_days': 10, 'holding_calendar_days': 14,
        'asof_return': result, 'marked_return': result if outcome == 'open' else None,
        'market_value': 900 if outcome == 'open' else None,
        'unrealized_pnl': -100 if outcome == 'open' else None,
    }


def test_three_rules_are_fixed_without_new_parameters():
    assert FINALIST_SCENARIO_KEYS == (
        'baseline', 'adx_n21_ge_20_and_rising_3d', 'adx_n14_ge_20_and_rising_3d',
    )


def test_disagreement_cause_uses_strict_rising_and_inclusive_twenty():
    first, second = trade(), trade(group='only_adx21')
    assignments, reasons = disagreement_reasons([first, second])
    assert [row['rejection_reason'] for row in assignments] == [
        'below_20_and_rising', 'at_least_20_and_not_rising',
    ]
    assert assignments[1]['adx14_change_3d'] == 0
    assert sum(row['entry_trade_count'] for row in reasons) == 2
    bad = {**first, 'cross_group': 'both_pass'}
    with pytest.raises(ValueError, match='不一致'):
        disagreement_reasons([bad])
    with pytest.raises(ValueError, match='早于'):
        disagreement_reasons([{**first, 'factor_completed_date': '2026-06-22'}])


def test_loss_boundary_and_unclosed_trades_are_not_counted_as_final_losses():
    rows = [trade(result=-.1), trade(result=-.10001), trade(result=-.9, outcome='open')]
    metrics = closed_risk_metrics(rows)
    assert metrics['closed_count'] == 2 and metrics['open_count'] == 1
    assert metrics['loss_over_10pct_count'] == 1
    assert metrics['loss_over_10pct_rate'] == .5
    assert closed_risk_metrics(rows[2:])['loss_over_10pct_rate'] is None


def test_common_weights_include_other_baseline_groups_and_missing_factors():
    rows = [trade(result=-.2), trade(group='only_adx21', result=.1)]
    rows += [{**trade(result=0.), 'cross_group': 'missing_factor'} for _ in range(6)]
    rows += [trade(day='2026-06-23', result=.1), trade(day='2026-06-23', group='only_adx21', result=-.2)]
    daily, comparisons = daily_weighted_risk(rows)
    loss = next(row for row in comparisons if row['metric'] == 'loss_over_10pct_rate')
    assert loss['baseline_closed_denominator'] == 10
    assert loss['raw_adx14'] == loss['raw_adx21'] == .5
    assert loss['weighted_adx14'] == .8 and loss['weighted_adx21'] == .2
    assert loss['weighted_difference_21_minus_14'] == pytest.approx(-.6)
    assert loss['positive_difference_dates'] == loss['negative_difference_dates'] == 1
    assert next(row['common_weight'] for row in daily if row['entry_date'] == '2026-06-22') == .8


def test_empty_group_does_not_become_zero_or_get_renormalized_away():
    rows = [trade(), trade(group='only_adx21'), trade(day='2026-06-23')]
    _, comparisons = daily_weighted_risk(rows)
    assert all(row['weighted_adx14'] is None and row['weighted_adx21'] is None for row in comparisons)
    assert all(row['missing_pair_date_count'] == 1 for row in comparisons)
    assert all(row['eligible_entry_date_count'] == 2 for row in comparisons)
