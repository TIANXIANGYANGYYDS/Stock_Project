"""盘中影子交易的确定性三分钟重放逻辑。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping, Sequence

from app.quant.core.models import Bar
from app.quant.runtime.daily_flow import (
    DailyFlow,
    HoldingItem,
    IndependentAccount,
    PreselectionItem,
    SellCandidateItem,
    apply_trade_signal,
    close_daily_flow,
    create_daily_flow,
    mark_holdings,
    start_daily_flow,
)
from app.quant.runtime.daily_macd import (
    DailyMacdState,
    provisional_daily_indicator_from_state,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    MINIMUM_SHRINK_RATIO,
    confirm_provisional_histogram,
)


LIVE_RUNTIME_SCHEMA_VERSION = "2.0"
MAX_STORED_EXECUTION_ATTEMPTS = 10
LiveAction = Literal["buy", "sell", "hold"]


@dataclass(frozen=True)
class LiveObservationSpec:
    """一只股票在交易日开盘前冻结的指标判断输入。"""

    code: str
    name: str
    action: LiveAction
    observation_before_date: str
    observation_date: str
    previous_close: float
    reference_histogram: float
    previous_state: DailyMacdState | None
    adx: float | None = None
    adx_3_days_ago: float | None = None
    factor_completed_date: str | None = None
    factor_comparison_date: str | None = None


@dataclass(frozen=True)
class LiveThreeMinuteBar:
    """由三个连续实时一分钟柱组成、以结束时间标记的三分钟柱。"""

    start_at: str
    end_at: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float | None

    def indicator_bar(self) -> Bar:
        return Bar(
            trade_date=self.end_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
        )


def _session_minute_starts(trade_date: str) -> list[str]:
    starts: list[str] = []
    for hour, minutes in (
        (9, range(30, 60)),
        (10, range(60)),
        (11, range(30)),
        (13, range(60)),
        (14, range(60)),
    ):
        starts.extend(
            f"{trade_date}T{hour:02d}:{minute:02d}:00+08:00"
            for minute in minutes
        )
    return starts


def three_minute_bar_ends(trade_date: str) -> tuple[str, ...]:
    """返回A股连续竞价时段固定的80个三分钟结束时间。"""

    starts = _session_minute_starts(trade_date)
    return tuple(
        (
            datetime.fromisoformat(starts[index]) + timedelta(minutes=3)
        ).isoformat()
        for index in range(0, len(starts), 3)
    )


def expected_completed_bar_count(now: datetime, trade_date: str) -> int:
    """按北京时间计算当前最多应该完成的三分钟柱数量。"""

    if now.date().isoformat() < trade_date:
        return 0
    if now.date().isoformat() > trade_date:
        return EXPECTED_INTRADAY_BARS_PER_DAY
    ends = three_minute_bar_ends(trade_date)
    now_iso = now.isoformat()
    return sum(end_at <= now_iso for end_at in ends)


def next_evaluation_at(trade_date: str, completed_count: int) -> str | None:
    ends = three_minute_bar_ends(trade_date)
    if completed_count >= len(ends):
        return None
    return ends[max(completed_count, 0)]


def aggregate_complete_three_minute_bars(
    rows: Sequence[Mapping[str, Any]], *, trade_date: str
) -> tuple[LiveThreeMinuteBar, ...]:
    """只聚合从开盘开始连续完整的三分钟柱，遇到分钟缺口立即停止。"""

    by_timestamp = {str(row.get("timestamp")): row for row in rows}
    expected = _session_minute_starts(trade_date)
    output: list[LiveThreeMinuteBar] = []
    for index in range(0, len(expected), 3):
        timestamps = expected[index : index + 3]
        if any(timestamp not in by_timestamp for timestamp in timestamps):
            break
        chunk = [by_timestamp[timestamp] for timestamp in timestamps]
        prices = [
            float(item[field])
            for item in chunk
            for field in ("open", "high", "low", "close")
        ]
        if any(price <= 0 for price in prices):
            break
        previous_close = next(
            (
                float(item["previous_close"])
                for item in chunk
                if item.get("previous_close") is not None
                and float(item["previous_close"]) > 0
            ),
            None,
        )
        start = datetime.fromisoformat(timestamps[0])
        output.append(
            LiveThreeMinuteBar(
                start_at=start.isoformat(),
                end_at=(start + timedelta(minutes=3)).isoformat(),
                open=float(chunk[0]["open"]),
                high=max(float(item["high"]) for item in chunk),
                low=min(float(item["low"]) for item in chunk),
                close=float(chunk[-1]["close"]),
                previous_close=previous_close,
            )
        )
    return tuple(output)


def observation_spec_document(spec: LiveObservationSpec) -> dict[str, Any]:
    document = asdict(spec)
    return document


def observation_spec_from_document(document: Mapping[str, Any]) -> LiveObservationSpec:
    state = document["previous_state"]
    return LiveObservationSpec(
        code=str(document["code"]),
        name=str(document.get("name") or ""),
        action=str(document["action"]),  # type: ignore[arg-type]
        observation_before_date=str(document["observation_before_date"]),
        observation_date=str(document["observation_date"]),
        previous_close=float(document["previous_close"]),
        reference_histogram=float(document["reference_histogram"]),
        previous_state=DailyMacdState(
            fast_ema=float(state["fast_ema"]),
            slow_ema=float(state["slow_ema"]),
            dea=float(state["dea"]),
        ) if state else None,
        adx=document.get("adx"),
        adx_3_days_ago=document.get("adx_3_days_ago"),
        factor_completed_date=document.get("factor_completed_date"),
        factor_comparison_date=document.get("factor_comparison_date"),
    )


def opening_flow_document(flow: DailyFlow) -> dict[str, Any]:
    """保存日内重放所需的不可变开盘状态。"""

    return {
        "trade_date": flow.trade_date,
        "selection_date": flow.selection_date,
        "generated_at": flow.generated_at,
        "preselection": [asdict(item) for item in flow.preselection],
        "sell_candidates": [asdict(item) for item in flow.sell_candidates],
        "holdings": [asdict(item) for item in flow.holdings],
        "accounts": [asdict(item) for item in flow.accounts],
        "opening_total_assets": flow.opening_total_assets,
    }


def opening_flow_from_document(document: Mapping[str, Any]) -> DailyFlow:
    return create_daily_flow(
        trade_date=str(document["trade_date"]),
        selection_date=str(document["selection_date"]),
        generated_at=str(document["generated_at"]),
        candidates=[
            PreselectionItem(**dict(item))
            for item in document.get("preselection", [])
        ],
        sell_candidates=[
            SellCandidateItem(**dict(item))
            for item in document.get("sell_candidates", [])
        ],
        holdings=[
            HoldingItem(**dict(item)) for item in document.get("holdings", [])
        ],
        accounts=[IndependentAccount(**dict(item)) for item in document.get("accounts", [])],
        opening_total_assets=document.get("opening_total_assets"),
    )


def _signal_id(code: str, action: str, signal_at: str) -> str:
    compact_time = (
        signal_at.replace("-", "")
        .replace(":", "")
        .replace("+", "")
    )
    return f"{code}-{action}-{compact_time}"


def _scaled_state(
    spec: LiveObservationSpec, current_previous_close: float
) -> tuple[DailyMacdState, float, float]:
    factor = current_previous_close / spec.previous_close
    return (
        DailyMacdState(
            fast_ema=spec.previous_state.fast_ema * factor,
            slow_ema=spec.previous_state.slow_ema * factor,
            dea=spec.previous_state.dea * factor,
        ),
        spec.reference_histogram * factor,
        factor,
    )


def _snapshot_key(
    *,
    expected_bar_count: int,
    bars_by_code: Mapping[str, Sequence[LiveThreeMinuteBar]],
    closed: bool,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"{expected_bar_count}|{int(closed)}".encode("utf-8"))
    for code, bars in sorted(bars_by_code.items()):
        digest.update(f"|{code}".encode("utf-8"))
        for bar in bars:
            digest.update(
                f"{bar.end_at}:{bar.open:.8f}:{bar.high:.8f}:"
                f"{bar.low:.8f}:{bar.close:.8f}:{bar.previous_close}"
                .encode("utf-8")
            )
    return digest.hexdigest()


def _record_execution_attempt(
    signal: dict[str, Any], attempt: dict[str, Any]
) -> None:
    """保留最近的撮合尝试，避免跌停顺延使单日文档无限增长。"""

    signal["attempt_count"] = int(signal.get("attempt_count", 0)) + 1
    attempts = [dict(item) for item in signal.get("attempts", [])]
    attempts.append(attempt)
    signal["attempts"] = attempts[-MAX_STORED_EXECUTION_ATTEMPTS:]


def replay_live_day(
    *,
    opening_flow: DailyFlow,
    observation_specs: Sequence[LiveObservationSpec],
    opening_pending_signals: Sequence[Mapping[str, Any]],
    bars_by_code: Mapping[str, Sequence[LiveThreeMinuteBar]],
    expected_bar_count: int,
    close_market: bool,
    opening_exit_states: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """以真实完整柱确定性重放正式ADX14/E2策略，恢复跨日账户和延期状态。"""
    from app.quant.research.factors import FactorSnapshot
    from app.quant.strategies.provisional_daily_macd_3m.adx import (
        buy_allowed, e2_controller, liquidation_quote,
    )

    flow = opening_flow
    bars_by_code = {code: tuple(bars[:expected_bar_count]) for code, bars in bars_by_code.items()}
    specs = {item.code: item for item in observation_specs}
    signals = [dict(item) for item in opening_pending_signals]
    pending_statuses = {"pending_execution", "deferred_limit_down", "deferred_t1"}
    pending_by_code = {str(item["code"]): item for item in signals if item.get("status") in pending_statuses}
    signal_generated_codes = set(pending_by_code)
    inherited = opening_exit_states or {}
    snapshots, controllers, observations, scaled = {}, {}, {}, {}
    exit_decisions = []
    saved_hold_checks = set()
    holding_by_code = {h.code: h for h in flow.holdings}

    for spec in observation_specs:
        snapshot = FactorSnapshot(flow.trade_date, spec.factor_completed_date or spec.observation_date,
            {"adx_14": spec.adx, "adx_14_3_days_ago": spec.adx_3_days_ago})
        snapshots[spec.code] = snapshot
        if spec.code in holding_by_code:
            state = inherited.get(spec.code, {})
            controllers[spec.code] = e2_controller(snapshot,
                state="EXIT_PENDING" if spec.code in pending_by_code else str(state.get("state") or "HOLDING"),
                deferred_from=state.get("deferred_from"))
        code_bars = bars_by_code.get(spec.code, ())
        if code_bars and code_bars[0].previous_close is not None and spec.previous_state is not None:
            scaled[spec.code] = _scaled_state(spec, float(code_bars[0].previous_close))
        observations[spec.code] = {
            "code": spec.code, "name": spec.name, "action": spec.action,
            "state": "holding" if spec.action == "hold" else "watching",
            "data_status": "waiting_data", "observation_before_date": spec.observation_before_date,
            "observation_date": spec.observation_date, "reference_histogram": spec.reference_histogram,
            "provisional_histogram": None, "shrink_ratio": None, "condition_met": False,
            "consecutive_confirmations": 0, "required_confirmations": CONFIRMATION_BARS,
            "last_complete_bar_at": None, "signal_id": None, "reason": "等待第一根完整三分钟K线",
            "adx_14": spec.adx, "adx_14_3_days_ago": spec.adx_3_days_ago,
            "factor_completed_date": snapshot.completed_date,
            "factor_comparison_date": spec.factor_comparison_date,
            "adx_buy_allowed": buy_allowed(snapshot),
        }
    for code, signal in pending_by_code.items():
        observations.setdefault(code, {
            "code": code, "name": signal.get("name", ""), "action": signal["action"],
            "state": signal["status"], "data_status": "waiting_data",
            "consecutive_confirmations": CONFIRMATION_BARS, "last_complete_bar_at": signal.get("signal_at"),
            "signal_id": signal.get("signal_id"), "reason": "上一交易日信号等待下一根可交易K线",
        })
    maximum_bars = max((len(items) for items in bars_by_code.values()), default=0)
    first_bar_at = min((items[0].end_at for items in bars_by_code.values() if items), default=None)
    if first_bar_at is not None:
        flow = start_daily_flow(flow, started_at=first_bar_at)
    high_so_far, low_so_far = {}, {}

    for bar_index in range(maximum_bars):
        for code in sorted(set(specs) | set(pending_by_code)):
            code_bars = bars_by_code.get(code, ())
            if bar_index >= len(code_bars):
                continue
            bar = code_bars[bar_index]
            observation = observations[code]
            observation.update(data_status="fresh" if bar.previous_close is not None else "missing_previous_close",
                               last_complete_bar_at=bar.end_at)
            high_so_far[code] = max(high_so_far.get(code, bar.high), bar.high)
            low_so_far[code] = min(low_so_far.get(code, bar.low), bar.low)
            pending = pending_by_code.get(code)
            if pending is not None:
                if pending["signal_at"] > bar.start_at:
                    continue
                if bar.previous_close is None:
                    observation["reason"] = "缺少行情源前收盘价，禁止模拟撮合"
                    continue
                from app.quant.runtime.daily_flow import at_daily_price_limit, daily_price_limit
                action = str(pending["action"])
                holding = holding_by_code.get(code)
                attempt = {"attempt_at": bar.start_at, "execution_bar_end_at": bar.end_at,
                           "reference_open": bar.open}
                _record_execution_attempt(pending, attempt)
                if action == "sell" and holding and holding.entry_execution_at[:10] == flow.trade_date:
                    attempt["status"] = pending["status"] = "deferred_t1"
                    observation.update(state="deferred_t1", reason="T+1锁定，退出意图保留至下一交易日")
                    continue
                limit = daily_price_limit(action=action, code=code, name=pending.get("name", ""),
                    trade_date=flow.trade_date, previous_close=bar.previous_close)
                attempt["daily_price_limit"] = limit
                if at_daily_price_limit(action=action, code=code, name=pending.get("name", ""),
                        trade_date=flow.trade_date, previous_close=bar.previous_close, price=bar.open):
                    status = "rejected_limit_up" if action == "buy" else "deferred_limit_down"
                    attempt["status"] = pending["status"] = status
                    observation.update(state=status, reason="涨停取消买入" if action == "buy" else "跌停顺延卖出")
                    if action == "buy":
                        pending_by_code.pop(code)
                    continue
                try:
                    flow = apply_trade_signal(flow, action=action, code=code,
                        signal_at=pending["signal_at"], signal_price=float(pending["signal_price"]),
                        previous_close=bar.previous_close, execution_at=bar.start_at,
                        execution_reference_price=bar.open, execution_bar_low=bar.low, execution_bar_high=bar.high,
                        execution_price_source="next_3m_bar_open", reason=pending.get("reason", "原MACD确认"))
                except ValueError as exc:
                    if action != "buy" or "资金不足以买入一手" not in str(exc):
                        raise
                    attempt["status"] = pending["status"] = "rejected_insufficient_cash"
                    pending_by_code.pop(code)
                    observation.update(state=pending["status"], reason="该股独立账户资金不足一手")
                    continue
                execution = asdict(flow.executions[-1])
                holding_by_code = {h.code: h for h in flow.holdings}
                attempt["status"] = "filled"
                pending.update(status="filled", execution_at=execution["execution_at"],
                    execution_reference_price=execution["execution_reference_price"],
                    execution_price=execution["execution_price"], shares=execution["shares"])
                pending_by_code.pop(code)
                observation.update(state="filled", reason="信号已按下一根三分钟K线开盘模拟成交")
                if action == "buy":
                    controllers[code] = e2_controller(snapshots.get(code))
                elif code in controllers:
                    controllers[code].on_fill("sell")

            spec = specs.get(code)
            if spec is None:
                continue
            holding = holding_by_code.get(code)
            provisional = None
            reference_histogram, factor, shrink_ratio, consecutive = spec.reference_histogram, None, None, 0
            if code in scaled:
                state, reference_histogram, factor = scaled[code]
                provisional = provisional_daily_indicator_from_state(state, trade_date=flow.trade_date,
                    day_open=code_bars[0].open, high_so_far=high_so_far[code], low_so_far=low_so_far[code],
                    current_close=bar.close, config=flow.config)
            original_signal = False
            observes_original = (spec.action == "buy" and holding is None) or (
                spec.action == "sell" and holding is not None and holding.entry_execution_at[:10] < flow.trade_date)
            if observes_original and code not in signal_generated_codes and provisional is not None:
                qualifies, shrink_ratio = confirm_provisional_histogram(action=spec.action,
                    reference_histogram=reference_histogram, provisional_histogram=provisional.histogram)
                consecutive = int(observation["consecutive_confirmations"]) + 1 if qualifies else 0
                observation.update(reference_histogram=reference_histogram,
                    provisional_histogram=provisional.histogram, provisional_dif=provisional.dif,
                    shrink_ratio=shrink_ratio, adjustment_factor=factor, condition_met=qualifies,
                    consecutive_confirmations=consecutive, state="confirming" if qualifies else "watching",
                    reason=f"临时日线连续确认{consecutive}/{CONFIRMATION_BARS}")
                original_signal = consecutive >= CONFIRMATION_BARS

            decision = None
            if holding is not None and code in controllers:
                controller = controllers[code]
                decision = controller.decide(at=bar.end_at,
                    quote=liquidation_quote({"shares": holding.shares, "entry_notional": holding.entry_notional,
                                            "buy_commission": holding.buy_commission}, bar.indicator_bar(), flow.config),
                    dif=provisional.dif if provisional else None,
                    histogram=provisional.histogram if provisional else None,
                    original_sell=original_signal and spec.action == "sell", entry_at=holding.entry_execution_at)
                controller.rows.clear()
                check_key = (code, decision.get("state_after"), decision.get("data_anomaly"))
                if decision["action"] != "pending" and (decision["action"] != "hold" or check_key not in saved_hold_checks):
                    exit_decisions.append({"code": code, **decision})
                    saved_hold_checks.add(check_key)
                observation["exit_state"] = controller.state
                if controller.state == "DEFERRED_EXIT":
                    observation.update(state="deferred_exit", reason="E2延期：趋势、净盈利及DIF/H条件继续成立")
                if original_signal:
                    signal_generated_codes.add(code)
                if decision["action"] != "submit":
                    continue
                action, reason = "sell", decision["reason"]
                if not any(c.code == code for c in flow.sell_candidates):
                    flow = replace(flow, sell_candidates=(*flow.sell_candidates,
                        SellCandidateItem(code, spec.name, "E2延期资格失效，提交退出", bar.close)))
            elif original_signal and spec.action == "buy":
                action, reason = "buy", "原MACD三柱确认且ADX14趋势强"
            else:
                continue

            signal_id = _signal_id(code, action, bar.end_at)
            accepted = action == "sell" or buy_allowed(snapshots.get(code))
            signal = {
                "signal_id": signal_id, "code": code, "name": spec.name, "action": action,
                "observation_before_date": spec.observation_before_date, "observation_date": spec.observation_date,
                "reference_histogram": reference_histogram, "minimum_shrink_ratio": MINIMUM_SHRINK_RATIO,
                "signal_at": bar.end_at, "signal_price": bar.close,
                "provisional_dif": provisional.dif if provisional else None,
                "provisional_dea": provisional.dea if provisional else None,
                "provisional_histogram": provisional.histogram if provisional else None,
                "shrink_ratio": shrink_ratio, "confirmation_count": consecutive,
                "status": "pending_execution" if accepted else "rejected_adx",
                "reason": reason if accepted else "ADX14未达到20且较三个市场交易日前上升",
                "execution_at": None, "execution_price": None, "attempt_count": 0, "attempts": [],
                "adx_14": spec.adx, "adx_14_3_days_ago": spec.adx_3_days_ago,
                "factor_completed_date": spec.factor_completed_date,
                "factor_comparison_date": spec.factor_comparison_date,
            }
            if decision:
                signal.update(exit_reason=reason, deferred_from=decision["deferred_from"],
                              estimated_net_return=decision["estimated_net_return"])
            signals.append(signal)
            signal_generated_codes.add(code)
            if accepted:
                pending_by_code[code] = signal
            observation.update(state="signal_confirmed" if accepted else "rejected_adx",
                               signal_id=signal_id, reason=signal["reason"])

    # 每只持仓使用本股真实末柱时间，缺行情者沿用已有估值。
    for code, code_bars in bars_by_code.items():
        if code_bars and code in holding_by_code:
            last = code_bars[-1]
            previous = {code: float(last.previous_close)} if last.previous_close is not None else {}
            flow = mark_holdings(flow, prices={code: last.close}, previous_closes=previous, marked_at=last.end_at)
    latest_bar_at = max((items[-1].end_at for items in bars_by_code.values() if items), default=None)
    if latest_bar_at and flow.monitoring_started_at:
        flow = replace(flow, updated_at=latest_bar_at)
    if close_market:
        flow = close_daily_flow(flow, closed_at=f"{flow.trade_date}T15:00:00+08:00")
    incomplete_codes = tuple(sorted(code for code in observations
        if len(bars_by_code.get(code, ())) < expected_bar_count or (
            bars_by_code.get(code) and bars_by_code[code][0].previous_close is None)))
    exit_states = {h.code: {"state": controllers[h.code].state,
                           "deferred_from": controllers[h.code].deferred_from}
                   for h in flow.holdings if h.code in controllers}
    return {
        "flow": flow, "observations": tuple(observations[code] for code in sorted(observations)),
        "signals": tuple(signals), "pending_signals": tuple(pending_by_code.values()),
        "exit_states": exit_states, "exit_decisions": tuple(exit_decisions),
        "last_complete_bar_at": latest_bar_at,
        "complete_observation_count": len(observations) - len(incomplete_codes),
        "incomplete_codes": incomplete_codes,
        "snapshot_key": _snapshot_key(expected_bar_count=expected_bar_count, bars_by_code=bars_by_code, closed=close_market),
    }
