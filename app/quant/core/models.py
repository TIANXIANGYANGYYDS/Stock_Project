"""正式策略共享的配置、行情和指标数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestConfig:
    """单股票独立账户使用的固定指标和撮合配置。"""

    code: str = "600176"
    initial_cash: float = 100_000.0
    fast_period: int = 20
    slow_period: int = 100
    signal_period: int = 30
    warmup_bars: int = 130
    commission_rate: float = 0.0001
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0005
    lot_size: int = 100


@dataclass(frozen=True)
class Bar:
    """一根日线或三分钟K线的前复权 OHLC 行情。"""

    trade_date: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class IndicatorBar:
    """附加日线 MACD 数值的行情。"""

    trade_date: str
    open: float
    high: float
    low: float
    close: float
    dif: float
    dea: float
    histogram: float
