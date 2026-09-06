"""历史回放的前收盘参考价核验，不修改行情，也不用于实时兜底撮合。"""
from __future__ import annotations

import math
from typing import Any, Mapping


def recover_previous_close(
    *, daily: Mapping[str, Any] | None, previous_daily: Mapping[str, Any] | None,
    observed_close: float | None, previous_observed_close: float | None = None,
) -> tuple[float | None, str]:
    """从日线绝对涨跌额恢复开盘前已知参考价，并交叉核验价格单位。

    当日日线/末分钟只用于核验及恢复静态前收盘参考价，不进入该日的信号指标。
    无法核验、复权尺度不一致或前日收盘有矛盾时拒绝恢复，保持缺行情状态。
    """
    if daily is None or previous_daily is None or observed_close is None:
        return None, "missing_validation_input"
    try:
        close = float(daily["close"])
        change = float(daily["change_amount"])
        pct = float(daily["pct_chg"])
        previous = float(previous_daily["close"])
        observed = float(observed_close)
    except (KeyError, TypeError, ValueError):
        return None, "missing_daily_reference_fields"
    if not all(math.isfinite(value) for value in (close, change, pct, previous, observed)):
        return None, "invalid_price"
    reference = round(close - change, 2)
    if min(close, previous, observed, reference) <= 0:
        return None, "invalid_price"
    if abs(close - observed) > .005:
        return None, "adjusted_and_observed_close_disagree"
    if abs(reference - previous) > .005:
        return None, "previous_daily_reference_disagree"
    if previous_observed_close is not None and abs(reference - previous_observed_close) > .005:
        return None, "previous_observed_reference_disagree"
    if abs((close / reference - 1) * 100 - pct) > .015:
        return None, "reported_return_disagrees"
    return reference, "validated_daily_change_reference"


def validate_history_day(*, rows, observed_bars, trade_date):
    """历史整日必须有80个正确端点，且与原始采集的重叠价格多数一致。

    这是数据源尺度核验，不是收益或交易条件筛选。只接受原生行情聚合结果，
    不插值、不延展、不把不同来源的半日价格拼成一条路径。
    """
    from app.quant.runtime.live import three_minute_bar_ends
    expected = three_minute_bar_ends(trade_date)
    ordered = sorted(rows, key=lambda row: row['timestamp'])
    if [row['timestamp'] for row in ordered] != list(expected):
        return False, 'historical_day_incomplete'
    for row in ordered:
        if row.get('adjust') != 'qfq' or row.get('interval') != '3m':
            return False, 'historical_price_basis_unknown'
        prices = [float(row[key]) for key in ('open', 'high', 'low', 'close')]
        if not all(math.isfinite(p) and p > 0 for p in prices):
            return False, 'invalid_historical_prices'
        o, h, l, c = prices
        if h < max(o, c) or l > min(o, c):
            return False, 'invalid_historical_prices'
    by_time = {row['timestamp']: row for row in ordered}
    matches = sum(abs(bar.close - float(by_time[bar.end_at]['close'])) < .005 for bar in observed_bars)
    if matches < 3 or matches * 2 < len(observed_bars):
        return False, 'insufficient_price_basis_agreement'
    return True, 'validated_complete_historical_day'
