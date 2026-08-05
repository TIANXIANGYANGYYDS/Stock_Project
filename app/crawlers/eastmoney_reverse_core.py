from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Iterable


@dataclass(frozen=True)
class Kline:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    amplitude_pct: float
    pct_chg: float
    change_amount: float
    turnover_pct: float

    @classmethod
    def from_csv(cls, line: str) -> "Kline":
        values = line.split(",")
        if len(values) != 11:
            raise ValueError(f"unexpected EastMoney K-line width: {len(values)}")
        return cls(values[0], *(float(value) for value in values[1:]))


def _js_fixed(value: float, digits: int) -> float:
    quantum = Decimal(1).scaleb(-digits)
    with localcontext() as context:
        context.prec = 50
        return float(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))


def _js_precision_12(value: float) -> float:
    return float(format(value, ".12g"))


def _js_sum(values: Iterable[float]) -> float:
    total = 0.0
    for value in values:
        total += value
    return total


def _mean(rows: list[Kline], index: int, period: int, field: str) -> float:
    values = (getattr(row, field) for row in rows[index - period + 1 : index + 1])
    return _js_sum(values) / period


def calculate_indicators(rows: Iterable[Kline]) -> dict[str, dict[str, float]]:
    """Port the indicator branches used by quotechart2022 on the concept page."""

    klines = list(rows)
    output: dict[str, dict[str, float]] = {}
    fast = slow = dea = 0.0
    previous_k = previous_d = 0.0
    rsi_up: dict[int, float] = {}
    rsi_down: dict[int, float] = {}

    for index, row in enumerate(klines):
        values: dict[str, float] = {}

        # ma.ts and volume_ma.ts deliberately start one index later than the
        # mathematical minimum; retain that page behavior.
        for period in (5, 10, 20, 30, 60):
            if index >= period:
                values[f"ma{period}"] = _mean(klines, index, period, "close")
        for period in (5, 10):
            if index >= period:
                values[f"vol_ma{period}"] = _mean(klines, index, period, "volume")

        if index == 0:
            fast = slow = row.close
            dif = dea = 0.0
        else:
            fast = (2 * row.close + 11 * fast) / 13
            slow = (2 * row.close + 25 * slow) / 27
            dif = fast - slow
            dea = (2 * dif + 8 * dea) / 10
        values.update(
            macd_dif=_js_fixed(dif, 3),
            macd_dea=_js_fixed(dea, 3),
            macd_hist=_js_fixed(2 * (dif - dea), 3),
        )

        if index >= 19:
            closes = [item.close for item in reversed(klines[index - 19 : index + 1])]
            middle = _js_sum(closes) / 20
            deviation = math.sqrt(
                _js_sum((value - middle) ** 2 for value in closes) / 20
            )
            values.update(
                boll_mid=_js_fixed(middle, 3),
                boll_upper=_js_fixed(middle + 2 * deviation, 3),
                boll_lower=_js_fixed(middle - 2 * deviation, 3),
            )

        typical = (row.high + row.low + row.close) / 3
        if index >= 13:
            typicals = [
                (item.high + item.low + item.close) / 3
                for item in reversed(klines[index - 13 : index + 1])
            ]
            typical_mean = _js_sum(typicals) / 14
            mean_deviation = _js_sum(
                abs(value - typical_mean) for value in typicals
            ) / 14
            if mean_deviation != 0:
                values["cci14"] = _js_fixed(
                    (typical - typical_mean) / (0.015 * mean_deviation), 3
                )

        nine = klines[max(0, index - 8) : index + 1]
        lowest = min(item.low for item in nine)
        highest = max(item.high for item in nine)
        rsv = 50.0 if highest == lowest else (row.close - lowest) / (highest - lowest) * 100
        if index == 0:
            current_k = current_d = rsv
        else:
            current_k = rsv / 3 + previous_k * 2 / 3
            current_d = current_k / 3 + previous_d * 2 / 3
        current_j = current_k * 3 - current_d * 2
        previous_k, previous_d = current_k, current_d
        if index > 1:
            values.update(
                kdj_k=_js_fixed(current_k, 3),
                kdj_d=_js_fixed(current_d, 3),
                kdj_j=_js_fixed(current_j, 3),
            )

        if index > 0:
            change = row.close - klines[index - 1].close
            up = max(change, 0)
            down = abs(change)
            for period in (6, 12, 24):
                if index == 1:
                    rsi_up[period] = up
                    rsi_down[period] = down
                else:
                    rsi_up[period] = up + rsi_up[period] * (period - 1) / period
                    rsi_down[period] = down + rsi_down[period] * (period - 1) / period
            for period, threshold in ((6, 4), (12, 10), (24, 22)):
                if index > threshold and rsi_down[period] != 0:
                    values[f"rsi{period}"] = _js_fixed(
                        rsi_up[period] / rsi_down[period] * 100, 3
                    )

        for period, field in ((10, "wr10"), (6, "wr6")):
            window = klines[max(0, index - period + 1) : index + 1]
            lowest = min(item.low for item in window)
            highest = max(item.high for item in window)
            if highest != lowest:
                values[field] = _js_fixed(
                    100 * (highest - row.close) / (highest - lowest), 3
                )

        output[row.date] = values
    return output


