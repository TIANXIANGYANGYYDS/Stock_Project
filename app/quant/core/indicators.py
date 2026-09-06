"""行情与 MACD 指标的输入校验和计算函数。"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from app.quant.core.models import BacktestConfig, Bar, IndicatorBar


def validate_config(config: BacktestConfig) -> None:
    """检查回测配置是否满足 MACD 和成交模拟的基本约束。

    Args:
        config: 待校验的回测配置。周期使用正整数语义；本金、整手股数和
            各项费率会直接影响后续买卖计算。

    Returns:
        无返回值。所有检查通过即表示配置可以继续用于指标和回测计算。

    Raises:
        ValueError: 快慢周期关系无效、信号周期或预热长度无效、本金或整手
            股数不为正，或者佣金、印花税、滑点不在 ``[0, 1)`` 范围内。
    """

    if not (0 < config.fast_period < config.slow_period):
        raise ValueError("MACD 周期必须满足 0 < fast_period < slow_period")
    if config.signal_period <= 0 or config.warmup_bars < 3:
        raise ValueError("signal_period 必须大于 0，warmup_bars 不能小于 3")
    if config.initial_cash <= 0 or config.lot_size <= 0:
        raise ValueError("本金和每手股数必须大于 0")
    rates = (
        config.commission_rate,
        config.stamp_duty_rate,
        config.slippage_rate,
    )
    if any(rate < 0 or rate >= 1 for rate in rates):
        raise ValueError("费用率和滑点必须在 [0, 1) 内")


def validate_bars(bars: Sequence[Bar]) -> None:
    """检查一只股票的日线序列能否安全用于指标和回测。

    Args:
        bars: 已按预期交易顺序提供的日线。函数会校验非空、日期严格递增且
            唯一、四个价格均为有限正数，以及最高价和最低价能覆盖开收盘价。

    Returns:
        无返回值。函数只读取行情，不会排序、修复或修改传入数据。

    Raises:
        ValueError: 日线为空、日期乱序或重复、价格无效，或者 OHLC 关系异常。
    """

    if not bars:
        raise ValueError("日线数据为空")
    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError("日线日期必须严格递增且不能重复")
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ValueError(f"日线价格异常: {bar}")
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise ValueError(f"日线 OHLC 关系异常: {bar}")


def calculate_macd(
    bars: Iterable[Bar], config: BacktestConfig
) -> list[IndicatorBar]:
    """按收盘价为每根日线计算 MACD(快线, 慢线, 信号线)。

    快慢 EMA 均以第一根日线收盘价初始化，第一根日线的 DIF 和 DEA 初始化
    为 0；此后使用 ``alpha = 2 / (period + 1)`` 递推。柱体采用国内行情软件
    常见定义 ``2 * (DIF - DEA)``，因此负值为绿柱、正值为红柱。

    Args:
        bars: 按交易日期升序排列的原始日线可迭代对象。该函数不自行校验
            日期和 OHLC，调用方应先执行 :func:`validate_bars`。
        config: 提供 ``fast_period``、``slow_period`` 和 ``signal_period`` 的
            回测配置。该函数不使用其中的本金和交易成本字段。

    Returns:
        与输入顺序和数量完全一致的指标日线列表；每项保留原 OHLC，并新增
        DIF、DEA 和 MACD 柱体。函数只计算指标，不产生买卖信号。
    """

    fast_ema: float | None = None
    slow_ema: float | None = None
    dea: float | None = None
    fast_alpha = 2.0 / (config.fast_period + 1.0)
    slow_alpha = 2.0 / (config.slow_period + 1.0)
    signal_alpha = 2.0 / (config.signal_period + 1.0)
    output: list[IndicatorBar] = []
    for bar in bars:
        # 第一根K线作为三条递推序列的共同起点，避免人为补造历史数据。
        if fast_ema is None:
            fast_ema = bar.close
            slow_ema = bar.close
            dif = 0.0
            dea = 0.0
        else:
            # 后续K线沿用上一日结果递推，保证与原回测的 MACD 口径一致。
            fast_ema += fast_alpha * (bar.close - fast_ema)
            slow_ema += slow_alpha * (bar.close - slow_ema)
            dif = fast_ema - slow_ema
            dea += signal_alpha * (dif - dea)
        output.append(
            IndicatorBar(
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                dif=dif,
                dea=dea,
                histogram=2.0 * (dif - dea),
            )
        )
    return output
