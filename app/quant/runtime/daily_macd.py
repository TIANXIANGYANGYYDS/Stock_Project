"""在分钟收盘时计算当天尚未完成的临时日线 MACD。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.quant.core.indicators import calculate_macd
from app.quant.core.models import BacktestConfig, Bar, IndicatorBar


@dataclass(frozen=True)
class DailyMacdState:
    """一根已完成日线收盘后的 MACD 递推状态。"""

    fast_ema: float
    slow_ema: float
    dea: float


def calculate_daily_macd_states(
    bars: Sequence[Bar], config: BacktestConfig
) -> list[DailyMacdState]:
    """计算每根完整日线收盘后的 EMA 状态，供盘中临时日线复用。"""

    fast_alpha = 2.0 / (config.fast_period + 1.0)
    slow_alpha = 2.0 / (config.slow_period + 1.0)
    signal_alpha = 2.0 / (config.signal_period + 1.0)
    fast_ema: float | None = None
    slow_ema: float | None = None
    dea: float | None = None
    states: list[DailyMacdState] = []
    for bar in bars:
        if fast_ema is None:
            fast_ema = bar.close
            slow_ema = bar.close
            dea = 0.0
        else:
            fast_ema += fast_alpha * (bar.close - fast_ema)
            slow_ema += slow_alpha * (bar.close - slow_ema)
            dif = fast_ema - slow_ema
            dea += signal_alpha * (dif - dea)
        states.append(DailyMacdState(fast_ema, slow_ema, dea))
    return states


def provisional_daily_indicator(
    completed_daily_bars: Sequence[Bar],
    *,
    trade_date: str,
    day_open: float,
    high_so_far: float,
    low_so_far: float,
    current_close: float,
    config: BacktestConfig,
) -> IndicatorBar:
    """把当前分钟收盘价作为今日收盘价，独立试算一根临时日线。

    每次试算都只在上一交易日的完整日线状态后追加一根临时日线，不会把
    一天内的多个分钟收盘误当成多根日线递推，因此不会加速 EMA。
    """

    if not completed_daily_bars:
        raise ValueError("临时日线 MACD 至少需要一根已完成日线")
    if completed_daily_bars[-1].trade_date >= trade_date:
        raise ValueError("临时日线日期必须晚于所有已完成日线")
    partial_bar = Bar(
        trade_date=trade_date,
        open=day_open,
        high=high_so_far,
        low=low_so_far,
        close=current_close,
    )
    return calculate_macd((*completed_daily_bars, partial_bar), config)[-1]


def provisional_daily_indicator_from_state(
    previous_state: DailyMacdState,
    *,
    trade_date: str,
    day_open: float,
    high_so_far: float,
    low_so_far: float,
    current_close: float,
    config: BacktestConfig,
) -> IndicatorBar:
    """从上一完整日线的冻结状态试算当天一根临时日线。"""

    fast_alpha = 2.0 / (config.fast_period + 1.0)
    slow_alpha = 2.0 / (config.slow_period + 1.0)
    signal_alpha = 2.0 / (config.signal_period + 1.0)
    fast_ema = previous_state.fast_ema + fast_alpha * (
        current_close - previous_state.fast_ema
    )
    slow_ema = previous_state.slow_ema + slow_alpha * (
        current_close - previous_state.slow_ema
    )
    dif = fast_ema - slow_ema
    dea = previous_state.dea + signal_alpha * (dif - previous_state.dea)
    return IndicatorBar(
        trade_date=trade_date,
        open=day_open,
        high=high_so_far,
        low=low_so_far,
        close=current_close,
        dif=dif,
        dea=dea,
        histogram=2.0 * (dif - dea),
    )
