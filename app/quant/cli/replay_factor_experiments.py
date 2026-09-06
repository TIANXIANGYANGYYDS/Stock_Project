"""运行67组首轮实验或12组独立账户买点提纯复核。"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from pymongo import ASCENDING, MongoClient

from app.core.config import PROJECT_ROOT, get_settings
from app.quant.cli.replay_sample import _market_dates, _sample_universe, sample_stocks
from app.quant.cli.replay_stock import _load_daily_documents, _load_minute_bars, replay
from app.quant.core.execution import money
from app.quant.core.models import Bar
from app.quant.data.market_data import (
    DAILY_HISTORY_COLLECTION,
    DEFAULT_ADJUST,
    THREE_MINUTE_HISTORY_COLLECTION,
)
from app.quant.research.evaluation import (
    MIN_FOLD_CLOSED_TRADES,
    MIN_OOS_CLOSED_TRADES,
    PLATFORM_EXPECTANCY_SIMILARITY,
    RANDOM_CONTROL_REQUIRED_PERCENTILE,
    RISK_WORSENING_TOLERANCE,
    fold_metrics,
    matched_random_control,
    portfolio_metrics,
    signal_distribution,
    trade_metrics,
)
from app.quant.research.factors import (
    NATR_PERIODS,
    RS_LOOKBACKS,
    RS_SKIPS,
    FactorBar,
    FactorSnapshot,
    attach_cross_sectional_ranks,
    calculate_factor_snapshots,
    natr_key,
    rs_momentum_key,
)
from app.quant.research.scenarios import (
    FACTOR_SCENARIOS,
    PURIFICATION_SCENARIOS,
    FactorScenario,
    are_grid_neighbors,
)
from app.quant.research.purification import (
    account_result,
    baseline_trade_diagnostics,
    paired_account_results,
    parameter_comparisons,
    render_purification_report,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    official_backtest_config,
)


DEFAULT_SAMPLE_SIZE = 300
DEFAULT_RANDOM_SEED = 20260903
FACTOR_HISTORY_TRADING_DAYS = 150
TURNOVER_COLLECTION = "stock_daily_detail"
INDUSTRY_FILE = (
    PROJECT_ROOT
    / "app/manually_execute_script/data/a_stock_ths_industry_boards.json"
)
BOARD_FILE = PROJECT_ROOT / "app/manually_execute_script/data/a_stock_ths_boards.json"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / ".local/quant" / STRATEGY_ID / "factor_experiments"
)


def _always(_: FactorScenario, __: FactorSnapshot) -> bool:
    return True


BASELINE = FactorScenario(
    key="baseline",
    label="MACD基线（无辅助指标）",
    factor="基线",
    required_fields=(),
    parameters=(),
    coordinates=(),
    predicate=_always,
)


@dataclass
class ScenarioAccumulator:
    scenario: FactorScenario
    market_dates: Sequence[str]
    daily_assets: dict[str, float] = field(init=False)
    final_assets: float = 0.0
    raw_buy_signal_count: int = 0
    available_signal_count: int = 0
    accepted_signal_count: int = 0
    filled_buy_count: int = 0
    accepted_by_date: Counter[str] = field(default_factory=Counter)
    buy_event_ids: set[tuple[str, str]] = field(default_factory=set)
    accepted_signals: list[dict[str, str]] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    signal_decisions: list[dict[str, Any]] = field(default_factory=list)
    stock_results: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.daily_assets = {trade_date: 0.0 for trade_date in self.market_dates}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行MACD单因子研究：grid67首轮或purification12买点提纯。"
    )
    parser.add_argument("--grid", choices=("grid67", "purification12"), default="grid67")
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not materialized:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(materialized[0])
    known_fields = set(fieldnames)
    for row in materialized[1:]:
        for key in row:
            if key not in known_fields:
                fieldnames.append(key)
                known_fields.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(materialized)


def _factor_calendar(
    collection: Any, *, start_date: str, end_date: str
) -> tuple[list[str], list[str]]:
    floor = (date.fromisoformat(start_date) - timedelta(days=600)).isoformat()
    dates = sorted(
        str(item)
        for item in collection.distinct(
            "trade_date",
            {
                "adjust": DEFAULT_ADJUST,
                "trade_date": {"$gte": floor, "$lte": end_date},
            },
        )
    )
    if start_date not in dates or end_date not in dates:
        raise ValueError("start-date和end-date必须都是市场交易日")
    start_index = dates.index(start_date)
    if start_index < max(RS_LOOKBACKS):
        raise ValueError("回放开始日前不足120个市场交易日，无法计算冻结RS网格")
    history_start = max(0, start_index - FACTOR_HISTORY_TRADING_DAYS)
    factor_dates = dates[history_start : dates.index(end_date) + 1]
    signal_dates = [item for item in dates if start_date <= item <= end_date]
    return factor_dates, signal_dates


def _load_turnover(
    collection: Any,
    *,
    codes: Sequence[str],
    start_date: str,
    end_date: str,
) -> dict[tuple[str, str], float]:
    cursor = collection.find(
        {
            "code": {"$in": list(codes)},
            "adjust": DEFAULT_ADJUST,
            "trade_date": {"$gte": start_date, "$lte": end_date},
            "turnover_pct": {"$ne": None},
        },
        {"_id": 0, "code": 1, "trade_date": 1, "turnover_pct": 1},
    ).batch_size(2_000)
    return {
        (str(item["code"]), str(item["trade_date"])): float(item["turnover_pct"])
        for item in cursor
    }


def _rank_source_keys() -> tuple[str, ...]:
    return tuple(
        rs_momentum_key(lookback, skip_recent)
        for lookback in RS_LOOKBACKS
        for skip_recent in RS_SKIPS
    ) + tuple(natr_key(period) for period in NATR_PERIODS)


def _load_ranked_factor_snapshots(
    collection: Any,
    *,
    sample_codes: set[str],
    factor_dates: Sequence[str],
    signal_dates: Sequence[str],
    turnover: dict[tuple[str, str], float],
) -> tuple[
    dict[str, dict[str, FactorSnapshot]],
    dict[str, dict[str, int]],
]:
    rank_sources = _rank_source_keys()
    populations: dict[str, dict[str, list[float]]] = {
        trade_date: {key: [] for key in rank_sources}
        for trade_date in signal_dates
    }
    sample_snapshots: dict[str, dict[str, FactorSnapshot]] = {}

    def consume(code: str, raw_bars: list[FactorBar]) -> None:
        bars = raw_bars
        if code in sample_codes:
            bars = [
                replace(
                    item,
                    turnover_pct=turnover.get((code, item.trade_date)),
                )
                for item in raw_bars
            ]
        snapshots = calculate_factor_snapshots(
            bars,
            market_dates=factor_dates,
            signal_dates=signal_dates,
        )
        for trade_date, snapshot in snapshots.items():
            for key in rank_sources:
                value = snapshot.value(key)
                if value is not None:
                    populations[trade_date][key].append(value)
        if code in sample_codes:
            sample_snapshots[code] = snapshots

    cursor = collection.find(
        {
            "adjust": DEFAULT_ADJUST,
            "trade_date": {"$gte": factor_dates[0], "$lte": factor_dates[-1]},
        },
        {
            "_id": 0,
            "code": 1,
            "trade_date": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 1,
        },
    ).sort([("code", ASCENDING), ("trade_date", ASCENDING)]).batch_size(2_000)
    current_code = ""
    current_bars: list[FactorBar] = []
    for item in cursor:
        code = str(item["code"])
        if current_code and code != current_code:
            consume(current_code, current_bars)
            current_bars = []
        current_code = code
        current_bars.append(
            FactorBar(
                trade_date=str(item["trade_date"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item.get("volume") or 0.0),
            )
        )
    if current_code:
        consume(current_code, current_bars)

    sorted_populations = {
        trade_date: {
            key: sorted(values) for key, values in by_key.items()
        }
        for trade_date, by_key in populations.items()
    }
    ranked: dict[str, dict[str, FactorSnapshot]] = {}
    for code, snapshots in sample_snapshots.items():
        ranked[code] = {
            trade_date: attach_cross_sectional_ranks(
                snapshot,
                populations=sorted_populations[trade_date],
            )
            for trade_date, snapshot in snapshots.items()
        }
    population_counts = {
        trade_date: {
            key: len(values) for key, values in by_key.items()
        }
        for trade_date, by_key in sorted_populations.items()
    }
    return ranked, population_counts


def _add_result(
    accumulator: ScenarioAccumulator,
    *,
    code: str,
    name: str,
    result: dict[str, Any],
    snapshots: dict[str, FactorSnapshot],
    initial_cash: float,
    store_decisions: bool,
) -> None:
    summary = result["summary"]
    accumulator.stock_results.append(account_result(
        code=code, name=name, initial_cash=initial_cash, result=result
    ))
    accumulator.final_assets += float(summary["final_assets"])
    accumulator.filled_buy_count += int(summary["filled_buy_count"])
    by_date = {str(row["trade_date"]): row for row in result["daily_rows"]}
    assets = initial_cash
    for trade_date in accumulator.market_dates:
        if trade_date in by_date:
            assets = float(by_date[trade_date]["total_assets"])
        accumulator.daily_assets[trade_date] += assets

    for signal in result["signal_rows"]:
        if signal["action"] != "buy":
            continue
        signal_date = str(signal["signal_at"])[:10]
        snapshot = snapshots.get(signal_date)
        available = (
            accumulator.scenario.key == "baseline"
            or accumulator.scenario.is_available(snapshot)
        )
        accepted = signal["final_status"] != "rejected_factor"
        accumulator.raw_buy_signal_count += 1
        accumulator.available_signal_count += available
        accumulator.accepted_signal_count += accepted
        if accepted:
            accumulator.accepted_by_date[signal_date] += 1
            accumulator.accepted_signals.append(
                {"code": code, "name": name, "signal_date": signal_date}
            )
        if store_decisions:
            factor_values = {
                field: snapshot.value(field) if snapshot else None
                for field in accumulator.scenario.required_fields
            }
            accumulator.signal_decisions.append(
                {
                    "scenario": accumulator.scenario.key,
                    "scenario_label": accumulator.scenario.label,
                    "factor": accumulator.scenario.factor,
                    "code": code,
                    "name": name,
                    "signal_at": signal["signal_at"],
                    "factor_available": available,
                    "accepted": accepted,
                    "final_status": signal["final_status"],
                    "factor_values": json.dumps(
                        factor_values, ensure_ascii=False, sort_keys=True
                    ),
                }
            )

    accumulator.buy_event_ids.update(
        (code, str(event["signal_at"]))
        for event in result["event_rows"]
        if event["action"] == "buy"
    )
    accumulator.closed_trades.extend(
        {
            "scenario": accumulator.scenario.key,
            "scenario_label": accumulator.scenario.label,
            **row,
        }
        for row in result["closed_trade_rows"]
    )


def _effective_replay_start(
    daily_bars: Sequence[Bar], *, requested_start: str, end_date: str
) -> Optional[str]:
    """返回具备两根前置日线后的首个可回放日期。"""

    first_requested_index = next(
        (
            index
            for index, bar in enumerate(daily_bars)
            if bar.trade_date >= requested_start
        ),
        None,
    )
    if first_requested_index is None:
        return None
    effective_index = max(first_requested_index, 2)
    if effective_index >= len(daily_bars):
        return None
    effective_start = daily_bars[effective_index].trade_date
    return effective_start if effective_start <= end_date else None


def _add_flat_account(
    accumulator: ScenarioAccumulator, *, initial_cash: float, code: str, name: str
) -> None:
    accumulator.stock_results.append(account_result(
        code=code, name=name, initial_cash=initial_cash, result=None
    ))
    accumulator.final_assets += initial_cash
    for trade_date in accumulator.market_dates:
        accumulator.daily_assets[trade_date] += initial_cash


def _direct_filtered_counts(
    scenario: FactorScenario,
    *,
    baseline_signals: Sequence[dict[str, str]],
    snapshots: dict[str, dict[str, FactorSnapshot]],
    market_dates: Sequence[str],
) -> tuple[list[int], int, int]:
    by_date: Counter[str] = Counter()
    available = 0
    for item in baseline_signals:
        snapshot = snapshots.get(item["code"], {}).get(item["signal_date"])
        is_available = scenario.key == "baseline" or scenario.is_available(snapshot)
        accepted = scenario.key == "baseline" or scenario.accepts(snapshot)
        available += is_available
        if accepted:
            by_date[item["signal_date"]] += 1
    counts = [by_date[trade_date] for trade_date in market_dates]
    return counts, available, sum(counts)


def _scenario_metrics(
    accumulator: ScenarioAccumulator,
    *,
    baseline: ScenarioAccumulator,
    snapshots: dict[str, dict[str, FactorSnapshot]],
    market_dates: Sequence[str],
    capital_base: float,
    baseline_top_winners: set[tuple[str, str]],
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    original_counts = [
        baseline.accepted_by_date[trade_date] for trade_date in market_dates
    ]
    filtered_counts, direct_available, direct_accepted = _direct_filtered_counts(
        accumulator.scenario,
        baseline_signals=baseline.accepted_signals,
        snapshots=snapshots,
        market_dates=market_dates,
    )
    metrics = {
        "scenario": accumulator.scenario.key,
        "scenario_label": accumulator.scenario.label,
        "factor": accumulator.scenario.factor,
        "parameters": json.dumps(
            dict(accumulator.scenario.parameters),
            ensure_ascii=False,
            sort_keys=True,
        ),
        **signal_distribution(original_counts, filtered_counts),
        **trade_metrics(accumulator.closed_trades),
        **portfolio_metrics(
            [money(accumulator.daily_assets[item]) for item in market_dates],
            capital_base=capital_base,
        ),
    }
    metrics.update(
        {
            "factor_available_baseline_signals": direct_available,
            "factor_coverage_rate": (
                direct_available / len(baseline.accepted_signals)
                if baseline.accepted_signals
                else None
            ),
            "path_raw_buy_signals": accumulator.raw_buy_signal_count,
            "path_accepted_buy_signals": accumulator.accepted_signal_count,
            "path_filled_buys": accumulator.filled_buy_count,
        }
    )
    retained_winners = len(accumulator.buy_event_ids & baseline_top_winners)
    winner_retention_rate = (
        retained_winners / len(baseline_top_winners)
        if baseline_top_winners
        else None
    )
    metrics.update(
        {
            "baseline_top_5pct_winner_count": len(baseline_top_winners),
            "baseline_top_5pct_winner_retained_count": retained_winners,
            "baseline_top_5pct_winner_retention_rate": winner_retention_rate,
            "winner_retention_minus_random_expectation": (
                winner_retention_rate - metrics["signal_retention_rate"]
                if winner_retention_rate is not None
                and metrics["signal_retention_rate"] is not None
                else None
            ),
        }
    )
    random_seed = seed + sum(
        (index + 1) * ord(character)
        for index, character in enumerate(accumulator.scenario.key)
    )
    metrics.update(
        matched_random_control(
            baseline_trades=baseline.closed_trades,
            candidate_trades=accumulator.closed_trades,
            seed=random_seed,
        )
    )
    folds = fold_metrics(
        market_dates=market_dates,
        assets_by_date={
            item: money(accumulator.daily_assets[item]) for item in market_dates
        },
        trades=accumulator.closed_trades,
        capital_base=capital_base,
    )
    for fold in folds:
        fold["scenario"] = accumulator.scenario.key
        fold["scenario_label"] = accumulator.scenario.label
        fold["cost_mode"] = "normal"
    return metrics, folds


def _add_fold_comparisons(
    metrics: dict[str, Any],
    folds: Sequence[dict[str, Any]],
    baseline_folds: Sequence[dict[str, Any]],
    *,
    prefix: str = "",
) -> None:
    valid_fold_count = 0
    positive_fold_count = 0
    for candidate, baseline in zip(folds, baseline_folds):
        valid = (
            candidate["trade_count"] >= MIN_FOLD_CLOSED_TRADES
            and baseline["trade_count"] >= MIN_FOLD_CLOSED_TRADES
            and candidate["net_expectancy"] is not None
            and baseline["net_expectancy"] is not None
        )
        delta = (
            float(candidate["net_expectancy"])
            - float(baseline["net_expectancy"])
            if valid
            else None
        )
        candidate[f"{prefix}baseline_net_expectancy"] = baseline["net_expectancy"]
        candidate[f"{prefix}net_expectancy_delta"] = delta
        candidate[f"{prefix}fold_valid"] = valid
        valid_fold_count += valid
        positive_fold_count += delta is not None and delta > 0
    oos = folds[-1]
    baseline_oos = baseline_folds[-1]
    oos_available = (
        oos["trade_count"] >= MIN_OOS_CLOSED_TRADES
        and baseline_oos["trade_count"] >= MIN_OOS_CLOSED_TRADES
    )
    metrics.update(
        {
            f"{prefix}valid_time_fold_count": valid_fold_count,
            f"{prefix}positive_expectancy_fold_count": positive_fold_count,
            f"{prefix}oos_available": oos_available,
            f"{prefix}oos_trade_count": oos["trade_count"],
            f"{prefix}oos_net_expectancy": oos["net_expectancy"],
            f"{prefix}oos_baseline_net_expectancy": baseline_oos[
                "net_expectancy"
            ],
            f"{prefix}oos_return_drawdown_ratio": oos[
                "return_drawdown_ratio"
            ],
            f"{prefix}oos_baseline_return_drawdown_ratio": baseline_oos[
                "return_drawdown_ratio"
            ],
            f"{prefix}oos_maximum_drawdown": oos["maximum_drawdown"],
            f"{prefix}oos_baseline_maximum_drawdown": baseline_oos[
                "maximum_drawdown"
            ],
        }
    )


def _oos_trades(
    accumulator: ScenarioAccumulator, folds: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    oos = folds[-1]
    return [
        item
        for item in accumulator.closed_trades
        if oos["start_date"]
        <= str(item["exit_execution_at"])[:10]
        <= oos["end_date"]
    ]


def _adverse_not_worse(
    candidate: Optional[float], baseline: Optional[float]
) -> Optional[bool]:
    if candidate is None or baseline is None:
        return None
    return candidate >= baseline * RISK_WORSENING_TOLERANCE


def _apply_promotion_rules(
    metrics: list[dict[str, Any]],
    *,
    scenarios: Sequence[FactorScenario],
    normal: dict[str, ScenarioAccumulator],
    normal_folds: dict[str, list[dict[str, Any]]],
    stress: dict[str, ScenarioAccumulator],
    stress_folds: dict[str, list[dict[str, Any]]],
) -> None:
    by_key = {item["scenario"]: item for item in metrics}
    baseline = by_key["baseline"]
    baseline_oos_tail = trade_metrics(
        _oos_trades(normal["baseline"], normal_folds["baseline"])
    )
    scenario_by_key = {item.key: item for item in scenarios}
    expectancy_gains: dict[str, Optional[float]] = {}
    for item in metrics:
        expectancy_gains[item["scenario"]] = (
            float(item["oos_net_expectancy"])
            - float(item["oos_baseline_net_expectancy"])
            if item["oos_available"]
            and item["oos_net_expectancy"] is not None
            and item["oos_baseline_net_expectancy"] is not None
            else None
        )

    for item in metrics:
        key = item["scenario"]
        scenario = scenario_by_key[key]
        if key == "baseline":
            item.update(
                {
                    "promotion_status": "baseline",
                    "promotion_eligible": False,
                    "promotion_failures": "",
                }
            )
            continue
        oos_trades = _oos_trades(normal[key], normal_folds[key])
        oos_tail = trade_metrics(oos_trades)
        criteria: dict[str, Optional[bool]] = {
            "criterion_oos_expectancy_increment": (
                item["oos_net_expectancy"] > item["oos_baseline_net_expectancy"]
                if item["oos_available"]
                and item["oos_net_expectancy"] is not None
                and item["oos_baseline_net_expectancy"] is not None
                else None
            ),
            "criterion_oos_risk_return_increment": (
                item["oos_return_drawdown_ratio"]
                > item["oos_baseline_return_drawdown_ratio"]
                if item["oos_available"]
                and item["oos_return_drawdown_ratio"] is not None
                and item["oos_baseline_return_drawdown_ratio"] is not None
                else None
            ),
            "criterion_random_control_95pct": (
                item["random_expectancy_percentile"]
                >= RANDOM_CONTROL_REQUIRED_PERCENTILE
                and item["random_risk_score_percentile"]
                >= RANDOM_CONTROL_REQUIRED_PERCENTILE
                if item["random_control_available"]
                and item["random_expectancy_percentile"] is not None
                and item["random_risk_score_percentile"] is not None
                else None
            ),
            "criterion_three_of_four_positive_folds": (
                item["positive_expectancy_fold_count"] >= 3
                if item["valid_time_fold_count"] == 4
                else None
            ),
            "criterion_double_cost_advantage": (
                item["stress_oos_net_expectancy"]
                > item["stress_oos_baseline_net_expectancy"]
                and item["stress_oos_return_drawdown_ratio"]
                > item["stress_oos_baseline_return_drawdown_ratio"]
                if item["stress_oos_available"]
                and item["stress_oos_net_expectancy"] is not None
                and item["stress_oos_baseline_net_expectancy"] is not None
                and item["stress_oos_return_drawdown_ratio"] is not None
                and item["stress_oos_baseline_return_drawdown_ratio"] is not None
                else None
            ),
            "criterion_drawdown_not_worse": (
                item["oos_maximum_drawdown"]
                <= item["oos_baseline_maximum_drawdown"]
                * RISK_WORSENING_TOLERANCE
                if item["oos_available"]
                else None
            ),
            "criterion_es95_not_worse": _adverse_not_worse(
                oos_tail["es95_return"], baseline_oos_tail["es95_return"]
            ),
            "criterion_max_loss_not_worse": _adverse_not_worse(
                oos_tail["maximum_single_trade_loss"],
                baseline_oos_tail["maximum_single_trade_loss"],
            ),
            "criterion_right_tail_not_below_random": (
                item["baseline_top_5pct_winner_retention_rate"]
                >= item["signal_retention_rate"]
                if item["baseline_top_5pct_winner_retention_rate"] is not None
                and item["signal_retention_rate"] is not None
                else None
            ),
        }
        candidate_gain = expectancy_gains[key]
        stable_neighbors = []
        if candidate_gain is not None and candidate_gain > 0:
            for neighbor in scenarios:
                neighbor_gain = expectancy_gains.get(neighbor.key)
                if (
                    are_grid_neighbors(scenario, neighbor)
                    and neighbor_gain is not None
                    and neighbor_gain > 0
                    and min(candidate_gain, neighbor_gain)
                    / max(candidate_gain, neighbor_gain)
                    >= PLATFORM_EXPECTANCY_SIMILARITY
                ):
                    stable_neighbors.append(neighbor.key)
        criteria["criterion_stable_parameter_platform"] = bool(stable_neighbors)
        item["stable_neighbor_count"] = len(stable_neighbors)
        item["stable_neighbors"] = ",".join(stable_neighbors)
        item.update(criteria)
        failures = [name for name, value in criteria.items() if value is not True]
        eligible = scenario.factor != "RSI对照" and not failures
        item["promotion_eligible"] = eligible
        item["promotion_status"] = (
            "eligible"
            if eligible
            else ("control_only" if scenario.factor == "RSI对照" else "not_eligible")
        )
        item["promotion_failures"] = ",".join(failures)


def _load_board_mappings() -> tuple[dict[str, str], dict[str, set[str]]]:
    industry_data = json.loads(INDUSTRY_FILE.read_text(encoding="utf-8"))
    board_data = json.loads(BOARD_FILE.read_text(encoding="utf-8"))
    industries: dict[str, str] = {}
    for industry in industry_data.get("industries", []):
        for stock in industry.get("stocks", []):
            raw_code = str(stock.get("code") or stock.get("id") or "")
            if raw_code:
                industries.setdefault(raw_code.zfill(6), str(industry["name"]))
    concepts: dict[str, set[str]] = defaultdict(set)
    for concept in board_data.get("concepts", []):
        for stock in concept.get("stocks", []):
            raw_code = str(stock.get("code") or stock.get("id") or "")
            if raw_code:
                concepts[raw_code.zfill(6)].add(str(concept["name"]))
    return industries, concepts


def _attribution_rows(
    accumulators: Sequence[ScenarioAccumulator],
    *,
    mapping: dict[str, Any],
    overlapping: bool,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for accumulator in accumulators:
        signals: Counter[str] = Counter()
        trades: Counter[str] = Counter()
        pnls: defaultdict[str, float] = defaultdict(float)

        def groups(code: str) -> set[str]:
            value = mapping.get(code)
            if value is None:
                return {"未映射"}
            if overlapping:
                return set(value) or {"未映射"}
            return {str(value)}

        for signal in accumulator.accepted_signals:
            for group in groups(signal["code"]):
                signals[group] += 1
        for trade in accumulator.closed_trades:
            for group in groups(str(trade["code"])):
                trades[group] += 1
                pnls[group] += float(trade["net_pnl"])
        for group in sorted(set(signals) | set(trades)):
            output.append(
                {
                    "scenario": accumulator.scenario.key,
                    "scenario_label": accumulator.scenario.label,
                    "group": group,
                    "accepted_buy_signals": signals[group],
                    "accepted_signal_share": (
                        signals[group] / accumulator.accepted_signal_count
                        if accumulator.accepted_signal_count
                        else None
                    ),
                    "closed_trades": trades[group],
                    "closed_trade_net_pnl": round(pnls[group], 2),
                }
            )
    return output


def _format_percent(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2%}"


def _format_number(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def _top_rows(
    metrics: Sequence[dict[str, Any]], key: str, count: int = 12
) -> list[dict[str, Any]]:
    return sorted(
        (item for item in metrics if item[key] is not None),
        key=lambda item: float(item[key]),
        reverse=True,
    )[:count]


def _render_report(summary: dict[str, Any], metrics: Sequence[dict[str, Any]]) -> str:
    baseline = next(item for item in metrics if item["scenario"] == "baseline")
    lines = [
        "# MACD后置辅助指标：冻结第一轮67组",
        "",
        "## 冻结协议与结论边界",
        "",
        f"- 场景：1组基线 + {summary['core_scenario_count']}组核心 + {summary['rsi_control_count']}组RSI对照 = {summary['scenario_count']}组。",
        f"- 样本：{summary['sample_size']}只，{summary['start_date']}至{summary['end_date']}，共{summary['market_trade_day_count']}个交易日。",
        f"- 四个时序折范围：{summary['fold_ranges']}；第四折是预先固定的临时时序留出集。",
        "- 当前窗口较短，不足以证明跨年度/跨市场状态稳定性；本报告不把任何场景视为可直接上线的正式OOS晋级结果。",
        "- 正式MACD参数、观察、确认、卖出与撮合规则未修改；辅助指标只在MACD买入信号产生后门控。",
        "- S=0因盘中不可使用当日最终收盘，端点使用最近完整日线；其他日线指标也全部截止t-1。",
        "- RTOV来自stock_daily_detail真实历史换手率；价格与MACD继续使用独立量化行情。",
        f"- {summary['delayed_replay_start_stock_count']}只股票因期初前置日线不足而延后进入回放，其中{summary['flat_insufficient_history_stock_count']}只在整个窗口保持空仓。",
        f"- 首日基线信号{summary['first_day_baseline_signal_count']}个，占全部基线信号{_format_percent(summary['first_day_baseline_signal_share'])}；解读单日最大值时需考虑统一空仓启动效应。",
        f"- 晋级结果：{summary['promotion_eligible_count']}个核心场景满足全部冻结标准。",
        "",
        "## 1. 信号压缩能力",
        "",
        "| 场景 | 保留率 | 过滤后日均/中位 | P10/P90 | 零信号日 | 单日最大 | 覆盖率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in metrics:
        lines.append(
            f"| {item['scenario_label']} | {_format_percent(item['signal_retention_rate'])} | "
            f"{_format_number(item['filtered_daily_signal_mean'])}/{_format_number(item['filtered_daily_signal_median'])} | "
            f"{_format_number(item['filtered_daily_signal_p10'])}/{_format_number(item['filtered_daily_signal_p90'])} | "
            f"{_format_percent(item['zero_signal_day_rate'])} | {item['maximum_daily_signals']} | "
            f"{_format_percent(item['factor_coverage_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 2. 单笔交易质量（按净期望排序前12）",
            "",
            "| 场景 | 交易数 | 净期望/元 | 均值/中位收益 | 胜率 | 盈亏比 | PF | 持仓交易日 | MAE/MFE均值 | P10/P90/P95 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in _top_rows(metrics, "net_expectancy"):
        lines.append(
            f"| {item['scenario_label']} | {item['trade_count']} | {_format_number(item['net_expectancy'])} | "
            f"{_format_percent(item['mean_net_return'])}/{_format_percent(item['median_net_return'])} | "
            f"{_format_percent(item['win_rate'])} | {_format_number(item['payoff_ratio'])} | "
            f"{_format_number(item['profit_factor'])} | {_format_number(item['average_holding_trading_days'])} | "
            f"{_format_percent(item['mean_mae_return'])}/{_format_percent(item['mean_mfe_return'])} | "
            f"{_format_percent(item['trade_return_p10'])}/{_format_percent(item['trade_return_p90'])}/{_format_percent(item['trade_return_p95'])} |"
        )
    lines.extend(
        [
            "",
            "## 3. 组合与OOS风险收益",
            "",
            "| 场景 | 总收益 | 最大回撤 | 收益/回撤 | OOS净期望 | OOS收益/回撤 | 正增量折 | 2倍成本OOS净期望 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in _top_rows(metrics, "total_return"):
        lines.append(
            f"| {item['scenario_label']} | {_format_percent(item['total_return'])} | "
            f"{_format_percent(item['maximum_drawdown'])} | {_format_number(item['return_drawdown_ratio'])} | "
            f"{_format_number(item['oos_net_expectancy'])} | {_format_number(item['oos_return_drawdown_ratio'])} | "
            f"{item['positive_expectancy_fold_count']}/4 | {_format_number(item['stress_oos_net_expectancy'])} |"
        )
    lines.extend(
        [
            "",
            "## 4. 尾部风险（按ES95较优排序前12）",
            "",
            "| 场景 | ES90/ES95 | 最差1%/5% | 最大单笔亏损 | 最大连亏 | MAE P90/P95 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in _top_rows(metrics, "es95_return"):
        lines.append(
            f"| {item['scenario_label']} | {_format_percent(item['es90_return'])}/{_format_percent(item['es95_return'])} | "
            f"{_format_percent(item['worst_1pct_mean_return'])}/{_format_percent(item['worst_5pct_mean_return'])} | "
            f"{_format_percent(item['maximum_single_trade_loss'])} | {item['maximum_consecutive_losses']} | "
            f"{_format_percent(item['mae_loss_p90'])}/{_format_percent(item['mae_loss_p95'])} |"
        )
    lines.extend(
        [
            "",
            "## 5. 右尾赢家与随机对照",
            "",
            "| 场景 | 基线前5%赢家保留 | 随机期望差 | 筛后前10%利润贡献 | 随机净期望分位 | 随机风险分位 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in metrics:
        lines.append(
            f"| {item['scenario_label']} | {_format_percent(item['baseline_top_5pct_winner_retention_rate'])} | "
            f"{_format_percent(item['winner_retention_minus_random_expectation'])} | "
            f"{_format_percent(item['top_10pct_winner_profit_contribution'])} | "
            f"{_format_percent(item['random_expectancy_percentile'])} | "
            f"{_format_percent(item['random_risk_score_percentile'])} |"
        )
    eligible = [item for item in metrics if item["promotion_eligible"]]
    lines.extend(
        [
            "",
            "## 晋级判定",
            "",
            f"- 基线总收益率 {_format_percent(baseline['total_return'])}，净期望 {_format_number(baseline['net_expectancy'])} 元。",
            "- 自动判定同时要求：临时时序留出集净期望与组合风险收益增量、同数量交易级随机对照双95分位、四折至少三折为正、相邻参数稳定平台、双倍成本仍有优势、回撤/ES95/极端亏损不恶化超过5%、右尾赢家不低于同保留率随机预期。",
            (
                "- 通过全部标准：" + "、".join(item["scenario_label"] for item in eligible)
                if eligible
                else "- 当前没有场景通过全部冻结标准。"
            ),
            "- RSI始终只是对照组，即使数值优秀也不自动晋级。",
            "- `scenario_metrics.csv`、`time_fold_metrics.csv`与`promotion_assessment.csv`包含所有67组完整数值和失败原因。",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> Path:
    if args.start_date > args.end_date:
        raise ValueError("start-date不能晚于end-date")
    start_date = args.start_date.isoformat()
    end_date = args.end_date.isoformat()
    grid = getattr(args, "grid", "grid67")
    output_directory = (
        args.output_root
        / f"{start_date}_{end_date}"
        / f"n{args.sample_size}_seed{args.seed}_{grid}"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    selected_factors = PURIFICATION_SCENARIOS if grid == "purification12" else FACTOR_SCENARIOS
    scenarios = (BASELINE,) + selected_factors
    if len(scenarios) != (12 if grid == "purification12" else 67):
        raise RuntimeError("场景数不符合冻结网格")

    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5_000)
    try:
        database = client[settings.mongo_db_name]
        daily_collection = database[DAILY_HISTORY_COLLECTION]
        market_dates = _market_dates(
            daily_collection, start_date=start_date, end_date=end_date
        )
        if not market_dates or market_dates[0] != start_date:
            raise ValueError("start-date必须是期初股票池存在的市场交易日")
        universe = _sample_universe(daily_collection, start_date=start_date)
        sampled = sample_stocks(
            universe, sample_size=args.sample_size, seed=args.seed
        )
        factor_dates, signal_dates = _factor_calendar(
            daily_collection, start_date=start_date, end_date=end_date
        )
        turnover = _load_turnover(
            database[TURNOVER_COLLECTION],
            codes=[item.code for item in sampled],
            start_date=factor_dates[0],
            end_date=factor_dates[-1],
        )
        snapshots, populations = _load_ranked_factor_snapshots(
            daily_collection,
            sample_codes={item.code for item in sampled},
            factor_dates=factor_dates,
            signal_dates=signal_dates,
            turnover=turnover,
        )
        normal = {
            scenario.key: ScenarioAccumulator(scenario, market_dates)
            for scenario in scenarios
        }
        stress = {
            scenario.key: ScenarioAccumulator(scenario, market_dates)
            for scenario in scenarios
        }
        delayed_replay_starts: dict[str, Optional[str]] = {}
        for position, stock in enumerate(sampled, start=1):
            daily_documents = _load_daily_documents(
                daily_collection, code=stock.code, through_date=end_date
            )
            daily_bars = [
                Bar(
                    trade_date=str(item["trade_date"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                )
                for item in daily_documents
            ]
            minute_bars = _load_minute_bars(
                database[THREE_MINUTE_HISTORY_COLLECTION],
                code=stock.code,
                start_date=start_date,
                end_date=end_date,
            )
            name = str(daily_documents[-1].get("name") or stock.name_at_start)
            stock_snapshots = snapshots.get(stock.code, {})
            config = official_backtest_config(code=stock.code)
            stress_config = replace(
                config,
                commission_rate=config.commission_rate * 2,
                stamp_duty_rate=config.stamp_duty_rate * 2,
                slippage_rate=config.slippage_rate * 2,
            )
            effective_start = _effective_replay_start(
                daily_bars,
                requested_start=start_date,
                end_date=end_date,
            )
            if effective_start != start_date:
                delayed_replay_starts[stock.code] = effective_start
            if effective_start is None:
                for scenario in scenarios:
                    _add_flat_account(
                        normal[scenario.key], initial_cash=config.initial_cash, code=stock.code, name=name
                    )
                    _add_flat_account(
                        stress[scenario.key], initial_cash=config.initial_cash, code=stock.code, name=name
                    )
                continue
            for scenario in scenarios:
                gate = None
                if scenario.key != "baseline":
                    gate = lambda code, trade_date, selected=scenario: (
                        selected.accepts(snapshots.get(code, {}).get(trade_date)),
                        selected.label,
                    )
                result = replay(
                    code=stock.code,
                    name=name,
                    daily_bars=daily_bars,
                    minute_bars_by_date=minute_bars,
                    start_date=effective_start,
                    end_date=end_date,
                    config=config,
                    buy_signal_gate=gate,
                )
                _add_result(
                    normal[scenario.key],
                    code=stock.code,
                    name=name,
                    result=result,
                    snapshots=stock_snapshots,
                    initial_cash=config.initial_cash,
                    store_decisions=True,
                )
                stress_result = replay(
                    code=stock.code,
                    name=name,
                    daily_bars=daily_bars,
                    minute_bars_by_date=minute_bars,
                    start_date=effective_start,
                    end_date=end_date,
                    config=stress_config,
                    buy_signal_gate=gate,
                    official_strategy_configuration=False,
                )
                _add_result(
                    stress[scenario.key],
                    code=stock.code,
                    name=name,
                    result=stress_result,
                    snapshots=stock_snapshots,
                    initial_cash=config.initial_cash,
                    store_decisions=False,
                )
            if position % 5 == 0 or position == len(sampled):
                print(
                    f"factor_{grid}_progress completed={position}/{len(sampled)}",
                    flush=True,
                )
    finally:
        client.close()

    baseline = normal["baseline"]
    profitable_baseline = sorted(
        (
            item
            for item in baseline.closed_trades
            if float(item["net_pnl"]) > 0
        ),
        key=lambda item: float(item["net_pnl"]),
        reverse=True,
    )
    top_count = (
        max(1, math.ceil(len(profitable_baseline) * 0.05))
        if profitable_baseline
        else 0
    )
    baseline_top_winners = {
        (str(item["code"]), str(item["entry_signal_at"]))
        for item in profitable_baseline[:top_count]
    }
    capital_base = args.sample_size * official_backtest_config(
        code="000000"
    ).initial_cash
    metrics: list[dict[str, Any]] = []
    normal_folds: dict[str, list[dict[str, Any]]] = {}
    stress_folds: dict[str, list[dict[str, Any]]] = {}
    for scenario in scenarios:
        item, folds = _scenario_metrics(
            normal[scenario.key],
            baseline=baseline,
            snapshots=snapshots,
            market_dates=market_dates,
            capital_base=capital_base,
            baseline_top_winners=baseline_top_winners,
            seed=args.seed,
        )
        normal_folds[scenario.key] = folds
        stress_item, stress_fold_rows = _scenario_metrics(
            stress[scenario.key],
            baseline=stress["baseline"],
            snapshots=snapshots,
            market_dates=market_dates,
            capital_base=capital_base,
            baseline_top_winners=baseline_top_winners,
            seed=args.seed,
        )
        for fold in stress_fold_rows:
            fold["cost_mode"] = "double_cost"
        stress_folds[scenario.key] = stress_fold_rows
        for key, value in stress_item.items():
            if key not in {"scenario", "scenario_label", "factor", "parameters"}:
                item[f"stress_{key}"] = value
        metrics.append(item)

    baseline_folds = normal_folds["baseline"]
    stress_baseline_folds = stress_folds["baseline"]
    for item in metrics:
        key = item["scenario"]
        _add_fold_comparisons(item, normal_folds[key], baseline_folds)
        _add_fold_comparisons(
            item,
            stress_folds[key],
            stress_baseline_folds,
            prefix="stress_",
        )
    _apply_promotion_rules(
        metrics,
        scenarios=scenarios,
        normal=normal,
        normal_folds=normal_folds,
        stress=stress,
        stress_folds=stress_folds,
    )

    industries, concepts = _load_board_mappings()
    ordered_accumulators = [normal[item.key] for item in scenarios]
    industry_rows = _attribution_rows(
        ordered_accumulators, mapping=industries, overlapping=False
    )
    concept_rows = _attribution_rows(
        ordered_accumulators, mapping=concepts, overlapping=True
    )
    factor_rows: list[dict[str, Any]] = []
    for stock in sampled:
        for trade_date in signal_dates:
            snapshot = snapshots.get(stock.code, {}).get(trade_date)
            factor_rows.append(
                {
                    "code": stock.code,
                    "name": stock.name_at_start,
                    "signal_date": trade_date,
                    "completed_date": snapshot.completed_date if snapshot else None,
                    **(snapshot.values if snapshot else {}),
                    "population_counts": json.dumps(
                        populations[trade_date], sort_keys=True
                    ),
                }
            )

    _write_csv(
        output_directory / "sampled_stocks.csv",
        (
            {
                "sample_rank": item.sample_rank,
                "code": item.code,
                "name_at_start": item.name_at_start,
                "effective_replay_start": delayed_replay_starts.get(
                    item.code, start_date
                ),
                "replay_start_delayed": item.code in delayed_replay_starts,
            }
            for item in sampled
        ),
    )
    _write_csv(output_directory / "factor_snapshots.csv", factor_rows)
    _write_csv(
        output_directory / "signal_decisions.csv",
        (row for item in ordered_accumulators for row in item.signal_decisions),
    )
    _write_csv(
        output_directory / "closed_trades.csv",
        (row for item in ordered_accumulators for row in item.closed_trades),
    )
    _write_csv(output_directory / "scenario_metrics.csv", metrics)
    _write_csv(
        output_directory / "promotion_assessment.csv",
        (
            {
                key: value
                for key, value in item.items()
                if key.startswith("criterion_")
                or key
                in {
                    "scenario",
                    "scenario_label",
                    "factor",
                    "parameters",
                    "promotion_status",
                    "promotion_eligible",
                    "promotion_failures",
                    "stable_neighbor_count",
                    "stable_neighbors",
                }
            }
            for item in metrics
        ),
    )
    _write_csv(
        output_directory / "time_fold_metrics.csv",
        (
            row
            for scenario in scenarios
            for row in normal_folds[scenario.key] + stress_folds[scenario.key]
        ),
    )
    _write_csv(output_directory / "industry_attribution.csv", industry_rows)
    _write_csv(output_directory / "concept_attribution.csv", concept_rows)

    summary = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "macd_strategy_modified": False,
        "start_date": start_date,
        "end_date": end_date,
        "sample_size": args.sample_size,
        "random_seed": args.seed,
        "scenario_count": len(scenarios),
        "core_scenario_count": sum(item.factor != "RSI对照" for item in selected_factors),
        "rsi_control_count": sum(item.factor == "RSI对照" for item in selected_factors),
        "factor_scenarios": [item.key for item in selected_factors],
        "grid": grid,
        "account_model": "equal_initial_cash_per_stock_independent_compounding_all_in_all_out",
        "promotion_rules_role": "legacy_rules_recomputed_within_selected_grid" if grid == "purification12" else "first_round_assessment",
        "daily_factor_cutoff": "previous_completed_trading_day",
        "rs_s0_adaptation": "signal day close unavailable intraday; use t-1 close",
        "turnover_source": TURNOVER_COLLECTION,
        "random_control_iterations": 1_000,
        "market_trade_day_count": len(market_dates),
        "fold_ranges": ", ".join(
            f"F{item['fold']}={item['start_date']}~{item['end_date']}"
            for item in baseline_folds
        ),
        "first_day_baseline_signal_count": baseline.accepted_by_date[
            market_dates[0]
        ],
        "first_day_baseline_signal_share": (
            baseline.accepted_by_date[market_dates[0]]
            / len(baseline.accepted_signals)
            if baseline.accepted_signals
            else None
        ),
        "delayed_replay_start_stock_count": len(delayed_replay_starts),
        "flat_insufficient_history_stock_count": sum(
            value is None for value in delayed_replay_starts.values()
        ),
        "oos_definition": "fourth chronological fold; provisional short-window holdout",
        "promotion_eligible_count": sum(
            bool(item["promotion_eligible"]) for item in metrics
        ),
        "metrics": metrics,
    }
    if grid == "purification12":
        assignments, diagnostics = baseline_trade_diagnostics(
            baseline_trades=baseline.closed_trades, scenarios=selected_factors,
            snapshots=snapshots, market_dates=market_dates, account_count=len(sampled),
        )
        paired_rows, paired_summaries = [], []
        for scenario in selected_factors:
            rows, paired_summary = paired_account_results(
                baseline.stock_results, normal[scenario.key].stock_results,
                scenario=scenario.key, scenario_label=scenario.label,
            )
            paired_rows.extend(rows)
            paired_summaries.append(paired_summary)
        neighbors = parameter_comparisons(selected_factors, metrics, diagnostics)
        for filename, rows in (
            ("baseline_trade_assignments.csv", assignments),
            ("purification_diagnostics.csv", diagnostics),
            ("stock_paired_results.csv", paired_rows),
            ("paired_summary.csv", paired_summaries),
            ("parameter_comparisons.csv", neighbors),
        ):
            _write_csv(output_directory / filename, rows)
        summary["paired_summaries"] = paired_summaries
        summary["oos_definition"] = "reused exploratory folds; no new independent OOS"
        report = render_purification_report(summary, metrics, diagnostics, paired_summaries, neighbors)
    else:
        report = _render_report(summary, metrics)
    (output_directory / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    report_path = output_directory / "report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def main() -> None:
    report_path = run(build_argument_parser().parse_args())
    print(f"factor_experiments_finished report={report_path}", flush=True)


if __name__ == "__main__":
    main()