def calculate_chip(rows: Iterable[Kline], index: int) -> dict[str, Any]:
    """Port CYQCalculator and the concept page's DOM/canvas serialization."""

    klines = list(rows)
    if index < 0 or index >= len(klines):
        raise IndexError("chip index is outside K-line data")
    source = klines[: index + 1]
    factor = 150
    maximum = max(row.high for row in source)
    minimum = min(row.low for row in source)
    accuracy = max(0.01, (maximum - minimum) / (factor - 1))
    y_values = [_js_fixed(minimum + accuracy * item, 2) for item in range(factor)]
    x_values = [0.0] * factor

    for row in source:
        average = (row.open + row.close + row.high + row.low) / 4
        turnover = min(1, row.turnover_pct / 100 if row.turnover_pct else 0)
        high_index = math.floor((row.high - minimum) / accuracy)
        low_index = math.ceil((row.low - minimum) / accuracy)
        peak = factor - 1 if row.high == row.low else 2 / (row.high - row.low)
        average_index = math.floor((average - minimum) / accuracy)
        x_values = [value * (1 - turnover) for value in x_values]
        if row.high == row.low:
            x_values[average_index] += peak * turnover / 2
            continue
        for item in range(low_index, high_index + 1):
            price = minimum + accuracy * item
            if price <= average:
                multiplier = peak if abs(average - row.low) < 1e-8 else (price - row.low) / (average - row.low) * peak
            else:
                multiplier = peak if abs(row.high - average) < 1e-8 else (row.high - price) / (row.high - average) * peak
            x_values[item] += multiplier * turnover

    rounded_x = [_js_precision_12(value) for value in x_values]
    total = _js_sum(rounded_x)

    def cost_at(chips: float) -> float:
        accumulated = 0.0
        for item, value in enumerate(rounded_x):
            if accumulated + value > chips:
                return minimum + item * accuracy
            accumulated += value
        return 0.0

    current = klines[index].close
    below = _js_sum(
        value
        for item, value in enumerate(rounded_x)
        if current >= minimum + item * accuracy
    )

    def cost_range(percent: float) -> dict[str, float]:
        low = cost_at(total * (1 - percent) / 2)
        high = cost_at(total * (1 + percent) / 2)
        concentration = 0.0 if low + high == 0 else (high - low) / (low + high)
        return {
            "low": _js_fixed(low, 2),
            "high": _js_fixed(high, 2),
            "concentration": _js_fixed(100 * concentration, 2) / 100,
        }

    maximum_x = max(x_values)
    chart_x = (
        [_js_fixed(value * 230 / maximum_x, 12) for value in x_values]
        if maximum_x > 0
        else []
    )
    return {
        "profit_ratio": _js_fixed(100 * (0 if total == 0 else below / total), 2) / 100,
        "avg_cost": _js_fixed(cost_at(total * 0.5), 2),
        "cost_90": cost_range(0.9),
        "cost_70": cost_range(0.7),
        "chart": {"x": chart_x, "y": [_js_fixed(value, 4) for value in y_values]},
    }
