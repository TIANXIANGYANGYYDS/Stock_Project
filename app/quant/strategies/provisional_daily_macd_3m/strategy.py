"""正式策略的日线观察、盘中确认和固定账户配置。"""

from __future__ import annotations

from typing import Literal

from app.quant.core.models import BacktestConfig, IndicatorBar
from app.quant.strategies.provisional_daily_macd_3m.config import (
    COMMISSION_RATE,
    DAILY_MACD_PARAMETERS,
    DAILY_WARMUP_BARS,
    INITIAL_CASH_PER_STOCK,
    LOT_SIZE,
    MINIMUM_SHRINK_RATIO,
    SLIPPAGE_RATE,
    STAMP_DUTY_RATE,
)


ObservationAction = Literal["buy", "sell"]


def determine_observation_action(
    previous_previous: IndicatorBar,
    previous: IndicatorBar,
) -> ObservationAction | None:
    """用两根已完成日线决定当前交易日允许观察的方向。"""

    if (
        previous_previous.histogram < 0
        and previous.histogram < previous_previous.histogram
    ):
        return "buy"
    if (
        previous_previous.histogram > 0
        and previous.histogram > previous_previous.histogram
    ):
        return "sell"
    return None


def confirm_provisional_histogram(
    *,
    action: ObservationAction,
    reference_histogram: float,
    provisional_histogram: float,
) -> tuple[bool, float]:
    """判断临时柱体是否保持同侧并相对昨日缩短至少1%。"""

    if action == "buy":
        if reference_histogram >= 0:
            raise ValueError("买入观察基准必须是绿柱")
        ratio = (provisional_histogram - reference_histogram) / abs(
            reference_histogram
        )
        same_side = provisional_histogram < 0
    else:
        if reference_histogram <= 0:
            raise ValueError("卖出观察基准必须是红柱")
        ratio = (
            reference_histogram - provisional_histogram
        ) / reference_histogram
        same_side = provisional_histogram > 0
    return same_side and ratio >= MINIMUM_SHRINK_RATIO, ratio


def official_backtest_config(*, code: str) -> BacktestConfig:
    """生成正式策略固定资金、指标和交易成本配置。"""

    fast, slow, signal = DAILY_MACD_PARAMETERS
    return BacktestConfig(
        code=code,
        initial_cash=INITIAL_CASH_PER_STOCK,
        fast_period=fast,
        slow_period=slow,
        signal_period=signal,
        warmup_bars=DAILY_WARMUP_BARS,
        commission_rate=COMMISSION_RATE,
        stamp_duty_rate=STAMP_DUTY_RATE,
        slippage_rate=SLIPPAGE_RATE,
        lot_size=LOT_SIZE,
    )
