from __future__ import annotations

import pytest

from app.quant.research.evaluation import (
    chronological_folds,
    expected_shortfall,
    matched_random_control,
    maximum_consecutive_losses,
    signal_distribution,
    trade_metrics,
)


def _trade(pnl: float, net_return: float, mae: float, mfe: float) -> dict:
    return {
        "net_pnl": pnl,
        "net_return": net_return,
        "mae_return": mae,
        "mfe_return": mfe,
        "holding_calendar_days": 4,
        "holding_trading_days": 3,
        "exit_execution_at": "2026-01-01T10:00:00+08:00",
    }


def test_signal_distribution_includes_zero_days_and_tail_dates() -> None:
    metrics = signal_distribution([10, 0, 20, 10], [5, 0, 4, 1])

    assert metrics["original_daily_signal_mean"] == pytest.approx(10.0)
    assert metrics["filtered_daily_signal_median"] == pytest.approx(2.5)
    assert metrics["signal_retention_rate"] == pytest.approx(0.25)
    assert metrics["zero_signal_day_rate"] == pytest.approx(0.25)
    assert metrics["maximum_daily_signals"] == 5


def test_trade_quality_tail_and_right_winner_metrics_are_complete() -> None:
    trades = [
        _trade(100.0, 0.10, -0.03, 0.15),
        _trade(-50.0, -0.05, -0.08, 0.02),
        _trade(-20.0, -0.02, -0.04, 0.01),
        _trade(40.0, 0.04, -0.01, 0.08),
    ]
    metrics = trade_metrics(trades)

    assert metrics["net_expectancy"] == pytest.approx(17.5)
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["payoff_ratio"] == pytest.approx(2.0)
    assert metrics["profit_factor"] == pytest.approx(2.0)
    assert metrics["average_holding_trading_days"] == pytest.approx(3.0)
    assert metrics["maximum_consecutive_losses"] == 2
    assert metrics["maximum_single_trade_loss"] == pytest.approx(-0.05)
    assert metrics["mae_loss_p95"] > metrics["mae_loss_p90"]
    assert metrics["top_10pct_winner_profit_contribution"] == pytest.approx(
        100.0 / 140.0
    )
    assert expected_shortfall([0.10, -0.05, -0.02, 0.04], 0.95) == -0.05
    assert maximum_consecutive_losses([1, -1, -2, 1, -1]) == 2


def test_four_folds_and_random_control_are_deterministic() -> None:
    assert [len(item) for item in chronological_folds([str(i) for i in range(10)])] == [
        2,
        3,
        2,
        3,
    ]
    baseline = [
        _trade(float(index), index / 100.0, -0.01, 0.02)
        for index in range(-10, 11)
    ]
    candidate = baseline[-5:]

    first = matched_random_control(
        baseline_trades=baseline,
        candidate_trades=candidate,
        seed=42,
        iterations=100,
    )
    second = matched_random_control(
        baseline_trades=baseline,
        candidate_trades=candidate,
        seed=42,
        iterations=100,
    )

    assert first == second
    assert first["random_expectancy_percentile"] > 0.95
