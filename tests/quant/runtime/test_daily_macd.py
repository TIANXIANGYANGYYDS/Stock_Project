from __future__ import annotations

from app.quant.core.indicators import calculate_macd
from app.quant.core.models import BacktestConfig, Bar
from app.quant.runtime.daily_macd import (
    calculate_daily_macd_states,
    provisional_daily_indicator,
    provisional_daily_indicator_from_state,
)


def test_provisional_indicator_appends_one_independent_daily_bar() -> None:
    config = BacktestConfig(fast_period=2, slow_period=4, signal_period=3)
    completed = [
        Bar("2026-08-01", 10.0, 10.2, 9.9, 10.0),
        Bar("2026-08-02", 10.1, 10.4, 10.0, 10.3),
    ]
    provisional = provisional_daily_indicator(
        completed,
        trade_date="2026-08-03",
        day_open=10.2,
        high_so_far=10.5,
        low_so_far=10.1,
        current_close=10.4,
        config=config,
    )
    expected = calculate_macd(
        [*completed, Bar("2026-08-03", 10.2, 10.5, 10.1, 10.4)], config
    )[-1]
    assert provisional == expected
    from_state = provisional_daily_indicator_from_state(
        calculate_daily_macd_states(completed, config)[-1],
        trade_date="2026-08-03",
        day_open=10.2,
        high_so_far=10.5,
        low_so_far=10.1,
        current_close=10.4,
        config=config,
    )
    assert from_state == expected
