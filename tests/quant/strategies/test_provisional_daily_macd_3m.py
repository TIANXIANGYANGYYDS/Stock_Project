from __future__ import annotations

from app.quant.core.models import IndicatorBar
from app.quant.strategies import (
    DEFAULT_STRATEGY,
    STRATEGIES,
    STRATEGY_LABELS,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    DAILY_MACD_PARAMETERS,
    DAILY_WARMUP_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    INTRADAY_INTERVAL,
    MINIMUM_SHRINK_RATIO,
    STRATEGY_ID,
    confirm_provisional_histogram,
    determine_observation_action,
    official_backtest_config,
)


def indicator(histogram: float) -> IndicatorBar:
    return IndicatorBar(
        trade_date="2026-08-01",
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        dif=0.0,
        dea=0.0,
        histogram=histogram,
    )


def test_first_deterministic_strategy_has_stable_identity_and_parameters() -> None:
    assert STRATEGY_ID == "provisional_daily_macd_3m_v1"
    assert DEFAULT_STRATEGY == STRATEGY_ID
    assert STRATEGIES == (STRATEGY_ID,)
    assert tuple(STRATEGY_LABELS) == (STRATEGY_ID,)
    assert DAILY_MACD_PARAMETERS == (20, 100, 30)
    assert DAILY_WARMUP_BARS == 130
    assert INTRADAY_INTERVAL == "3m"
    assert EXPECTED_INTRADAY_BARS_PER_DAY == 80
    assert MINIMUM_SHRINK_RATIO == 0.01
    assert CONFIRMATION_BARS == 3


def test_official_buy_and_sell_rules_are_symmetric() -> None:
    assert determine_observation_action(indicator(-0.2), indicator(-0.3)) == "buy"
    assert determine_observation_action(indicator(0.2), indicator(0.3)) == "sell"
    assert confirm_provisional_histogram(
        action="buy",
        reference_histogram=-0.3,
        provisional_histogram=-0.29,
    )[0]
    assert confirm_provisional_histogram(
        action="sell",
        reference_histogram=0.3,
        provisional_histogram=0.29,
    )[0]


def test_official_configuration_is_fixed() -> None:
    config = official_backtest_config(code="002491")

    assert config.code == "002491"
    assert config.initial_cash == 100_000.0
    assert config.commission_rate == 0.0001
    assert config.stamp_duty_rate == 0.0005
    assert config.slippage_rate == 0.0005
    assert config.lot_size == 100
