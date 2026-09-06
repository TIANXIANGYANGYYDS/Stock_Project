from __future__ import annotations

import pytest

from app.quant.core.indicators import calculate_macd, validate_bars, validate_config
from app.quant.core.models import BacktestConfig, Bar


def test_constant_price_has_zero_macd() -> None:
    config = BacktestConfig(fast_period=2, slow_period=3, signal_period=2)
    bars = [Bar(f"2026-01-{day:02d}", 10, 10, 10, 10) for day in range(1, 7)]

    output = calculate_macd(bars, config)

    assert all(item.dif == 0 for item in output)
    assert all(item.dea == 0 for item in output)
    assert all(item.histogram == 0 for item in output)


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="MACD 周期"):
        validate_config(BacktestConfig(fast_period=100, slow_period=20))


def test_invalid_ohlc_is_rejected() -> None:
    with pytest.raises(ValueError, match="OHLC"):
        validate_bars([Bar("2026-01-01", 10, 9, 8, 10)])
