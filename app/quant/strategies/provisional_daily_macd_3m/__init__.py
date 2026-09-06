"""公开第一个确定性量化策略的稳定定义。"""

from app.quant.strategies.provisional_daily_macd_3m.config import (
    COMMISSION_RATE,
    CONFIRMATION_BARS,
    DAILY_MACD_PARAMETERS,
    DAILY_WARMUP_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    INITIAL_CASH_PER_STOCK,
    INTRADAY_INTERVAL,
    LOT_SIZE,
    MINIMUM_SHRINK_RATIO,
    SLIPPAGE_RATE,
    STAMP_DUTY_RATE,
    STRATEGY_ID,
    STRATEGY_LABEL,
    STRATEGY_VERSION,
)
from app.quant.strategies.provisional_daily_macd_3m.strategy import (
    confirm_provisional_histogram,
    determine_observation_action,
    official_backtest_config,
)

__all__ = (
    "COMMISSION_RATE",
    "CONFIRMATION_BARS",
    "DAILY_MACD_PARAMETERS",
    "DAILY_WARMUP_BARS",
    "EXPECTED_INTRADAY_BARS_PER_DAY",
    "INITIAL_CASH_PER_STOCK",
    "INTRADAY_INTERVAL",
    "LOT_SIZE",
    "MINIMUM_SHRINK_RATIO",
    "SLIPPAGE_RATE",
    "STAMP_DUTY_RATE",
    "STRATEGY_ID",
    "STRATEGY_LABEL",
    "STRATEGY_VERSION",
    "confirm_provisional_histogram",
    "determine_observation_action",
    "official_backtest_config",
)
