"""量化查询的公开数据契约。匿名策略身份，完整提供已记录的业务与计算数据。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from app.quant.strategies.provisional_daily_macd_3m.config import STRATEGY_ID, STRATEGY_LABEL


DEFAULT_PUBLIC_STRATEGY_ID = "strategy_1"
# 公开编号与内部存储键分离；只有显式登记的策略可以通过查询 API 访问。
PUBLIC_STRATEGIES = {DEFAULT_PUBLIC_STRATEGY_ID: (STRATEGY_ID, "策略1")}
SignalStatus = Literal["pending_execution", "filled", "rejected", "cancelled", "unknown"]
ObservationState = Literal[
    "watching", "holding", "signal_confirmed", "pending_execution", "filled",
    "rejected", "not_triggered", "unknown",
]
DataStatus = Literal[
    "waiting_open", "waiting_data", "fresh", "partial", "closed", "closed_partial", "error", "unknown",
]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class PublicStrategy(PublicModel):
    id: str
    name: str
    execution_kind: Literal["shadow_simulation"] = "shadow_simulation"


class StrategyDetail(PublicStrategy):
    version: str | None = None
    macd_parameters: list[int] = []
    intraday_interval: str | None = None
    minimum_shrink_ratio: float | None = None
    confirmation_bars: int | None = None
    buy_filter: dict[str, Any] = {}
    exit_policy: str | None = None
    recording_start_date: str | None = None


class ExecutionRule(PublicModel):
    mode: str | None = None
    initial_cash_per_stock: float | None = None
    slippage_rate: float | None = None
    commission_rate: float | None = None
    stamp_duty_rate: float | None = None
    lot_size: int | None = None
    settlement: str | None = None
    price_limit: str | None = None


class TimelineEntry(PublicModel):
    stage: str
    status: str
    at: str | None = None


class StrategyList(PublicModel):
    items: list[PublicStrategy]
    total: int


class Recording(PublicModel):
    start_date: str | None = None
    mode: Literal["historical_replay", "live", "unknown"] = "unknown"
    market_data_trade_date: str | None = None
    computed_at: str | None = None
    data_kind: str | None = None
    strategy_version: str | None = None
    reference_price_method: str | None = None
    historical_bar_policy: str | None = None
    history_rebased_at: str | None = None
    accounting_rebased_at: str | None = None
    execution_kind: Literal["shadow_simulation"] = "shadow_simulation"


class Runtime(PublicModel):
    data_status_detail: str | None = None
    schema_version: str | None = None
    mode: str | None = None
    last_complete_bar_at: str | None = None
    next_evaluation_at: str | None = None
    expected_complete_bar_count: int | None = None
    bars_per_complete_day: int | None = None
    observation_count: int | None = None
    complete_observation_count: int | None = None
    incomplete_observation_count: int | None = None
    tracked_code_count: int | None = None
    source: dict[str, Any] = {}
    preparation_quality: dict[str, Any] = {}
    resource_limits: dict[str, Any] = {}
    observation_state_counts: dict[str, int] = {}
    last_error: str | None = None
    last_error_at: str | None = None
    version: int | None = None
    evaluated_at: str | None = None
    last_valuation_at: str | None = None
    data_status: DataStatus = "unknown"
    incomplete_code_count: int | None = None
    incomplete_codes: list[str] = []


class Summary(PublicModel):
    watching_count: int | None = None
    not_triggered_count: int | None = None
    sell_candidate_count: int | None = None
    realized_return: float | None = None
    gross_unrealized_pnl: float | None = None
    gross_unrealized_return: float | None = None
    unrealized_return: float | None = None
    holding_market_day_pnl: float | None = None
    holding_market_day_return: float | None = None
    open_position_account_day_pnl: float | None = None
    open_position_account_day_return: float | None = None
    closed_position_account_day_pnl: float | None = None
    closed_position_account_day_return: float | None = None
    return_basis: str | None = None
    account_count: int | None = None
    universe_account_count: int | None = None
    inactive_account_count: int | None = None
    new_account_count: int | None = None
    capital_inflow: float | None = None
    opening_total_assets: float | None = None
    account_day_return_base: float | None = None
    account_day_return_basis: str | None = None
    initial_capital: float | None = None
    cash_balance: float | None = None
    market_value: float | None = None
    total_assets: float | None = None
    total_pnl: float | None = None
    total_return: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    account_day_pnl: float | None = None
    account_day_return: float | None = None
    observation_count: int | None = None
    preselection_count: int | None = None
    buy_count: int | None = None
    sell_count: int | None = None
    holding_count: int | None = None
    t1_locked_holding_count: int | None = None
    closed_trade_count: int | None = None
    signal_count: int | None = None
    pending_signal_count: int | None = None
    rejected_signal_count: int | None = None
    buy_notional: float | None = None
    sell_notional: float | None = None
    turnover: float | None = None
    total_fees: float | None = None
    net_cash_flow: float | None = None


class StockRecord(PublicModel):
    code: str
    name: str | None = None


class IndicatorDetails(StockRecord):
    reason: str | None = None
    observation_before_date: str | None = None
    observation_date: str | None = None
    reference_histogram: float | None = None
    provisional_dif: float | None = None
    provisional_dea: float | None = None
    provisional_histogram: float | None = None
    shrink_ratio: float | None = None
    adx_14: float | None = None
    adx_14_3_days_ago: float | None = None
    factor_completed_date: str | None = None
    factor_comparison_date: str | None = None


class ExecutionAttempt(PublicModel):
    attempt_at: str | None = None
    execution_bar_end_at: str | None = None
    reference_open: float | None = None
    daily_price_limit: float | None = None
    status: str | None = None
    reason: str | None = None


class Signal(IndicatorDetails):
    status_detail: str | None = None
    minimum_shrink_ratio: float | None = None
    confirmation_count: int | None = None
    execution_reference_price: float | None = None
    attempt_count: int | None = None
    attempts: list[ExecutionAttempt] = []
    exit_reason: str | None = None
    deferred_from: str | None = None
    estimated_net_return: float | None = None
    signal_id: str | None = None
    action: Literal["buy", "sell"] | None = None
    status: SignalStatus = "unknown"
    signal_at: str | None = None
    signal_price: float | None = None
    execution_at: str | None = None
    execution_price: float | None = None
    shares: int | None = None


class Observation(IndicatorDetails):
    data_status_detail: str | None = None
    state_detail: str | None = None
    condition_met: bool | None = None
    consecutive_confirmations: int | None = None
    required_confirmations: int | None = None
    last_complete_bar_at: str | None = None
    adx_buy_allowed: bool | None = None
    adjustment_factor: float | None = None
    exit_state: str | None = None
    action: Literal["buy", "sell", "hold"] | None = None
    state: ObservationState = "unknown"
    data_status: DataStatus = "unknown"
    signal_id: str | None = None


class Execution(StockRecord):
    trade_date: str | None = None
    snapshot_id: str | None = None
    recording: Recording | None = None
    execution_kind: Literal["shadow_simulation"] = "shadow_simulation"
    marker_type: Literal["simulated_execution"] = "simulated_execution"
    price_basis: Literal["recorded_execution_price"] = "recorded_execution_price"
    reason: str | None = None
    execution_reference_price: float | None = None
    execution_price_source: str | None = None
    previous_close: float | None = None
    daily_price_limit: float | None = None
    execution_bar_low: float | None = None
    execution_bar_high: float | None = None
    slippage_rate: float | None = None
    event_id: str | None = None
    action: Literal["buy", "sell"] | None = None
    status: Literal["filled", "unknown"] = "unknown"
    signal_at: str | None = None
    signal_price: float | None = None
    execution_at: str | None = None
    execution_price: float | None = None
    shares: int | None = None
    notional: float | None = None
    commission: float | None = None
    stamp_duty: float | None = None
    total_fees: float | None = None
    cash_flow: float | None = None


class Holding(StockRecord):
    entry_signal_at: str | None = None
    entry_signal_price: float | None = None
    entry_reference_price: float | None = None
    gross_total_pnl: float | None = None
    gross_total_return: float | None = None
    shares: int | None = None
    entry_event_id: str | None = None
    entry_execution_at: str | None = None
    entry_execution_price: float | None = None
    entry_notional: float | None = None
    buy_commission: float | None = None
    cost_basis: float | None = None
    marked_at: str | None = None
    mark_price: float | None = None
    previous_close: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_return: float | None = None
    total_pnl: float | None = None
    total_return: float | None = None
    market_day_pnl: float | None = None
    market_day_return: float | None = None
    account_day_pnl: float | None = None
    account_day_return: float | None = None
    sellable_today: bool | None = None
    t1_locked: bool | None = None


class ClosedTrade(StockRecord):
    entry_signal_at: str | None = None
    exit_signal_at: str | None = None
    entry_signal_price: float | None = None
    exit_signal_price: float | None = None
    entry_reference_price: float | None = None
    exit_reference_price: float | None = None
    shares: int | None = None
    entry_event_id: str | None = None
    exit_event_id: str | None = None
    entry_execution_at: str | None = None
    exit_execution_at: str | None = None
    entry_execution_price: float | None = None
    exit_execution_price: float | None = None
    entry_notional: float | None = None
    exit_notional: float | None = None
    buy_commission: float | None = None
    sell_commission: float | None = None
    stamp_duty: float | None = None
    total_fees: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    net_return: float | None = None


class Candidate(StockRecord):
    reason: str | None = None
    reference_price: float | None = None
    status: str | None = None


class ExitDecision(StockRecord):
    at: str | None = None
    entry_at: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    action: str | None = None
    reason: str | None = None
    original_sell: bool | None = None
    deferred_from: str | None = None
    adx_state: str | None = None
    adx: float | None = None
    adx_3_days_ago: float | None = None
    factor_completed_date: str | None = None
    provisional_dif: float | None = None
    provisional_histogram: float | None = None
    allow_delay: bool | None = None
    data_anomaly: bool | None = None
    quote_price: float | None = None
    quote_net_proceeds: float | None = None
    entry_cost: float | None = None
    estimated_net_return: float | None = None


class Account(StockRecord):
    first_buy_at: str | None = None
    has_traded: bool = True
    initial_capital: float | None = None
    cash_balance: float | None = None
    market_value: float | None = None
    total_assets: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    total_pnl: float | None = None
    total_return: float | None = None
    has_position: bool | None = None
    shares: int | None = None
    marked_at: str | None = None


class SnapshotMeta(PublicModel):
    schema_version: Literal["1.2"] = "1.2"
    source_schema_version: str | None = None
    strategy_id: str
    strategy_name: str
    trade_date: str
    snapshot_id: str
    updated_at: str | None = None
    currency: Literal["CNY"] = "CNY"
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    execution_kind: Literal["shadow_simulation"] = "shadow_simulation"


class ObservationSummary(PublicModel):
    count: int | None = None
    state_counts: dict[ObservationState, int] = {}
    detail_state_counts: dict[str, int] = {}


class SignalSummary(PublicModel):
    count: int | None = None
    recent_items: list[Signal] = []


class Overview(SnapshotMeta):
    strategy: StrategyDetail
    selection_date: str | None = None
    generated_at: str | None = None
    execution_rule: ExecutionRule
    timeline: list[TimelineEntry] = []
    status: Literal["waiting_open", "monitoring", "closed", "error", "unknown"] = "unknown"
    recording: Recording
    runtime: Runtime
    summary: Summary
    observation_summary: ObservationSummary
    signal_summary: SignalSummary


class OverviewResponse(PublicModel):
    data: Overview


class SignalPool(PublicModel):
    count: int
    items: list[Signal]


class ObservationPool(PublicModel):
    count: int
    items: list[Observation]


class ExecutionPool(PublicModel):
    interval: str | None = None
    count: int
    items: list[Execution]


class HoldingPool(PublicModel):
    count: int
    items: list[Holding]


class ClosedTradePool(PublicModel):
    count: int
    items: list[ClosedTrade]


class CandidatePool(PublicModel):
    count: int
    items: list[Candidate]


class ExitDecisionPool(PublicModel):
    count: int
    items: list[ExitDecision]


class AccountPool(PublicModel):
    available: bool
    count: int
    items: list[Account]


class DailySnapshot(Overview):
    preselection_pool: CandidatePool
    sell_candidate_pool: CandidatePool
    exit_decisions: ExitDecisionPool
    accounts: AccountPool
    observation_pool: ObservationPool
    signals: SignalPool
    intraday_trading: ExecutionPool
    holding_pool: HoldingPool
    closed_trades: ClosedTradePool


class DailySnapshotResponse(PublicModel):
    data: DailySnapshot


class PageMeta(SnapshotMeta):
    total: int
    page: int
    page_size: int


class SignalPage(PageMeta):
    items: list[Signal]


class ObservationPage(PageMeta):
    items: list[Observation]


class ExecutionHistory(PublicModel):
    covered_start_date: str | None = None
    covered_end_date: str | None = None
    trade_day_count: int = 0
    recording_start_dates: list[str] = []
    recording_modes: list[str] = []
    strategy_versions: list[str] = []
    computed_at: str | None = None
    history_rebased_at: str | None = None
    accounting_rebased_at: str | None = None
    incomplete_trade_dates: list[str] = []


class ExecutionPage(PageMeta):
    # 跨日查询没有唯一交易日，不能伪装成末日快照。
    trade_date: str | None = None
    query_mode: Literal["single_day", "date_range"] = "single_day"
    code: str | None = None
    action: Literal["buy", "sell"] | None = None
    start_date: str | None = None
    end_date: str | None = None
    history_version: str | None = None
    history: ExecutionHistory | None = None
    items: list[Execution]


class HoldingPage(PageMeta):
    items: list[Holding]


class ClosedTradePage(PageMeta):
    items: list[ClosedTrade]


class CandidatePage(PageMeta):
    items: list[Candidate]


class ExitDecisionPage(PageMeta):
    items: list[ExitDecision]


class AccountPage(PageMeta):
    available: bool
    items: list[Account]


class PerformancePoint(SnapshotMeta):
    recording: Recording
    runtime: Runtime
    summary: Summary


class PerformancePage(PublicModel):
    strategy_id: str
    strategy_name: str
    items: list[PerformancePoint]
    total: int
    page: int
    page_size: int


def anonymize_document(document: Mapping[str, Any], public_id: str) -> dict[str, Any]:
    """只替换真实策略名称/存储标识；计算术语、参数、原因及数值原样保留。"""
    replacements = {STRATEGY_LABEL: PUBLIC_STRATEGIES[DEFAULT_PUBLIC_STRATEGY_ID][1]}
    for alias, (private_id, label) in PUBLIC_STRATEGIES.items():
        replacements[private_id] = alias
    real_name = (document.get("strategy") or {}).get("name")
    if real_name:
        replacements[real_name] = PUBLIC_STRATEGIES[public_id][1]
    pattern = re.compile("|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)))

    def replace(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub(lambda match: replacements[match.group()], value)
        if isinstance(value, Mapping):
            return {replace(key): replace(child) for key, child in value.items() if key != "_id"}
        if isinstance(value, (list, tuple)):
            return [replace(child) for child in value]
        return value

    result = replace(document)
    result["strategy_id"] = public_id
    result["strategy"] = {**(result.get("strategy") or {}),
                          "id": public_id, "name": PUBLIC_STRATEGIES[public_id][1]}
    return result


def public_strategy(public_id: str) -> PublicStrategy:
    return PublicStrategy(id=public_id, name=PUBLIC_STRATEGIES[public_id][1])


def public_id_for_document(document: Mapping[str, Any]) -> str:
    internal_id = document.get("strategy_id") or document.get("strategy", {}).get("id")
    for public_id, (private_id, _) in PUBLIC_STRATEGIES.items():
        if internal_id in (private_id, public_id):
            return public_id
    raise ValueError("未登记的公开策略")


def _opaque_id(prefix: str, public_id: str, value: Any) -> str | None:
    if value is None:
        return None
    digest = hashlib.sha256(json.dumps([public_id, value], default=str).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def snapshot_meta(document: Mapping[str, Any], public_id: str) -> dict[str, Any]:
    runtime = document.get("runtime") or {}
    revision = [document.get("trade_date"), document.get("updated_at"), document.get("status"),
                (document.get("recording") or {}).get("computed_at"),
                (document.get("recording") or {}).get("accounting_rebased_at"),
                "public_schema_1.2"]
    revision.extend(runtime.get(key) for key in (
        "version", "evaluated_at", "last_valuation_at", "data_status", "last_error_at",
    ))
    return SnapshotMeta(
        source_schema_version=document.get("schema_version"),
        strategy_id=public_id, strategy_name=public_strategy(public_id).name,
        trade_date=document["trade_date"], snapshot_id=_opaque_id("snap", public_id, revision),
        updated_at=document.get("updated_at"),
    ).model_dump()


def signal_status(value: Any) -> str:
    return {
        "pending_execution": "pending_execution", "deferred_limit_down": "pending_execution",
        "deferred_t1": "pending_execution", "filled": "filled", "cancelled": "cancelled",
        "rejected": "rejected", "rejected_adx": "rejected", "rejected_limit_up": "rejected",
        "rejected_insufficient_cash": "rejected",
    }.get(value, "unknown")


def observation_state(value: Any) -> str:
    return {
        "watching": "watching", "confirming": "watching", "holding": "holding",
        "deferred_exit": "holding", "signal_confirmed": "signal_confirmed",
        "pending_execution": "pending_execution", "deferred_t1": "pending_execution",
        "deferred_limit_down": "pending_execution", "filled": "filled", "rejected": "rejected",
        "rejected_adx": "rejected", "rejected_limit_up": "rejected",
        "rejected_insufficient_cash": "rejected", "not_triggered": "not_triggered",
    }.get(value, "unknown")


def _data_status(value: Any) -> str:
    return value if value in (
        "waiting_open", "waiting_data", "fresh", "partial", "closed", "closed_partial", "error",
    ) else "unknown"


def _sum_money(item: Mapping[str, Any], *fields: str) -> float | None:
    # 缺数据不是 0；不能把错误或旧快照伪装成零成交、零费用。
    if any(item.get(key) is None for key in fields):
        return None
    return round(sum(float(item[key]) for key in fields), 2)


def public_signal(item: Mapping[str, Any], public_id: str) -> Signal:
    return Signal.model_validate({**item,
        "signal_id": _opaque_id("sig", public_id, item.get("signal_id")),
        "status_detail": item.get("status"),
        "status": signal_status(item.get("status")),
    })


def public_observation(item: Mapping[str, Any], public_id: str) -> Observation:
    return Observation.model_validate({**item,
        "signal_id": _opaque_id("sig", public_id, item.get("signal_id")),
        "state_detail": item.get("state"),
        "state": observation_state(item.get("state")),
        "data_status_detail": item.get("data_status"),
        "data_status": _data_status(item.get("data_status")),
    })


def public_execution(item: Mapping[str, Any], public_id: str,
                     *, document: Mapping[str, Any] | None = None) -> Execution:
    fees = _sum_money(item, "commission", "stamp_duty")
    cash_flow = None
    if fees is not None and item.get("notional") is not None and item.get("action") in ("buy", "sell"):
        cash_flow = round(float(item["notional"]) * (-1 if item["action"] == "buy" else 1) - fees, 2)
    context = {}
    if document is not None:
        context = {"trade_date": document["trade_date"],
                   "snapshot_id": snapshot_meta(document, public_id)["snapshot_id"],
                   "recording": public_recording(document).model_dump()}
    return Execution.model_validate({**item, **context,
        "execution_kind": "shadow_simulation", "marker_type": "simulated_execution",
        "price_basis": "recorded_execution_price",
        "event_id": _opaque_id("tx", public_id, item.get("event_id")),
        "status": "filled" if item.get("status") == "filled" else "unknown",
        "total_fees": fees, "cash_flow": cash_flow,
    })


def public_holding(item: Mapping[str, Any], public_id: str) -> Holding:
    return Holding.model_validate({**item,
        "entry_event_id": _opaque_id("tx", public_id, item.get("entry_event_id")),
        "cost_basis": _sum_money(item, "entry_notional", "buy_commission"),
    })


def public_closed_trade(item: Mapping[str, Any], public_id: str) -> ClosedTrade:
    return ClosedTrade.model_validate({**item,
        "entry_event_id": _opaque_id("tx", public_id, item.get("entry_event_id")),
        "exit_event_id": _opaque_id("tx", public_id, item.get("exit_event_id")),
        "total_fees": _sum_money(item, "buy_commission", "sell_commission", "stamp_duty"),
    })


def public_candidate(item: Mapping[str, Any], public_id: str) -> Candidate:
    return Candidate.model_validate(item)


def public_exit_decision(item: Mapping[str, Any], public_id: str) -> ExitDecision:
    return ExitDecision.model_validate(item)


POOL_SERIALIZERS = {
    "signals": public_signal, "observation_pool": public_observation,
    "intraday_trading": public_execution, "holding_pool": public_holding,
    "closed_trades": public_closed_trade,
    "preselection_pool": public_candidate, "sell_candidate_pool": public_candidate,
    "exit_decisions": public_exit_decision,
}


def public_pool(document: Mapping[str, Any], pool_name: str, public_id: str) -> list[dict[str, Any]]:
    if pool_name == "intraday_trading":
        return [public_execution(item, public_id, document=document).model_dump()
                for item in (document.get(pool_name) or {}).get("items", [])]
    return [POOL_SERIALIZERS[pool_name](item, public_id).model_dump()
            for item in (document.get(pool_name) or {}).get("items", [])]


def execution_history_metadata(
    documents: list[Mapping[str, Any]], public_id: str, *,
    start_date: str, end_date: str, code: str | None, action: str | None,
) -> tuple[str, ExecutionHistory]:
    """版本包含区间内所有日期的修订信息，与分页页码无关；输入已匿名。"""
    ordered = sorted(documents, key=lambda d: d["trade_date"])
    sources = [{"trade_date": d["trade_date"],
                "snapshot_id": snapshot_meta(d, public_id)["snapshot_id"],
                "recording": public_recording(d).model_dump(),
                "strategy_version": (d.get("strategy") or {}).get("version")}
               for d in ordered]
    version = _opaque_id("hist", public_id, {
        "start_date": start_date, "end_date": end_date, "code": code, "action": action,
        "sources": sources,
    })
    def recording_values(key: str) -> list[str]:
        return sorted({s["recording"][key] for s in sources if s["recording"].get(key)})
    def latest(key: str) -> str | None:
        values = recording_values(key)
        return values[-1] if values else None
    history = ExecutionHistory(
        covered_start_date=ordered[0]["trade_date"] if ordered else None,
        covered_end_date=ordered[-1]["trade_date"] if ordered else None,
        trade_day_count=len(ordered),
        recording_start_dates=recording_values("start_date"),
        recording_modes=recording_values("mode"),
        strategy_versions=sorted({s["strategy_version"] for s in sources if s["strategy_version"]}
                                 | set(recording_values("strategy_version"))),
        computed_at=latest("computed_at"), history_rebased_at=latest("history_rebased_at"),
        accounting_rebased_at=latest("accounting_rebased_at"),
        incomplete_trade_dates=[d["trade_date"] for d in ordered
            if (d.get("runtime") or {}).get("data_status") not in ("closed", "fresh")],
    )
    return version, history


def public_accounts(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """仅展示曾买入账户（含已清仓）；没有账本或估值的数据不捏造成零。"""
    source = (document.get("_runtime_state") or {}).get("accounts")
    if source is None:
        return []
    pool = document.get("holding_pool") or {}
    holdings = {item["code"]: item for item in pool.get("items", [])}
    complete = "items" in pool and pool.get("count") == len(pool["items"])
    result = []
    for raw in source:
        if raw.get("first_buy_at") is None:
            continue
        holding = holdings.get(raw["code"])
        market_value = holding.get("market_value") if holding else (0.0 if complete else None)
        capital, cash, realized = raw.get("initial_cash"), raw.get("cash"), raw.get("realized_pnl")
        assets = round(cash + market_value, 2) if cash is not None and market_value is not None else None
        pnl = round(assets - capital, 2) if assets is not None and capital is not None else None
        result.append(Account(
            code=raw["code"], name=raw.get("name"), initial_capital=capital, cash_balance=cash,
            first_buy_at=raw["first_buy_at"], has_traded=True,
            market_value=market_value, total_assets=assets, realized_pnl=realized,
            unrealized_pnl=round(pnl - realized, 2) if pnl is not None and realized is not None else None,
            total_pnl=pnl, total_return=pnl / capital if pnl is not None and capital else None,
            has_position=True if holding else (False if complete else None),
            shares=holding.get("shares") if holding else (0 if complete else None),
            marked_at=holding.get("marked_at") if holding else None,
        ).model_dump())
    return result


def public_recording(document: Mapping[str, Any]) -> Recording:
    source = document.get("recording") or {}
    mode = source.get("mode")
    return Recording.model_validate({**source,
        "mode": mode if mode in ("historical_replay", "live") else "unknown",
        "execution_kind": "shadow_simulation",
    })


def public_runtime(document: Mapping[str, Any]) -> Runtime:
    source = document.get("runtime") or {}
    return Runtime.model_validate({**source, "data_status_detail": source.get("data_status"),
                                   "data_status": _data_status(source.get("data_status"))})


def public_summary(document: Mapping[str, Any], public_id: str) -> Summary:
    summary = Summary.model_validate(document.get("summary") or {}).model_dump()
    summary["observation_count"] = (document.get("observation_pool") or {}).get("count")
    source = document.get("intraday_trading") or {}
    if "items" in source:
        executions = public_pool(document, "intraday_trading", public_id)
        filled = [item for item in executions if item["status"] == "filled"]
        # 不完整成交池不生成虚假的部分合计。
        if len(executions) == source.get("count") and len(filled) == len(executions):
            buys = [item for item in filled if item["action"] == "buy"]
            sells = [item for item in filled if item["action"] == "sell"]
            def total(items: list[dict[str, Any]], field: str) -> float | None:
                if any(item.get(field) is None for item in items):
                    return None
                return round(sum(item[field] for item in items), 2)
            summary.update(buy_notional=total(buys, "notional"), sell_notional=total(sells, "notional"),
                           turnover=total(filled, "notional"), total_fees=total(filled, "total_fees"),
                           net_cash_flow=total(filled, "cash_flow"))
    return Summary.model_validate(summary)


def public_overview(document: Mapping[str, Any], public_id: str) -> Overview:
    runtime = document.get("runtime") or {}
    counts: Counter = Counter()
    for state, count in (runtime.get("observation_state_counts") or {}).items():
        counts[observation_state(state)] += int(count)
    status = document.get("status")
    return Overview(
        **snapshot_meta(document, public_id), strategy=StrategyDetail.model_validate({
            **(document.get("strategy") or {}), **public_strategy(public_id).model_dump()}),
        selection_date=document.get("selection_date"), generated_at=document.get("generated_at"),
        execution_rule=ExecutionRule.model_validate(document.get("execution_rule") or {}),
        timeline=document.get("timeline") or [],
        status=status if status in ("waiting_open", "monitoring", "closed", "error") else "unknown",
        recording=public_recording(document), runtime=public_runtime(document),
        summary=public_summary(document, public_id),
        observation_summary=ObservationSummary(
            count=(document.get("observation_pool") or {}).get("count"), state_counts=dict(counts),
            detail_state_counts=runtime.get("observation_state_counts") or {}),
        signal_summary=SignalSummary(count=(document.get("signals") or {}).get("count"),
            recent_items=[public_signal(item, public_id) for item in runtime.get("recent_signals", [])]),
    )


def public_quant_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """完整每日数据只匿名策略身份，恢复用冗余结构不直接输出。"""
    public_id = public_id_for_document(document)
    document = anonymize_document(document, public_id)
    data = public_overview(document, public_id).model_dump()
    for name in POOL_SERIALIZERS:
        items = public_pool(document, name, public_id)
        data[name] = {"count": len(items), "items": items}
    data["intraday_trading"]["interval"] = (document.get("intraday_trading") or {}).get("interval")
    accounts = public_accounts(document)
    data["accounts"] = {"available": (document.get("_runtime_state") or {}).get("accounts") is not None,
                        "count": len(accounts), "items": accounts}
    return DailySnapshot.model_validate(data).model_dump()
