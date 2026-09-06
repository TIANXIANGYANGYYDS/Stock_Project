from __future__ import annotations

from app.quant.research.factors import FactorSnapshot
from app.quant.research.scenarios import (
    AUXILIARY_INDICATOR_GRID,
    CORE_FACTOR_SCENARIOS,
    FACTOR_SCENARIOS,
    RSI_CONTROL_SCENARIOS,
    are_grid_neighbors,
)


def _scenario(key: str):
    return next(item for item in FACTOR_SCENARIOS if item.key == key)


def test_first_round_grid_is_exactly_frozen_at_66_auxiliary_scenarios() -> None:
    assert AUXILIARY_INDICATOR_GRID["rs_rank"] == {
        "lookback": (20, 60, 120),
        "skip_recent": (0, 5),
        "min_percentile": (0.60, 0.70, 0.80),
    }
    assert len(CORE_FACTOR_SCENARIOS) == 54
    assert len(RSI_CONTROL_SCENARIOS) == 12
    assert len(FACTOR_SCENARIOS) == 66
    assert len({item.key for item in FACTOR_SCENARIOS}) == 66
    assert {item.factor for item in CORE_FACTOR_SCENARIOS} == {
        "RSRank",
        "RTOV",
        "ADX",
        "NATR",
    }


def test_scenario_boundaries_and_missing_values_are_explicit() -> None:
    snapshot = FactorSnapshot(
        signal_date="2026-08-01",
        completed_date="2026-07-31",
        values={
            "rs_rank_l60_s5": 0.70,
            "rtov_20": 1.0,
            "adx_14": 20.0,
            "adx_14_3_days_ago": 21.0,
            "natr_rank_14": 0.80,
            "rsi_14": 40.0,
        },
    )

    assert _scenario("rs_l60_s5_q70").accepts(snapshot)
    assert _scenario("rtov_n20_10_30").accepts(snapshot)
    assert _scenario("adx_n14_ge_20_and_falling_3d").accepts(snapshot)
    assert _scenario("natr_n14_q20_80").accepts(snapshot)
    assert _scenario("rsi_n14_40_60").accepts(snapshot)
    assert not _scenario("rs_l20_s5_q70").accepts(snapshot)


def test_grid_neighbors_change_only_one_adjacent_coordinate() -> None:
    center = _scenario("rs_l60_s5_q70")

    assert are_grid_neighbors(center, _scenario("rs_l60_s5_q60"))
    assert are_grid_neighbors(center, _scenario("rs_l120_s5_q70"))
    assert not are_grid_neighbors(center, _scenario("rs_l120_s0_q80"))
    assert not are_grid_neighbors(center, _scenario("adx_n14_lt_20"))


def test_grid_neighbors_do_not_treat_opposite_adx_rules_as_a_platform() -> None:
    assert not are_grid_neighbors(
        _scenario("adx_n21_ge_20_and_falling_3d"),
        _scenario("adx_n21_ge_20_and_rising_3d"),
    )
    assert are_grid_neighbors(
        _scenario("adx_n7_ge_20_and_rising_3d"),
        _scenario("adx_n14_ge_20_and_rising_3d"),
    )


def test_rtov_platform_uses_comparable_neighboring_rules() -> None:
    assert are_grid_neighbors(
        _scenario("rtov_n10_ge_08"),
        _scenario("rtov_n10_ge_10"),
    )
    assert are_grid_neighbors(
        _scenario("rtov_n10_ge_10"),
        _scenario("rtov_n10_10_30"),
    )
    assert not are_grid_neighbors(
        _scenario("rtov_n10_ge_12"),
        _scenario("rtov_n10_10_30"),
    )
