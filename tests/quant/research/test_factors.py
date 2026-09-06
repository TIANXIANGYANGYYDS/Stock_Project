from __future__ import annotations

from dataclasses import replace

import pytest

from app.quant.research.factors import (
    NATR_PERIODS,
    RS_LOOKBACKS,
    RS_SKIPS,
    FactorBar,
    FactorSnapshot,
    adx_comparison_key,
    adx_key,
    attach_cross_sectional_ranks,
    calculate_factor_snapshots,
    natr_key,
    natr_rank_key,
    percentile_rank,
    rs_momentum_key,
    rs_rank_key,
    rsi_key,
    rtov_key,
)


def _dates(count: int) -> list[str]:
    return [f"d{index:03d}" for index in range(count)]


def test_rs_grid_and_rtov_use_only_data_known_at_intraday_signal() -> None:
    market_dates = _dates(150)
    bars = [
        FactorBar(
            trade_date=trade_date,
            high=100.0 + index,
            low=100.0 + index,
            close=100.0 + index,
            volume=10.0,
            turnover_pct=10.0,
        )
        for index, trade_date in enumerate(market_dates)
    ]
    bars[129] = replace(bars[129], turnover_pct=30.0)

    snapshot = calculate_factor_snapshots(
        bars,
        market_dates=market_dates,
        signal_dates=[market_dates[130]],
    )[market_dates[130]]

    assert snapshot.completed_date == market_dates[129]
    assert snapshot.value(rs_momentum_key(20, 0)) == pytest.approx(
        229.0 / 210.0 - 1.0
    )
    assert snapshot.value(rs_momentum_key(20, 5)) == pytest.approx(
        225.0 / 210.0 - 1.0
    )
    assert snapshot.value(rs_momentum_key(120, 5)) == pytest.approx(
        225.0 / 110.0 - 1.0
    )
    assert snapshot.value(rtov_key(10)) == pytest.approx(3.0)
    assert snapshot.value(rtov_key(20)) == pytest.approx(3.0)
    assert snapshot.value(rtov_key(40)) == pytest.approx(3.0)


def test_constant_prices_have_zero_trend_and_volatility_and_neutral_rsi() -> None:
    market_dates = _dates(150)
    bars = [
        FactorBar(trade_date, 10.0, 10.0, 10.0, 100.0, 1.0)
        for trade_date in market_dates
    ]
    snapshot = calculate_factor_snapshots(
        bars,
        market_dates=market_dates,
        signal_dates=[market_dates[-1]],
    )[market_dates[-1]]

    for period in (7, 14, 21):
        assert snapshot.value(adx_key(period)) == pytest.approx(0.0)
        assert snapshot.value(adx_comparison_key(period)) == pytest.approx(0.0)
    for period in (10, 14, 20):
        assert snapshot.value(natr_key(period)) == pytest.approx(0.0)
    for period in (9, 14, 21):
        assert snapshot.value(rsi_key(period)) == pytest.approx(50.0)


def test_cross_sectional_rank_attaches_all_nine_rank_fields() -> None:
    values = {
        rs_momentum_key(lookback, skip): 2.0
        for lookback in RS_LOOKBACKS
        for skip in RS_SKIPS
    }
    values.update({natr_key(period): 2.0 for period in NATR_PERIODS})
    snapshot = FactorSnapshot("d130", "d129", values)
    populations = {key: [1.0, 2.0, 2.0, 4.0] for key in values}

    ranked = attach_cross_sectional_ranks(
        snapshot,
        populations=populations,
    )

    assert percentile_rank([1.0, 2.0, 2.0, 4.0], 2.0) == pytest.approx(0.625)
    for lookback in RS_LOOKBACKS:
        for skip in RS_SKIPS:
            assert ranked.value(rs_rank_key(lookback, skip)) == pytest.approx(
                0.625
            )
    for period in NATR_PERIODS:
        assert ranked.value(natr_rank_key(period)) == pytest.approx(0.625)
