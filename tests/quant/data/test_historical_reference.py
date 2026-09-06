from __future__ import annotations

import pytest

from app.quant.data.historical_reference import recover_previous_close


def inputs():
    return dict(daily={'close': 11.4, 'change_amount': .13, 'pct_chg': 1.15},
                previous_daily={'close': 11.27}, observed_close=11.4, previous_observed_close=11.27)


def test_previous_reference_is_recovered_without_changing_signal_prices():
    value, method = recover_previous_close(**inputs())
    assert value == 11.27
    assert method == 'validated_daily_change_reference'


@pytest.mark.parametrize('key,value', [
    ('observed_close', None), ('daily', None), ('previous_daily', None),
    ('observed_close', 12), ('previous_observed_close', 11.28),
    ('previous_daily', {'close': 11.28}),
    ('daily', {'close': 11.4, 'change_amount': .13, 'pct_chg': 20}),
    ('daily', {'close': 11.4}), ('observed_close', float('nan')),
])
def test_missing_or_inconsistent_input_does_not_invent_reference(key, value):
    args = inputs()
    args[key] = value
    reference, method = recover_previous_close(**args)
    assert reference is None
    assert method != 'validated_daily_change_reference'


def test_absent_previous_last_minute_can_use_agreeing_daily_reference():
    args = inputs()
    args['previous_observed_close'] = None
    assert recover_previous_close(**args)[0] == 11.27


def test_future_closing_price_changes_only_verify_the_same_known_reference():
    first = inputs()
    second = dict(daily={'close': 11.5, 'change_amount': .23, 'pct_chg': 2.04},
                  previous_daily={'close': 11.27}, observed_close=11.5, previous_observed_close=11.27)
    assert recover_previous_close(**first)[0] == recover_previous_close(**second)[0] == 11.27


def history_fixture():
    from app.quant.runtime.live import three_minute_bar_ends, LiveThreeMinuteBar
    rows = [{'timestamp': t, 'open': 10., 'high': 10.1, 'low': 9.9, 'close': 10., 'adjust': 'qfq', 'interval': '3m'}
            for t in three_minute_bar_ends('2026-08-20')]
    observed = [LiveThreeMinuteBar(start_at=r['timestamp'], end_at=r['timestamp'], open=10., high=10.1,
                                  low=9.9, close=10., previous_close=None) for r in rows[:20]]
    return rows, observed


def test_complete_history_day_requires_matching_timestamps_and_observed_price_basis():
    from app.quant.data.historical_reference import validate_history_day
    rows, observed = history_fixture()
    assert validate_history_day(rows=rows, observed_bars=observed, trade_date='2026-08-20')[0]
    assert not validate_history_day(rows=rows[:-1], observed_bars=observed, trade_date='2026-08-20')[0]
    assert not validate_history_day(rows=rows, observed_bars=observed[:2], trade_date='2026-08-20')[0]
    for r in rows:
        r.update(open=9., high=9.1, low=8.9, close=9.)
    assert not validate_history_day(rows=rows, observed_bars=observed, trade_date='2026-08-20')[0]
