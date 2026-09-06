"""第一轮因子实验的冻结评估口径。"""

from __future__ import annotations

import math
import random
from statistics import mean, median
from typing import Any, Optional, Sequence


RANDOM_CONTROL_ITERATIONS = 1_000
RANDOM_CONTROL_REQUIRED_PERCENTILE = 0.95
MIN_OOS_CLOSED_TRADES = 20
MIN_FOLD_CLOSED_TRADES = 5
RISK_WORSENING_TOLERANCE = 1.05
PLATFORM_EXPECTANCY_SIMILARITY = 0.75


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("百分位序列不能为空")
    if not 0 <= probability <= 1:
        raise ValueError("百分位概率必须位于[0,1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _percentile_or_none(
    values: Sequence[float], probability: float
) -> Optional[float]:
    return percentile(values, probability) if values else None


def maximum_drawdown(values: Sequence[float]) -> float:
    peak = 0.0
    drawdown = 0.0
    for value in values:
        if value <= 0:
            raise ValueError("资产必须大于0")
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def expected_shortfall(
    returns: Sequence[float], confidence: float
) -> Optional[float]:
    """返回最差 ``1-confidence`` 交易收益的平均值。"""

    if not returns:
        return None
    tail_count = max(1, math.ceil(len(returns) * (1.0 - confidence)))
    return mean(sorted(returns)[:tail_count])


def maximum_consecutive_losses(pnls: Sequence[float]) -> int:
    longest = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def signal_distribution(
    original_counts: Sequence[int], filtered_counts: Sequence[int]
) -> dict[str, Any]:
    if not original_counts or len(original_counts) != len(filtered_counts):
        raise ValueError("原始与过滤信号必须覆盖相同的非空交易日")
    original_total = sum(original_counts)
    filtered_total = sum(filtered_counts)
    return {
        "original_daily_signal_mean": mean(original_counts),
        "original_daily_signal_median": median(original_counts),
        "original_daily_signal_p10": percentile(original_counts, 0.10),
        "original_daily_signal_p90": percentile(original_counts, 0.90),
        "filtered_daily_signal_mean": mean(filtered_counts),
        "filtered_daily_signal_median": median(filtered_counts),
        "filtered_daily_signal_p10": percentile(filtered_counts, 0.10),
        "filtered_daily_signal_p90": percentile(filtered_counts, 0.90),
        "signal_retention_rate": (
            filtered_total / original_total if original_total else None
        ),
        "zero_signal_day_rate": sum(item == 0 for item in filtered_counts)
        / len(filtered_counts),
        "maximum_daily_signals": max(filtered_counts),
        "original_signal_count": original_total,
        "filtered_signal_count": filtered_total,
    }


def trade_metrics(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["net_return"]) for item in trades]
    pnls = [float(item["net_pnl"]) for item in trades]
    positive_returns = [item for item in returns if item > 0]
    negative_returns = [item for item in returns if item < 0]
    gross_profit = sum(item for item in pnls if item > 0)
    gross_loss = abs(sum(item for item in pnls if item < 0))
    mae_losses = [max(0.0, -float(item["mae_return"])) for item in trades]
    mfe_returns = [float(item["mfe_return"]) for item in trades]
    calendar_days = [float(item["holding_calendar_days"]) for item in trades]
    trading_days = [float(item["holding_trading_days"]) for item in trades]
    profitable_trades = sorted(
        (item for item in trades if float(item["net_pnl"]) > 0),
        key=lambda item: float(item["net_pnl"]),
        reverse=True,
    )
    top_count = (
        max(1, math.ceil(len(profitable_trades) * 0.10))
        if profitable_trades
        else 0
    )
    return {
        "trade_count": len(trades),
        "net_expectancy": mean(pnls) if pnls else None,
        "mean_net_return": mean(returns) if returns else None,
        "median_net_return": median(returns) if returns else None,
        "win_rate": (
            sum(item > 0 for item in pnls) / len(pnls) if pnls else None
        ),
        "payoff_ratio": (
            mean(positive_returns) / abs(mean(negative_returns))
            if positive_returns and negative_returns
            else None
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_calendar_days": (
            mean(calendar_days) if calendar_days else None
        ),
        "average_holding_trading_days": (
            mean(trading_days) if trading_days else None
        ),
        "mean_mae_return": (
            mean(float(item["mae_return"]) for item in trades)
            if trades
            else None
        ),
        "mean_mfe_return": mean(mfe_returns) if mfe_returns else None,
        "trade_return_p10": _percentile_or_none(returns, 0.10),
        "trade_return_p90": _percentile_or_none(returns, 0.90),
        "trade_return_p95": _percentile_or_none(returns, 0.95),
        "es90_return": expected_shortfall(returns, 0.90),
        "es95_return": expected_shortfall(returns, 0.95),
        "worst_1pct_mean_return": expected_shortfall(returns, 0.99),
        "worst_5pct_mean_return": expected_shortfall(returns, 0.95),
        "maximum_consecutive_losses": maximum_consecutive_losses(pnls),
        "maximum_single_trade_loss": min(returns) if returns else None,
        "mae_loss_p90": _percentile_or_none(mae_losses, 0.90),
        "mae_loss_p95": _percentile_or_none(mae_losses, 0.95),
        "top_10pct_winner_profit_contribution": (
            sum(float(item["net_pnl"]) for item in profitable_trades[:top_count])
            / gross_profit
            if gross_profit
            else None
        ),
    }


