"""MACD 买入信号后置筛选使用的无未来函数日线因子。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from statistics import median
from typing import Optional, Sequence


RS_LOOKBACKS = (20, 60, 120)
RS_SKIPS = (0, 5)
RTOV_LOOKBACKS = (10, 20, 40)
ADX_PERIODS = (7, 14, 21)
NATR_PERIODS = (10, 14, 20)
RSI_PERIODS = (9, 14, 21)


def rs_momentum_key(lookback: int, skip_recent: int) -> str:
    return f"rs_momentum_l{lookback}_s{skip_recent}"


def rs_rank_key(lookback: int, skip_recent: int) -> str:
    return f"rs_rank_l{lookback}_s{skip_recent}"


def rtov_key(lookback: int) -> str:
    return f"rtov_{lookback}"


def adx_key(period: int) -> str:
    return f"adx_{period}"


def adx_comparison_key(period: int) -> str:
    return f"adx_{period}_3_days_ago"


def natr_key(period: int) -> str:
    return f"natr_{period}"


def natr_rank_key(period: int) -> str:
    return f"natr_rank_{period}"


def rsi_key(period: int) -> str:
    return f"rsi_{period}"


@dataclass(frozen=True)
class FactorBar:
    trade_date: str
    high: float
    low: float
    close: float
    volume: float
    turnover_pct: Optional[float] = None


@dataclass(frozen=True)
class FactorSnapshot:
    """一个盘中信号在当时能够使用的日线因子。"""

    signal_date: str
    completed_date: str
    values: dict[str, Optional[float]]

    def value(self, key: str) -> Optional[float]:
        return self.values.get(key)


def percentile_rank(sorted_values: Sequence[float], value: float) -> float:
    """返回平均名次百分位；最小唯一值为 ``1/N``，并列取平均名次。"""

    if not sorted_values:
        raise ValueError("横截面不能为空")
    left = bisect_left(sorted_values, value)
    right = bisect_right(sorted_values, value)
    if left == right:
        raise ValueError("待排名值不在横截面中")
    average_one_based_rank = (left + right + 1.0) / 2.0
    return average_one_based_rank / len(sorted_values)


def attach_cross_sectional_ranks(
    snapshot: FactorSnapshot,
    *,
    populations: dict[str, Sequence[float]],
) -> FactorSnapshot:
    """把预先排序的全市场RS和NATR横截面名次附加到快照。"""

    values = dict(snapshot.values)
    for lookback in RS_LOOKBACKS:
        for skip_recent in RS_SKIPS:
            source = rs_momentum_key(lookback, skip_recent)
            target = rs_rank_key(lookback, skip_recent)
            raw_value = values.get(source)
            population = populations.get(source, ())
            values[target] = (
                percentile_rank(population, raw_value)
                if raw_value is not None and population
                else None
            )
    for period in NATR_PERIODS:
        source = natr_key(period)
        target = natr_rank_key(period)
        raw_value = values.get(source)
        population = populations.get(source, ())
        values[target] = (
            percentile_rank(population, raw_value)
            if raw_value is not None and population
            else None
        )
    return replace(snapshot, values=values)


def _true_ranges(bars: Sequence[FactorBar]) -> list[float]:
    output: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            output.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        output.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return output


def _atr(
    bars: Sequence[FactorBar], period: int, true_ranges: Sequence[float]
) -> list[Optional[float]]:
    output: list[Optional[float]] = [None] * len(bars)
    if len(bars) <= period:
        return output
    value = sum(true_ranges[1 : period + 1]) / period
    output[period] = value
    for index in range(period + 1, len(bars)):
        value = (value * (period - 1) + true_ranges[index]) / period
        output[index] = value
    return output


def _adx(
    bars: Sequence[FactorBar], period: int, true_ranges: Sequence[float]
) -> list[Optional[float]]:
    output: list[Optional[float]] = [None] * len(bars)
    if len(bars) < 2 * period:
        return output

    plus_dm = [0.0] * len(bars)
    minus_dm = [0.0] * len(bars)
    for index in range(1, len(bars)):
        up = bars[index].high - bars[index - 1].high
        down = bars[index - 1].low - bars[index].low
        plus_dm[index] = up if up > down and up > 0 else 0.0
        minus_dm[index] = down if down > up and down > 0 else 0.0

    smoothed_tr = sum(true_ranges[1 : period + 1])
    smoothed_plus = sum(plus_dm[1 : period + 1])
    smoothed_minus = sum(minus_dm[1 : period + 1])
    dx: list[Optional[float]] = [None] * len(bars)

    def directional_index() -> float:
        if smoothed_tr == 0:
            return 0.0
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di
        if denominator == 0:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / denominator

    dx[period] = directional_index()
    for index in range(period + 1, len(bars)):
        smoothed_tr = (
            smoothed_tr - smoothed_tr / period + true_ranges[index]
        )
        smoothed_plus = (
            smoothed_plus - smoothed_plus / period + plus_dm[index]
        )
        smoothed_minus = (
            smoothed_minus - smoothed_minus / period + minus_dm[index]
        )
        dx[index] = directional_index()

    first_adx_index = 2 * period - 1
    value = sum(float(item) for item in dx[period : first_adx_index + 1]) / period
    output[first_adx_index] = value
    for index in range(first_adx_index + 1, len(bars)):
        value = (value * (period - 1) + float(dx[index])) / period
        output[index] = value
    return output


def _rsi(bars: Sequence[FactorBar], period: int) -> list[Optional[float]]:
    output: list[Optional[float]] = [None] * len(bars)
    if len(bars) <= period:
        return output
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(bars)):
        change = bars[index].close - bars[index - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value() -> float:
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + average_gain / average_loss)

    output[period] = value()
    for index in range(period + 1, len(bars)):
        average_gain = (
            average_gain * (period - 1) + gains[index - 1]
        ) / period
        average_loss = (
            average_loss * (period - 1) + losses[index - 1]
        ) / period
        output[index] = value()
    return output


def calculate_factor_snapshots(
    bars: Sequence[FactorBar],
    *,
    market_dates: Sequence[str],
    signal_dates: Sequence[str],
) -> dict[str, FactorSnapshot]:
    """计算信号时点快照，所有日线输入均在盘中信号前已完整。"""

    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError("因子日线日期必须严格递增且不能重复")
    market_index = {trade_date: index for index, trade_date in enumerate(market_dates)}
    bars_by_date = {bar.trade_date: bar for bar in bars}
    bar_index = {bar.trade_date: index for index, bar in enumerate(bars)}
    true_ranges = _true_ranges(bars)
    atr_values = {
        period: _atr(bars, period, true_ranges) for period in NATR_PERIODS
    }
    adx_values = {
        period: _adx(bars, period, true_ranges) for period in ADX_PERIODS
    }
    rsi_values = {period: _rsi(bars, period) for period in RSI_PERIODS}
    output: dict[str, FactorSnapshot] = {}

    for signal_date in signal_dates:
        signal_index = market_index.get(signal_date)
        if signal_index is None or signal_index == 0:
            continue
        completed_date = market_dates[signal_index - 1]
        completed_bar_index = bar_index.get(completed_date)
        values: dict[str, Optional[float]] = {}

        for lookback in RS_LOOKBACKS:
            for skip_recent in RS_SKIPS:
                start_index = signal_index - lookback
                # S=0不能使用尚未收盘的信号日，退到最近完整日线。
                end_index = signal_index - max(skip_recent, 1)
                start_bar = (
                    bars_by_date.get(market_dates[start_index])
                    if start_index >= 0
                    else None
                )
                end_bar = bars_by_date.get(market_dates[end_index])
                values[rs_momentum_key(lookback, skip_recent)] = (
                    end_bar.close / start_bar.close - 1.0
                    if start_bar is not None and end_bar is not None
                    else None
                )

        for lookback in RTOV_LOOKBACKS:
            current_bar = bars_by_date.get(completed_date)
            history = [
                bars_by_date.get(trade_date)
                for trade_date in market_dates[
                    signal_index - lookback - 1 : signal_index - 1
                ]
            ]
            historical_turnover = [
                item.turnover_pct
                for item in history
                if item is not None and item.turnover_pct is not None
            ]
            denominator = (
                median(historical_turnover)
                if len(historical_turnover) == lookback
                else None
            )
            values[rtov_key(lookback)] = (
                current_bar.turnover_pct / denominator
                if current_bar is not None
                and current_bar.turnover_pct is not None
                and denominator is not None
                and denominator > 0
                else None
            )

        if completed_bar_index is not None:
            for period in ADX_PERIODS:
                values[adx_key(period)] = adx_values[period][completed_bar_index]
                comparison_index = (
                    bar_index.get(market_dates[signal_index - 4])
                    if signal_index >= 4
                    else None
                )
                values[adx_comparison_key(period)] = (
                    adx_values[period][comparison_index]
                    if comparison_index is not None
                    else None
                )
            for period in NATR_PERIODS:
                atr = atr_values[period][completed_bar_index]
                values[natr_key(period)] = (
                    100.0 * atr / bars[completed_bar_index].close
                    if atr is not None
                    else None
                )
            for period in RSI_PERIODS:
                values[rsi_key(period)] = rsi_values[period][completed_bar_index]

        output[signal_date] = FactorSnapshot(
            signal_date=signal_date,
            completed_date=completed_date,
            values=values,
        )
    return output
