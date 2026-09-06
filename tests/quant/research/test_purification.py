from __future__ import annotations

from collections import Counter

import pytest

from app.quant.research.factors import FactorSnapshot
from app.quant.research.purification import (
    account_result,
    baseline_trade_diagnostics,
    paired_account_results,
)
from app.quant.research.scenarios import FACTOR_SCENARIOS, PURIFICATION_SCENARIOS


def _trade(code, signal_date, net_return):
    return {
        "code": code, "trade_id": 1, "entry_signal_at": signal_date + "T10:00:00",
        "entry_execution_at": "2026-07-06T09:33:00",
        "exit_execution_at": "2026-07-07T10:00:00",
        "net_return": net_return, "net_pnl": net_return * 1000,
        "mae_return": -.1, "mfe_return": .2,
        "holding_calendar_days": 1, "holding_trading_days": 2,
    }


def test_frozen_twelve_grid_reuses_exact_existing_single_factor_rules():
    assert len(PURIFICATION_SCENARIOS) == 11
    assert len({item.key for item in PURIFICATION_SCENARIOS}) == 11
    assert Counter(item.factor for item in PURIFICATION_SCENARIOS) == {"ADX": 3, "RTOV": 6, "RSRank": 2}
    assert all(any(item is original for original in FACTOR_SCENARIOS) for item in PURIFICATION_SCENARIOS)


def test_diagnostics_partition_baseline_by_signal_day_not_execution_or_exit_day():
    dates = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
    scenario = next(item for item in PURIFICATION_SCENARIOS if item.key == "rtov_n10_ge_10")
    snapshots = {"A": {
        "2026-07-01": FactorSnapshot("2026-07-01", "2026-06-30", {"rtov_10": 1.1}),
        "2026-07-02": FactorSnapshot("2026-07-02", "2026-07-01", {"rtov_10": .9}),
        "2026-07-06": FactorSnapshot("2026-07-06", "2026-07-03", {"rtov_10": .8}),
    }}
    trades = [_trade("A", dates[0], .2), _trade("A", dates[1], -.1), _trade("B", dates[2], -.3)]
    assignments, metrics = baseline_trade_diagnostics(
        baseline_trades=trades, scenarios=[scenario], snapshots=snapshots,
        market_dates=dates, account_count=3,
    )
    assert [row["group"] for row in assignments] == ["retained", "rejected", "rejected"]
    assert [row["net_return"] for row in assignments] == [.2, -.1, -.3]
    assert [row["entry_fold"] for row in assignments] == [1, 2, 3]
    assert [row["rejection_reason"] for row in assignments] == ["", "threshold", "missing_factor"]
    kept, rejected = [row for row in metrics if row["fold"] == 0]
    assert kept["trade_count"] + rejected["trade_count"] == len(trades)
    assert kept["mean_net_return"] == .2
    assert rejected["mean_net_return"] == -.2
    assert rejected["missing_factor_count"] == 1
    assert kept["stock_coverage_rate"] == pytest.approx(1/3)
    assert rejected["loss_lt_minus20pct_count"] == 1
    empty = next(row for row in metrics if row["fold"] == 4 and row["group"] == "retained")
    assert empty["trade_count"] == 0
    assert empty["mean_net_return"] is None
    assert empty["win_rate"] is None


def test_pairing_keeps_no_trade_accounts_and_marks_open_holdings_in_return():
    flat = account_result(code="A", name="A", initial_cash=1000, result=None)
    baseline = [flat, {**flat, "code": "B", "total_return": .1, "final_assets": 1100, "filled_buy_count": 1}, {**flat, "code": "C"}]
    open_account = account_result(code="A", name="A", initial_cash=1000, result={
        "summary": {"final_assets": 1050, "filled_buy_count": 1, "end_holding": True},
        "closed_trade_rows": [],
        "daily_rows": [{"total_assets": 900}, {"total_assets": 1050}],
    })
    candidates = [open_account, {**flat, "code": "B"}, {**flat, "code": "C"}]
    rows, summary = paired_account_results(baseline, candidates, scenario="test", scenario_label="test")
    assert open_account["maximum_drawdown"] == .1
    assert open_account["mean_net_return"] is None
    assert rows[0]["candidate_end_holding"] is True
    assert len(rows) == summary["account_count"] == 3
    assert summary["mean_return_delta"] == pytest.approx(-.05 / 3)
    assert summary["median_return_delta"] == 0
    assert summary["improved_account_count"] == summary["unchanged_account_count"] == summary["worsened_account_count"] == 1
    assert summary["candidate_no_buy_account_count"] == 2
    assert summary["candidate_mean_account_return"] == pytest.approx(sum(row["final_assets"] for row in candidates) / 3000 - 1)
    assert rows[2]["candidate_mean_net_return"] is None
    with pytest.raises(ValueError, match="同一批"):
        paired_account_results(baseline, candidates[:2], scenario="test", scenario_label="test")


def test_legacy_money_platform_is_separate_from_return_purification():
    from app.quant.research.purification import parameter_comparisons

    scenarios = [item for item in PURIFICATION_SCENARIOS if item.key in {
        "adx_n14_ge_20_and_rising_3d", "adx_n21_ge_20_and_rising_3d",
    }]
    metrics = [
        {"scenario": scenario.key, "oos_available": True,
         "oos_net_expectancy": value, "oos_baseline_net_expectancy": 1000}
        for scenario, value in zip(scenarios, (2000, 2500))
    ]
    diagnostics = [
        {"scenario": scenario.key, "fold": 0, "group": group, "mean_net_return": value}
        for scenario in scenarios for group, value in (("retained", .08), ("rejected", .02))
    ]
    row = parameter_comparisons(scenarios, metrics, diagnostics)[0]
    assert row["legacy_amount_gain_similarity"] == pytest.approx(2/3)
    assert row["legacy_75pct_platform_pass"] is False
    assert row["first_retained_minus_rejected_mean_return"] == pytest.approx(.06)
    assert row["second_retained_minus_rejected_mean_return"] == pytest.approx(.06)
