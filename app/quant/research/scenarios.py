"""冻结的第一轮辅助指标网格，共66个单因子场景。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Tuple

from app.quant.research.factors import (
    adx_comparison_key,
    adx_key,
    natr_rank_key,
    rs_rank_key,
    rsi_key,
    rtov_key,
)


AUXILIARY_INDICATOR_GRID = {
    "rs_rank": {
        "lookback": (20, 60, 120),
        "skip_recent": (0, 5),
        "min_percentile": (0.60, 0.70, 0.80),
    },
    "relative_turnover": {
        "lookback": (10, 20, 40),
        "rules": (
            (0.8, None),
            (1.0, None),
            (1.2, None),
            (1.0, 3.0),
        ),
    },
    "adx": {
        "period": (7, 14, 21),
        "rules": (
            "adx_lt_20",
            "adx_lt_25",
            "adx_ge_20_and_falling_3d",
            "adx_ge_20_and_rising_3d",
        ),
    },
    "natr_rank": {
        "period": (10, 14, 20),
        "percentile_bands": (
            (0.10, 0.90),
            (0.20, 0.90),
            (0.20, 0.80),
            (0.30, 0.80),
        ),
    },
    "rsi_control": {
        "period": (9, 14, 21),
        "value_bands": (
            (35.0, 55.0),
            (40.0, 60.0),
            (45.0, 65.0),
            (50.0, 70.0),
        ),
    },
}


@dataclass(frozen=True)
class FactorScenario:
    key: str
    label: str
    factor: str
    required_fields: Tuple[str, ...]
    parameters: Tuple[Tuple[str, Any], ...]
    coordinates: Tuple[int, ...]
    predicate: Callable[["FactorScenario", Any], bool]

    def is_available(self, snapshot: Any) -> bool:
        return snapshot is not None and all(
            snapshot.value(field) is not None for field in self.required_fields
        )

    def accepts(self, snapshot: Any) -> bool:
        return self.is_available(snapshot) and self.predicate(self, snapshot)

    def parameter(self, name: str) -> Any:
        return dict(self.parameters)[name]


def _minimum(scenario: FactorScenario, snapshot: Any) -> bool:
    value = float(snapshot.value(scenario.required_fields[0]))
    return value >= float(scenario.parameter("minimum"))


def _band(scenario: FactorScenario, snapshot: Any) -> bool:
    value = float(snapshot.value(scenario.required_fields[0]))
    return (
        float(scenario.parameter("minimum"))
        <= value
        <= float(scenario.parameter("maximum"))
    )


def _rtov(scenario: FactorScenario, snapshot: Any) -> bool:
    value = float(snapshot.value(scenario.required_fields[0]))
    minimum = float(scenario.parameter("minimum"))
    maximum = scenario.parameter("maximum")
    return value >= minimum and (maximum is None or value <= float(maximum))


def _adx(scenario: FactorScenario, snapshot: Any) -> bool:
    current = float(snapshot.value(scenario.required_fields[0]))
    rule = scenario.parameter("rule")
    if rule == "adx_lt_20":
        return current < 20.0
    if rule == "adx_lt_25":
        return current < 25.0
    previous = float(snapshot.value(scenario.required_fields[1]))
    if rule == "adx_ge_20_and_falling_3d":
        return current >= 20.0 and current < previous
    return current >= 20.0 and current > previous


def build_factor_scenarios() -> Tuple[FactorScenario, ...]:
    scenarios: list[FactorScenario] = []
    rs_grid = AUXILIARY_INDICATOR_GRID["rs_rank"]
    for l_index, lookback in enumerate(rs_grid["lookback"]):
        for s_index, skip_recent in enumerate(rs_grid["skip_recent"]):
            for q_index, threshold in enumerate(rs_grid["min_percentile"]):
                scenarios.append(
                    FactorScenario(
                        key=f"rs_l{lookback}_s{skip_recent}_q{int(threshold * 100)}",
                        label=(
                            f"RSRank(L={lookback},S={skip_recent}) >= "
                            f"{threshold:.0%}"
                        ),
                        factor="RSRank",
                        required_fields=(rs_rank_key(lookback, skip_recent),),
                        parameters=(
                            ("lookback", lookback),
                            ("skip_recent", skip_recent),
                            ("minimum", threshold),
                        ),
                        coordinates=(l_index, s_index, q_index),
                        predicate=_minimum,
                    )
                )

    rtov_grid = AUXILIARY_INDICATOR_GRID["relative_turnover"]
    for n_index, lookback in enumerate(rtov_grid["lookback"]):
        for rule_index, (minimum, maximum) in enumerate(rtov_grid["rules"]):
            rule_key = (
                f"ge_{int(minimum * 10):02d}"
                if maximum is None
                else f"{int(minimum * 10):02d}_{int(maximum * 10):02d}"
            )
            rule_label = (
                f">= {minimum:.1f}"
                if maximum is None
                else f"[{minimum:.1f}, {maximum:.1f}]"
            )
            scenarios.append(
                FactorScenario(
                    key=f"rtov_n{lookback}_{rule_key}",
                    label=f"RTOV{lookback} {rule_label}",
                    factor="RTOV",
                    required_fields=(rtov_key(lookback),),
                    parameters=(
                        ("lookback", lookback),
                        ("minimum", minimum),
                        ("maximum", maximum),
                    ),
                    coordinates=(n_index, rule_index),
                    predicate=_rtov,
                )
            )

    adx_grid = AUXILIARY_INDICATOR_GRID["adx"]
    for p_index, period in enumerate(adx_grid["period"]):
        for rule_index, rule in enumerate(adx_grid["rules"]):
            comparison = ""
            if "falling" in rule:
                comparison = "且较3日前下降"
            elif "rising" in rule:
                comparison = "且较3日前上升"
            threshold = "<20" if rule == "adx_lt_20" else "<25"
            if comparison:
                threshold = f">=20{comparison}"
            required = (adx_key(period),)
            if comparison:
                required += (adx_comparison_key(period),)
            scenarios.append(
                FactorScenario(
                    key=f"adx_n{period}_{rule[4:] if rule.startswith('adx_') else rule}",
                    label=f"ADX{period} {threshold}",
                    factor="ADX",
                    required_fields=required,
                    parameters=(("period", period), ("rule", rule)),
                    coordinates=(p_index, rule_index),
                    predicate=_adx,
                )
            )

    natr_grid = AUXILIARY_INDICATOR_GRID["natr_rank"]
    for p_index, period in enumerate(natr_grid["period"]):
        for band_index, (minimum, maximum) in enumerate(
            natr_grid["percentile_bands"]
        ):
            scenarios.append(
                FactorScenario(
                    key=(
                        f"natr_n{period}_q{int(minimum * 100)}_"
                        f"{int(maximum * 100)}"
                    ),
                    label=(
                        f"NATR{period} Rank [{minimum:.0%}, {maximum:.0%}]"
                    ),
                    factor="NATR",
                    required_fields=(natr_rank_key(period),),
                    parameters=(
                        ("period", period),
                        ("minimum", minimum),
                        ("maximum", maximum),
                    ),
                    coordinates=(p_index, band_index),
                    predicate=_band,
                )
            )

    rsi_grid = AUXILIARY_INDICATOR_GRID["rsi_control"]
    for p_index, period in enumerate(rsi_grid["period"]):
        for band_index, (minimum, maximum) in enumerate(rsi_grid["value_bands"]):
            scenarios.append(
                FactorScenario(
                    key=f"rsi_n{period}_{int(minimum)}_{int(maximum)}",
                    label=f"RSI{period} [{minimum:.0f}, {maximum:.0f}]（对照）",
                    factor="RSI对照",
                    required_fields=(rsi_key(period),),
                    parameters=(
                        ("period", period),
                        ("minimum", minimum),
                        ("maximum", maximum),
                    ),
                    coordinates=(p_index, band_index),
                    predicate=_band,
                )
            )
    return tuple(scenarios)


def are_grid_neighbors(first: FactorScenario, second: FactorScenario) -> bool:
    """判断两个同族场景是否只相差一个相邻网格坐标。"""

    if first.factor != second.factor or len(first.coordinates) != len(
        second.coordinates
    ):
        return False
    changed_dimensions = [
        index
        for index, (left, right) in enumerate(
            zip(first.coordinates, second.coordinates)
        )
        if left != right
    ]
    if len(changed_dimensions) != 1:
        return False
    changed = changed_dimensions[0]

    if first.factor == "ADX" and changed == 1:
        return {
            first.parameter("rule"),
            second.parameter("rule"),
        } == {"adx_lt_20", "adx_lt_25"}

    if first.factor == "RTOV" and changed == 1:
        first_minimum = float(first.parameter("minimum"))
        second_minimum = float(second.parameter("minimum"))
        first_maximum = first.parameter("maximum")
        second_maximum = second.parameter("maximum")
        if first_maximum is None and second_maximum is None:
            return round(abs(first_minimum - second_minimum), 10) == 0.2
        return (
            first_minimum == second_minimum == 1.0
            and {first_maximum, second_maximum} == {None, 3.0}
        )

    return (
        abs(first.coordinates[changed] - second.coordinates[changed]) == 1
    )


FACTOR_SCENARIOS = build_factor_scenarios()
CORE_FACTOR_SCENARIOS = tuple(
    item for item in FACTOR_SCENARIOS if item.factor != "RSI对照"
)
RSI_CONTROL_SCENARIOS = tuple(
    item for item in FACTOR_SCENARIOS if item.factor == "RSI对照"
)

if len(CORE_FACTOR_SCENARIOS) != 54:
    raise RuntimeError("第一轮核心辅助指标网格必须固定为54组")
if len(RSI_CONTROL_SCENARIOS) != 12:
    raise RuntimeError("第一轮RSI冗余对照必须固定为12组")
if len(FACTOR_SCENARIOS) != 66 or len({item.key for item in FACTOR_SCENARIOS}) != 66:
    raise RuntimeError("第一轮辅助指标场景必须是66个唯一场景")


# 第二轮只复用第一轮的11条规则，不新增公式、阈值或因子组合。
PURIFICATION_SCENARIO_KEYS = (
    "adx_n7_ge_20_and_rising_3d",
    "adx_n14_ge_20_and_rising_3d",
    "adx_n21_ge_20_and_rising_3d",
    "rtov_n10_ge_10",
    "rtov_n10_ge_12",
    "rtov_n20_ge_10",
    "rtov_n20_ge_12",
    "rtov_n40_ge_10",
    "rtov_n40_ge_12",
    "rs_l120_s0_q80",
    "rs_l120_s5_q80",
)
PURIFICATION_SCENARIOS = tuple(
    next(item for item in FACTOR_SCENARIOS if item.key == key)
    for key in PURIFICATION_SCENARIO_KEYS
)

ADX_COMPARISON_SCENARIOS = tuple(
    next(item for item in FACTOR_SCENARIOS if item.key == key)
    for key in (
        "adx_n21_ge_20_and_rising_3d",
        "adx_n14_ge_20_and_rising_3d",
        "adx_n7_ge_20_and_rising_3d",
        "rtov_n10_ge_10",
    )
)
