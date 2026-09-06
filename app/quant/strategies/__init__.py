"""量化模块唯一正式策略的公开入口。"""

from app.quant.strategies.provisional_daily_macd_3m import (
    STRATEGY_ID,
    STRATEGY_LABEL,
    STRATEGY_VERSION,
)


DEFAULT_STRATEGY = STRATEGY_ID
STRATEGIES = (STRATEGY_ID,)
STRATEGY_LABELS = {STRATEGY_ID: STRATEGY_LABEL}


__all__ = (
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "STRATEGY_ID",
    "STRATEGY_LABEL",
    "STRATEGY_LABELS",
    "STRATEGY_VERSION",
)