def portfolio_metrics(
    assets: Sequence[float], *, capital_base: float
) -> dict[str, Any]:
    if not assets:
        raise ValueError("组合资产序列不能为空")
    total_return = assets[-1] / capital_base - 1.0
    drawdown = maximum_drawdown([capital_base] + list(assets))
    return {
        "final_assets": assets[-1],
        "total_return": total_return,
        "maximum_drawdown": drawdown,
        "return_drawdown_ratio": total_return / drawdown if drawdown else None,
    }


def chronological_folds(
    market_dates: Sequence[str], fold_count: int = 4
) -> list[list[str]]:
    if len(market_dates) < fold_count:
        raise ValueError("交易日数量少于时间折数量")
    return [
        list(
            market_dates[
                index * len(market_dates) // fold_count :
                (index + 1) * len(market_dates) // fold_count
            ]
        )
        for index in range(fold_count)
    ]


def fold_metrics(
    *,
    market_dates: Sequence[str],
    assets_by_date: dict[str, float],
    trades: Sequence[dict[str, Any]],
    capital_base: float,
) -> list[dict[str, Any]]:
    folds = chronological_folds(market_dates)
    output: list[dict[str, Any]] = []
    for fold_index, fold_dates in enumerate(folds):
        start_position = market_dates.index(fold_dates[0])
        starting_assets = (
            capital_base
            if start_position == 0
            else assets_by_date[market_dates[start_position - 1]]
        )
        assets = [assets_by_date[item] for item in fold_dates]
        fold_trades = [
            item
            for item in trades
            if fold_dates[0] <= str(item["exit_execution_at"])[:10] <= fold_dates[-1]
        ]
        quality = trade_metrics(fold_trades)
        fold_return = assets[-1] / starting_assets - 1.0
        drawdown = maximum_drawdown([starting_assets] + assets)
        output.append(
            {
                "fold": fold_index + 1,
                "start_date": fold_dates[0],
                "end_date": fold_dates[-1],
                "trade_count": len(fold_trades),
                "net_expectancy": quality["net_expectancy"],
                "total_return": fold_return,
                "maximum_drawdown": drawdown,
                "return_drawdown_ratio": (
                    fold_return / drawdown if drawdown else None
                ),
            }
        )
    return output


def _risk_adjusted_trade_score(trades: Sequence[dict[str, Any]]) -> Optional[float]:
    if not trades:
        return None
    returns = [float(item["net_return"]) for item in trades]
    es95 = expected_shortfall(returns, 0.95)
    if es95 is None or es95 >= 0:
        return None
    return mean(returns) / abs(es95)


def matched_random_control(
    *,
    baseline_trades: Sequence[dict[str, Any]],
    candidate_trades: Sequence[dict[str, Any]],
    seed: int,
    iterations: int = RANDOM_CONTROL_ITERATIONS,
) -> dict[str, Any]:
    """从基线闭合交易中无放回抽取同数量交易作为随机对照。"""

    sample_size = len(candidate_trades)
    if sample_size == 0 or sample_size > len(baseline_trades):
        return {
            "random_control_available": False,
            "random_expectancy_percentile": None,
            "random_risk_score_percentile": None,
        }
    candidate_expectancy = mean(
        float(item["net_pnl"]) for item in candidate_trades
    )
    candidate_score = _risk_adjusted_trade_score(candidate_trades)
    rng = random.Random(seed)
    random_expectancies: list[float] = []
    random_scores: list[float] = []
    population = list(baseline_trades)
    for _ in range(iterations):
        sample = rng.sample(population, sample_size)
        random_expectancies.append(mean(float(item["net_pnl"]) for item in sample))
        score = _risk_adjusted_trade_score(sample)
        if score is not None:
            random_scores.append(score)

    def empirical_percentile(values: Sequence[float], value: float) -> float:
        return (sum(item <= value for item in values) + 1) / (len(values) + 1)

    return {
        "random_control_available": True,
        "random_expectancy_percentile": empirical_percentile(
            random_expectancies, candidate_expectancy
        ),
        "random_risk_score_percentile": (
            empirical_percentile(random_scores, candidate_score)
            if candidate_score is not None and random_scores
            else None
        ),
    }
