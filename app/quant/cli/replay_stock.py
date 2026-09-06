"""回放单只股票的盘中临时日线 MACD 买卖过程。"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence, Tuple

if TYPE_CHECKING:
    from app.quant.research.adx_exit import ExitController

from pymongo import ASCENDING, MongoClient

from app.core.config import PROJECT_ROOT, get_settings
from app.quant.core.execution import money
from app.quant.core.indicators import calculate_macd
from app.quant.core.models import BacktestConfig, Bar
from app.quant.data.market_data import (
    DAILY_HISTORY_COLLECTION,
    DEFAULT_ADJUST,
    THREE_MINUTE_HISTORY_COLLECTION,
)
from app.quant.runtime.daily_flow import at_daily_price_limit, daily_price_limit
from app.quant.runtime.daily_macd import (
    calculate_daily_macd_states,
    provisional_daily_indicator_from_state,
)
from app.quant.strategies.provisional_daily_macd_3m import (
    CONFIRMATION_BARS,
    EXPECTED_INTRADAY_BARS_PER_DAY,
    INTRADAY_INTERVAL,
    MINIMUM_SHRINK_RATIO,
    STRATEGY_ID,
    STRATEGY_LABEL,
    STRATEGY_VERSION,
    confirm_provisional_histogram,
    determine_observation_action,
    official_backtest_config,
)


DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / ".local" / "quant" / STRATEGY_ID / "traces"
)

BuySignalGate = Callable[[str, str], Tuple[bool, str]]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成单只股票盘中临时日线 MACD 的逐日完整审计回放。"
    )
    parser.add_argument("--code", required=True)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    return parser


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
        writer.writeheader()
        writer.writerows(rows)


def _load_daily_documents(
    collection: Any, *, code: str, through_date: str
) -> list[dict[str, Any]]:
    documents = list(
        collection.find(
            {
                "code": code,
                "adjust": DEFAULT_ADJUST,
                "trade_date": {"$lte": through_date},
            },
            {
                "_id": 0,
                "name": 1,
                "trade_date": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
            },
        ).sort("trade_date", ASCENDING)
    )
    if not documents:
        raise ValueError(f"{code} 没有可用的独立量化日线")
    return documents


def _load_minute_bars(
    collection: Any,
    *,
    code: str,
    start_date: str,
    end_date: str,
) -> dict[str, list[Bar]]:
    documents = list(
        collection.find(
            {
                "code": code,
                "adjust": DEFAULT_ADJUST,
                "interval": INTRADAY_INTERVAL,
                "trade_date": {"$gte": start_date, "$lte": end_date},
            },
            {
                "_id": 0,
                "trade_date": 1,
                "timestamp": 1,
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
            },
        ).sort("timestamp", ASCENDING)
    )
    bars = [
        Bar(
            trade_date=str(item["timestamp"]),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in documents
    ]
    by_date: dict[str, list[Bar]] = {}
    for bar in bars:
        by_date.setdefault(bar.trade_date[:10], []).append(bar)
    return by_date


def _threshold_histogram(action: str, reference: float, ratio: float) -> float:
    if action == "buy":
        return reference + abs(reference) * ratio
    return reference * (1.0 - ratio)


def _execution_price(
    *, action: str, reference: float, bar: Bar, slippage_rate: float
) -> float:
    slipped = reference * (
        1.0 + slippage_rate if action == "buy" else 1.0 - slippage_rate
    )
    return min(slipped, bar.high) if action == "buy" else max(slipped, bar.low)


def _buy_size(
    *, cash: float, execution_price: float, config: BacktestConfig
) -> tuple[int, float, float]:
    shares = int(cash / (execution_price * (1.0 + config.commission_rate)))
    shares = shares // config.lot_size * config.lot_size
    while shares > 0:
        notional = money(execution_price * shares)
        commission = money(notional * config.commission_rate)
        if money(notional + commission) <= cash:
            return shares, notional, commission
        shares -= config.lot_size
    return 0, 0.0, 0.0


def _state_label(position: dict[str, Any] | None) -> str:
    return "持仓" if position is not None else "空仓"


def _iso_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def replay(
    *,
    code: str,
    name: str,
    daily_bars: Sequence[Bar],
    minute_bars_by_date: dict[str, list[Bar]],
    start_date: str,
    end_date: str,
    config: BacktestConfig,
    buy_signal_gate: BuySignalGate | None = None,
    official_strategy_configuration: bool = True,
    exit_controller: ExitController | None = None,
    fixed_entry: dict[str, Any] | None = None,
    market_cache: dict[str, Any] | None = None,
    record_intraday: bool = True,
) -> dict[str, Any]:
    interval = INTRADAY_INTERVAL
    expected_bars = EXPECTED_INTRADAY_BARS_PER_DAY
    minimum_shrink_ratio = MINIMUM_SHRINK_RATIO
    confirmation_bars = CONFIRMATION_BARS
    # An optional, stock-local cache holds only immutable price indicators.
    # Cost, account state and exit decisions are always recomputed per replay.
    if market_cache is None:
        daily_indicators = calculate_macd(daily_bars, config)
        daily_states = calculate_daily_macd_states(daily_bars, config)
    else:
        if "daily_indicators" not in market_cache:
            market_cache["daily_indicators"] = calculate_macd(daily_bars, config)
            market_cache["daily_states"] = calculate_daily_macd_states(daily_bars, config)
        daily_indicators = market_cache["daily_indicators"]
        daily_states = market_cache["daily_states"]
    provisional_cache = market_cache.setdefault("provisional", {}) if market_cache is not None else {}
    if exit_controller is not None:
        from app.quant.research.adx_exit import liquidation_quote
    replay_indexes = [
        index
        for index, item in enumerate(daily_indicators)
        if start_date <= item.trade_date <= end_date
    ]
    if not replay_indexes:
        raise ValueError("指定范围内没有该股票日线")

    cash = config.initial_cash
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    realized_pnl = 0.0
    trade_id = 0
    daily_rows: list[dict[str, Any]] = []
    intraday_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    signal_by_id: dict[str, dict[str, Any]] = {}
    attempt_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    closed_trade_rows: list[dict[str, Any]] = []

    for daily_index in replay_indexes:
        if daily_index < 2:
            raise ValueError("日线历史不足以判断观察方向")
        current_daily = daily_indicators[daily_index]
        previous_previous = daily_indicators[daily_index - 2]
        previous = daily_indicators[daily_index - 1]
        current_bar = daily_bars[daily_index]
        trade_date = current_daily.trade_date
        bars = minute_bars_by_date.get(trade_date, [])
        if (
            position is not None
            and position["last_holding_trade_date"] != trade_date
        ):
            position["holding_trading_days"] += 1
            position["last_holding_trade_date"] = trade_date
        minute_close_matches = bool(
            bars and abs(bars[-1].close - current_daily.close) <= 0.011
        )
        warmed_up = daily_index + 1 >= config.warmup_bars
        raw_action = (
            determine_observation_action(previous_previous, previous)
            if warmed_up
            else None
        )
        cash_at_open = cash
        position_at_open = position.copy() if position is not None else None
        pending_at_open = pending["action"] if pending is not None else ""
        watch_action: str | None = None
        if not warmed_up:
            gate_reason = (
                f"日线预热不足：{daily_index + 1}/{config.warmup_bars}根"
            )
        elif pending is not None:
            gate_reason = f"已有{pending['action']}信号等待撮合"
        elif raw_action == "buy" and position is None:
            watch_action = "buy"
            gate_reason = "前一日绿柱继续变长，空仓，进入买入观察"
        elif raw_action == "buy":
            gate_reason = "出现买入观察形态，但账户已经持仓"
        elif raw_action == "sell" and position is None:
            gate_reason = "出现卖出观察形态，但账户没有持仓"
        elif raw_action == "sell" and position is not None:
            if position["entry_date"] == trade_date:
                gate_reason = "出现卖出观察形态，但持仓受T+1锁定"
            else:
                watch_action = "sell"
                gate_reason = "前一日红柱继续变长，持仓可卖，进入卖出观察"
        else:
            gate_reason = "前两根完整日线没有形成新的谷底或峰顶观察条件"

        if watch_action is not None and len(bars) != expected_bars:
            gate_reason += (
                f"；{interval}数据{len(bars)}/{expected_bars}，停止交易判断"
            )
            watch_action = None

        if fixed_entry is not None and watch_action == "buy":
            watch_action = None
            gate_reason = "同买入退出诊断：只注入指定实际买入，不允许其他买入"

        reference_histogram = previous.histogram if raw_action else None
        threshold_histogram = (
            _threshold_histogram(
                raw_action, previous.histogram, minimum_shrink_ratio
            )
            if raw_action
            else None
        )
        after_close_confirmed: bool | None = None
        after_close_shrink_ratio: float | None = None
        if raw_action is not None:
            after_close_confirmed, after_close_shrink_ratio = (
                confirm_provisional_histogram(
                    action=raw_action,
                    reference_histogram=previous.histogram,
                    provisional_histogram=current_daily.histogram,
                )
            )

        consecutive = 0
        max_consecutive = 0
        first_qualified_at: str | None = None
        signal_today: dict[str, Any] | None = None
        attempts_today: list[dict[str, Any]] = []
        fills_today: list[dict[str, Any]] = []
        signal_generated_today = False
        high_so_far = 0.0
        low_so_far = float("inf")

        for minute_index, minute_bar in enumerate(bars):
            high_so_far = max(high_so_far, minute_bar.high)
            low_so_far = min(low_so_far, minute_bar.low)

            if fixed_entry is not None and minute_bar.trade_date == fixed_entry["event"]["execution_at"]:
                entry = fixed_entry["event"]
                entry_signal = dict(fixed_entry["signal"])
                signal_rows.append(entry_signal)
                signal_by_id[entry_signal["signal_id"]] = entry_signal
                signal_today = entry_signal
                signal_generated_today = True
                cash = float(entry["cash_after"])
                position = {
                    "entry_signal_id": entry["signal_id"], "entry_signal_at": entry["signal_at"],
                    "entry_signal_price": entry["signal_price"], "entry_date": trade_date,
                    "entry_at": entry["execution_at"], "entry_reference_price": entry["execution_reference_price"],
                    "entry_execution_price": entry["execution_price"], "shares": entry["shares"],
                    "entry_notional": entry["notional"], "buy_commission": entry["commission"],
                    "lowest_price": entry["execution_price"], "highest_price": entry["execution_price"],
                    "holding_trading_days": 1, "last_holding_trade_date": trade_date,
                }
                event_rows.append(dict(entry))
                fills_today.append(dict(entry))
                if exit_controller is not None:
                    exit_controller.on_fill("buy")

            if pending is not None:
                action = str(pending["action"])
                limit = daily_price_limit(
                    action=action,  # type: ignore[arg-type]
                    code=code,
                    name=name,
                    trade_date=trade_date,
                    previous_close=previous.close,
                )
                at_limit = at_daily_price_limit(
                    action=action,  # type: ignore[arg-type]
                    code=code,
                    name=name,
                    trade_date=trade_date,
                    previous_close=previous.close,
                    price=minute_bar.open,
                )
                attempt = {
                    "signal_id": pending["signal_id"],
                    "code": code,
                    "name": name,
                    "action": action,
                    "attempt_at": minute_bar.trade_date,
                    "reference_open": minute_bar.open,
                    "bar_low": minute_bar.low,
                    "bar_high": minute_bar.high,
                    "previous_close": previous.close,
                    "daily_price_limit": limit,
                    "status": "",
                    "reason": "",
                }
                if (action == "sell" and position is not None
                        and position["entry_date"] == trade_date):
                    attempt["status"] = "deferred_t_plus_one"
                    attempt["reason"] = "持仓受T+1限制，保留原卖出意图"
                elif at_limit and action == "buy":
                    attempt["status"] = "rejected_limit_up"
                    attempt["reason"] = "撮合参考价达到涨停价，买入信号取消"
                    signal_by_id[pending["signal_id"]]["final_status"] = (
                        "rejected_limit_up"
                    )
                    signal_by_id[pending["signal_id"]]["execution_at"] = (
                        minute_bar.trade_date
                    )
                    pending = None
                elif at_limit:
                    attempt["status"] = "deferred_limit_down"
                    attempt["reason"] = "撮合参考价达到跌停价，保留原卖出信号"
                else:
                    execution_price = _execution_price(
                        action=action,
                        reference=minute_bar.open,
                        bar=minute_bar,
                        slippage_rate=config.slippage_rate,
                    )
                    cash_before = cash
                    if action == "buy":
                        if position is not None:
                            raise RuntimeError("待成交买入信号执行时账户已有持仓")
                        shares, notional, commission = _buy_size(
                            cash=cash,
                            execution_price=execution_price,
                            config=config,
                        )
                        if shares == 0:
                            attempt["status"] = "rejected_insufficient_cash"
                            attempt["reason"] = "账户现金不足一手"
                            signal_by_id[pending["signal_id"]]["final_status"] = (
                                "rejected_insufficient_cash"
                            )
                            signal_by_id[pending["signal_id"]]["execution_at"] = (
                                minute_bar.trade_date
                            )
                            pending = None
                            attempts_today.append(attempt)
                            attempt_rows.append(attempt)
                            continue
                        cash = money(cash - notional - commission)
                        position = {
                            "entry_signal_id": pending["signal_id"],
                            "entry_signal_at": pending["signal_at"],
                            "entry_signal_price": pending["signal_price"],
                            "entry_date": trade_date,
                            "entry_at": minute_bar.trade_date,
                            "entry_reference_price": minute_bar.open,
                            "entry_execution_price": execution_price,
                            "shares": shares,
                            "entry_notional": notional,
                            "buy_commission": commission,
                            "lowest_price": execution_price,
                            "highest_price": execution_price,
                            "holding_trading_days": 1,
                            "last_holding_trade_date": trade_date,
                        }
                        stamp_duty = 0.0
                        reason = "盘中临时日线绿柱连续缩短确认"
                    else:
                        if position is None:
                            raise RuntimeError("待成交卖出信号执行时账户没有持仓")
                        shares = int(position["shares"])
                        notional = money(execution_price * shares)
                        commission = money(notional * config.commission_rate)
                        stamp_duty = money(notional * config.stamp_duty_rate)
                        proceeds = money(notional - commission - stamp_duty)
                        cash = money(cash + proceeds)
                        entry_cost = money(
                            float(position["entry_notional"])
                            + float(position["buy_commission"])
                        )
                        entry_execution_price = float(
                            position["entry_execution_price"]
                        )
                        lowest_price = min(
                            float(position["lowest_price"]), execution_price
                        )
                        highest_price = max(
                            float(position["highest_price"]), execution_price
                        )
                        net_pnl = money(proceeds - entry_cost)
                        realized_pnl = money(realized_pnl + net_pnl)
                        trade_id += 1
                        closed_trade_rows.append(
                            {
                                "trade_id": trade_id,
                                "code": code,
                                "name": name,
                                "entry_signal_at": position["entry_signal_at"],
                                "entry_execution_at": position["entry_at"],
                                "entry_reference_price": position[
                                    "entry_reference_price"
                                ],
                                "entry_execution_price": position[
                                    "entry_execution_price"
                                ],
                                "exit_signal_at": pending["signal_at"],
                                "exit_execution_at": minute_bar.trade_date,
                                "exit_reference_price": minute_bar.open,
                                "exit_execution_price": execution_price,
                                "shares": shares,
                                "entry_notional": position["entry_notional"],
                                "exit_notional": notional,
                                "buy_commission": position["buy_commission"],
                                "sell_commission": commission,
                                "stamp_duty": stamp_duty,
                                "net_pnl": net_pnl,
                                "net_return": net_pnl / entry_cost,
                                "mae_return": (
                                    lowest_price / entry_execution_price - 1.0
                                ),
                                "mfe_return": (
                                    highest_price / entry_execution_price - 1.0
                                ),
                                "holding_calendar_days": (
                                    _iso_date(minute_bar.trade_date)
                                    - _iso_date(position["entry_at"])
                                ).days,
                                "holding_trading_days": position[
                                    "holding_trading_days"
                                ],
                            }
                        )
                        position = None
                        reason = "盘中临时日线红柱连续缩短确认"

                    event = {
                        "signal_id": pending["signal_id"],
                        "code": code,
                        "name": name,
                        "action": action,
                        "signal_at": pending["signal_at"],
                        "signal_price": pending["signal_price"],
                        "execution_at": minute_bar.trade_date,
                        "execution_reference_price": minute_bar.open,
                        "execution_bar_low": minute_bar.low,
                        "execution_bar_high": minute_bar.high,
                        "daily_price_limit": limit,
                        "slippage_rate": config.slippage_rate,
                        "execution_price": execution_price,
                        "shares": shares,
                        "notional": notional,
                        "commission": commission,
                        "stamp_duty": stamp_duty,
                        "cash_before": cash_before,
                        "cash_after": cash,
                        "reason": reason,
                    }
                    if exit_controller is not None:
                        if action == "sell":
                            event["exit_reason"] = pending.get("exit_reason", "original_macd")
                            event["deferred_from"] = pending.get("deferred_from")
                            closed_trade_rows[-1]["exit_reason"] = event["exit_reason"]
                            closed_trade_rows[-1]["deferred_from"] = event["deferred_from"]
                        exit_controller.on_fill(action)
                    event_rows.append(event)
                    fills_today.append(event)
                    attempt["status"] = "filled"
                    attempt["reason"] = reason
                    signal_by_id[pending["signal_id"]]["final_status"] = "filled"
                    signal_by_id[pending["signal_id"]]["execution_at"] = (
                        minute_bar.trade_date
                    )
                    signal_by_id[pending["signal_id"]]["execution_price"] = (
                        execution_price
                    )
                    pending = None
                attempts_today.append(attempt)
                attempt_rows.append(attempt)

            if position is not None:
                position["lowest_price"] = min(
                    float(position["lowest_price"]), minute_bar.low
                )
                position["highest_price"] = max(
                    float(position["highest_price"]), minute_bar.high
                )

            original_sell_now = False
            exit_check = (exit_controller is not None and position is not None
                          and pending is None and warmed_up and len(bars) == expected_bars)
            if watch_action is None and not exit_check:
                continue
            provisional = provisional_cache.get(minute_bar.trade_date)
            if provisional is None:
                provisional = provisional_daily_indicator_from_state(
                    daily_states[daily_index - 1],
                    trade_date=trade_date,
                    day_open=bars[0].open,
                    high_so_far=high_so_far,
                    low_so_far=low_so_far,
                    current_close=minute_bar.close,
                    config=config,
                )
                provisional_cache[minute_bar.trade_date] = provisional
            if watch_action is not None:
                qualifies, shrink_ratio = confirm_provisional_histogram(
                    action=watch_action,  # type: ignore[arg-type]
                    reference_histogram=previous.histogram,
                    provisional_histogram=provisional.histogram,
                )
                if signal_generated_today or (exit_controller is not None and pending is not None and not original_sell_now):
                    decision = "当日已经发出信号，不重复发出"
                elif qualifies:
                    consecutive += 1
                    max_consecutive = max(max_consecutive, consecutive)
                    first_qualified_at = first_qualified_at or minute_bar.trade_date
                    if consecutive >= confirmation_bars:
                        signal_id = (
                            f"{code}-{watch_action}-{minute_bar.trade_date}"
                            .replace(":", "")
                            .replace("+", "")
                        )
                        original_sell_now = watch_action == "sell"
                        signal_today = {
                            "signal_id": signal_id,
                            "code": code,
                            "name": name,
                            "action": watch_action,
                            "observation_before_date": previous_previous.trade_date,
                            "observation_date": previous.trade_date,
                            "reference_histogram": previous.histogram,
                            "minimum_shrink_ratio": minimum_shrink_ratio,
                            "threshold_histogram": threshold_histogram,
                            "signal_at": minute_bar.trade_date,
                            "signal_price": minute_bar.close,
                            "provisional_dif": provisional.dif,
                            "provisional_dea": provisional.dea,
                            "provisional_histogram": provisional.histogram,
                            "confirmation_count": consecutive,
                            "final_daily_histogram": current_daily.histogram,
                            "final_daily_confirmed": after_close_confirmed,
                            "final_status": "pending",
                            "execution_at": "",
                            "execution_price": "",
                        }
                        gate_passed = True
                        if watch_action == "buy" and buy_signal_gate is not None:
                            gate_passed, factor_gate_reason = buy_signal_gate(
                                code, trade_date
                            )
                            signal_today["factor_gate_passed"] = gate_passed
                            signal_today["factor_gate_reason"] = factor_gate_reason
                            if not gate_passed:
                                signal_today["final_status"] = "rejected_factor"
                        signal_rows.append(signal_today)
                        signal_by_id[signal_id] = signal_today
                        signal_generated_today = True
                        if gate_passed:
                            pending = signal_today
                            decision = (
                                f"连续第{consecutive}根满足，发出{watch_action}信号"
                            )
                        else:
                            decision = (
                                f"连续第{consecutive}根满足，MACD买入信号被因子过滤"
                            )
                    else:
                        decision = f"连续第{consecutive}/{confirmation_bars}根满足"
                else:
                    consecutive = 0
                    decision = "未达到缩短阈值，连续计数归零"
                if record_intraday:
                    intraday_rows.append(
                        {
                            "trade_date": trade_date,
                            "timestamp": minute_bar.trade_date,
                            "code": code,
                            "name": name,
                            "action": watch_action,
                            "observation_before_date": previous_previous.trade_date,
                            "observation_date": previous.trade_date,
                            "reference_histogram": previous.histogram,
                            "threshold_histogram": threshold_histogram,
                            "bar_open": minute_bar.open,
                            "bar_high": minute_bar.high,
                            "bar_low": minute_bar.low,
                            "bar_close": minute_bar.close,
                            "provisional_dif": provisional.dif,
                            "provisional_dea": provisional.dea,
                            "provisional_histogram": provisional.histogram,
                            "shrink_ratio": shrink_ratio,
                            "minimum_shrink_ratio": minimum_shrink_ratio,
                            "qualifies": qualifies,
                            "consecutive_count": consecutive,
                            "required_consecutive_count": confirmation_bars,
                            "decision": decision,
                        }
                    )

            if (exit_controller is not None and position is not None
                    and warmed_up and len(bars) == expected_bars
                    and (pending is None or original_sell_now)):
                quoted = liquidation_quote(position, minute_bar, config)
                exit_decision = exit_controller.decide(
                    at=minute_bar.trade_date, quote=quoted, dif=provisional.dif,
                    histogram=provisional.histogram, original_sell=original_sell_now,
                    entry_at=position["entry_at"],
                )
                if (exit_decision["action"] == "defer" or
                        (original_sell_now and exit_decision["state_before"] == "DEFERRED_EXIT"
                         and exit_decision["action"] == "hold")):
                    signal_today["final_status"] = "deferred_exit"
                    pending = None
                elif exit_decision["action"] == "submit":
                    if original_sell_now:
                        pending["exit_reason"] = exit_decision["reason"]
                        pending["deferred_from"] = exit_decision["deferred_from"]
                    else:
                        exit_id = f"{code}-research-sell-{minute_bar.trade_date}"
                        signal_today = {
                            "signal_id": exit_id, "code": code, "name": name, "action": "sell",
                            "signal_at": minute_bar.trade_date, "signal_price": minute_bar.close,
                            "provisional_dif": provisional.dif, "provisional_dea": provisional.dea,
                            "provisional_histogram": provisional.histogram,
                            "reference_histogram": previous.histogram,
                            "threshold_histogram": None, "confirmation_count": 0,
                            "final_daily_histogram": None, "final_daily_confirmed": None,
                            "final_status": "pending", "execution_at": "", "execution_price": "",
                            "exit_reason": exit_decision["reason"],
                            "deferred_from": exit_decision["deferred_from"],
                            "estimated_net_return": quoted["estimated_net_return"],
                        }
                        signal_rows.append(signal_today)
                        signal_by_id[exit_id] = signal_today
                        pending = signal_today
                    signal_generated_today = True

        mark_price = current_daily.close
        market_value = money(
            float(position["shares"]) * mark_price if position is not None else 0.0
        )
        unrealized_pnl = money(
            market_value
            - float(position["entry_notional"])
            - float(position["buy_commission"])
            if position is not None
            else 0.0
        )
        total_assets = money(cash + market_value)
        signal_id_today = signal_today["signal_id"] if signal_today else ""
        daily_rows.append(
            {
                "trade_date": trade_date,
                "code": code,
                "name": name,
                "daily_open": current_daily.open,
                "daily_high": current_daily.high,
                "daily_low": current_daily.low,
                "daily_close": current_daily.close,
                "daily_dif_after_close": current_daily.dif,
                "daily_dea_after_close": current_daily.dea,
                "daily_histogram_after_close": current_daily.histogram,
                "previous_previous_date": previous_previous.trade_date,
                "previous_previous_histogram": previous_previous.histogram,
                "previous_date": previous.trade_date,
                "previous_histogram": previous.histogram,
                "raw_observation_action": raw_action or "none",
                "reference_histogram": reference_histogram,
                "threshold_histogram": threshold_histogram,
                "account_state_at_open": _state_label(position_at_open),
                "cash_at_open": cash_at_open,
                "shares_at_open": (
                    position_at_open["shares"] if position_at_open else 0
                ),
                "pending_action_at_open": pending_at_open,
                "monitored_action": watch_action or "none",
                "gate_reason": gate_reason,
                "minute_bar_count": len(bars),
                "expected_minute_bar_count": expected_bars,
                "minute_close_matches_daily": minute_close_matches,
                "first_qualified_at": first_qualified_at or "",
                "max_consecutive_count": max_consecutive,
                "signal_id": signal_id_today,
                "signal_at": signal_today["signal_at"] if signal_today else "",
                "signal_price": (
                    signal_today["signal_price"] if signal_today else ""
                ),
                "provisional_histogram_at_signal": (
                    signal_today["provisional_histogram"] if signal_today else ""
                ),
                "after_close_shrink_ratio": after_close_shrink_ratio,
                "after_close_confirmed": after_close_confirmed,
                "execution_attempt_count_today": len(attempts_today),
                "execution_statuses_today": ",".join(
                    str(item["status"]) for item in attempts_today
                ),
                "filled_action_today": ",".join(
                    str(item["action"]) for item in fills_today
                ),
                "filled_at_today": ",".join(
                    str(item["execution_at"]) for item in fills_today
                ),
                "account_state_at_close": _state_label(position),
                "cash_at_close": cash,
                "shares_at_close": position["shares"] if position else 0,
                "mark_price": mark_price if position else "",
                "market_value": market_value,
                "realized_pnl_cumulative": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_assets": total_assets,
                "total_return": total_assets / config.initial_cash - 1.0,
                "pending_action_at_close": pending["action"] if pending else "",
                "signal_final_status": "",
                "signal_execution_at": "",
                "signal_execution_price": "",
            }
        )

        if exit_controller is not None:
            daily_rows[-1]["exit_state_at_close"] = exit_controller.state

    for row in daily_rows:
        signal_id = str(row["signal_id"])
        if signal_id:
            signal = signal_by_id[signal_id]
            row["signal_final_status"] = signal["final_status"]
            row["signal_execution_at"] = signal["execution_at"]
            row["signal_execution_price"] = signal["execution_price"]

    final_mark = daily_indicators[replay_indexes[-1]].close
    final_market_value = money(
        float(position["shares"]) * final_mark if position is not None else 0.0
    )
    final_assets = money(cash + final_market_value)
    false_intraday_signals = sum(
        item["final_daily_confirmed"] is False for item in signal_rows
    )
    monitored_rows = [
        item for item in daily_rows if item["monitored_action"] != "none"
    ]
    incomplete_minute_dates = [
        str(item["trade_date"])
        for item in daily_rows
        if int(item["minute_bar_count"]) != expected_bars
    ]
    incomplete_monitored_dates = [
        str(item["trade_date"])
        for item in monitored_rows
        if int(item["minute_bar_count"]) != expected_bars
    ]
    summary = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": "1.0.0" if buy_signal_gate is None and exit_controller is None else STRATEGY_VERSION,
        "strategy_name": "盘中临时日线 MACD 三分钟拐点" if buy_signal_gate is None and exit_controller is None else STRATEGY_LABEL,
        "official_strategy_configuration": official_strategy_configuration,
        "code": code,
        "name": name,
        "start_date": daily_indicators[replay_indexes[0]].trade_date,
        "end_date": daily_indicators[replay_indexes[-1]].trade_date,
        "daily_macd_parameters": [
            config.fast_period,
            config.slow_period,
            config.signal_period,
        ],
        "intraday_check_interval": interval,
        "expected_intraday_bars_per_day": expected_bars,
        "minimum_shrink_ratio": minimum_shrink_ratio,
        "confirmation_bars": confirmation_bars,
        "initial_cash": config.initial_cash,
        "slippage_rate": config.slippage_rate,
        "daily_count": len(replay_indexes),
        "complete_minute_day_count": sum(
            len(minute_bars_by_date.get(daily_indicators[index].trade_date, []))
            == expected_bars
            for index in replay_indexes
        ),
        "incomplete_minute_dates": incomplete_minute_dates,
        "monitored_day_count": len(monitored_rows),
        "complete_monitored_minute_day_count": sum(
            int(item["minute_bar_count"]) == expected_bars
            for item in monitored_rows
        ),
        "incomplete_monitored_dates": incomplete_monitored_dates,
        "buy_observation_day_count": sum(
            item["raw_observation_action"] == "buy" for item in daily_rows
        ),
        "sell_observation_day_count": sum(
            item["raw_observation_action"] == "sell" for item in daily_rows
        ),
        "monitored_buy_day_count": sum(
            item["monitored_action"] == "buy" for item in daily_rows
        ),
        "monitored_sell_day_count": sum(
            item["monitored_action"] == "sell" for item in daily_rows
        ),
        "buy_signal_count": sum(item["action"] == "buy" for item in signal_rows),
        "sell_signal_count": sum(item["action"] == "sell" for item in signal_rows),
        "false_intraday_signal_count": false_intraday_signals,
        "filled_buy_count": sum(item["action"] == "buy" for item in event_rows),
        "filled_sell_count": sum(item["action"] == "sell" for item in event_rows),
        "rejected_limit_up_buy_count": sum(
            item["status"] == "rejected_limit_up" for item in attempt_rows
        ),
        "deferred_limit_down_attempt_count": sum(
            item["status"] == "deferred_limit_down" for item in attempt_rows
        ),
        "closed_trade_count": len(closed_trade_rows),
        "winning_closed_trade_count": sum(
            float(item["net_pnl"]) > 0 for item in closed_trade_rows
        ),
        "realized_pnl": realized_pnl,
        "end_holding": position is not None,
        "end_shares": position["shares"] if position else 0,
        "end_mark_price": final_mark if position else None,
        "end_cash": cash,
        "end_market_value": final_market_value,
        "final_assets": final_assets,
        "total_pnl": money(final_assets - config.initial_cash),
        "total_return": final_assets / config.initial_cash - 1.0,
        "pending_action_at_end": pending["action"] if pending else None,
    }
    if money(summary["realized_pnl"] + daily_rows[-1]["unrealized_pnl"]) != money(
        summary["total_pnl"]
    ):
        raise RuntimeError("总资产无法由已实现和未实现盈亏回算")
    return {
        "summary": summary,
        "daily_rows": daily_rows,
        "intraday_rows": intraday_rows,
        "signal_rows": signal_rows,
        "attempt_rows": attempt_rows,
        "event_rows": event_rows,
        "closed_trade_rows": closed_trade_rows,
        **({"exit_decision_rows": exit_controller.rows,
            "exit_state_at_end": exit_controller.state} if exit_controller is not None else {}),
    }


def _text_action(action: object) -> str:
    return {"buy": "买入", "sell": "卖出", "none": "无"}.get(
        str(action), str(action)
    )


def render_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    daily_rows = result["daily_rows"]
    signal_rows = result["signal_rows"]
    event_rows = result["event_rows"]
    closed_rows = result["closed_trade_rows"]
    lines = [
        f"# {summary['code']} {summary['name']}：{summary['intraday_check_interval']}盘中临时日线 MACD 完整回放",
        "",
        f"- 策略：`{summary['strategy_id']}` v{summary['strategy_version']}（正式配置）",
        "",
        "## 策略口径",
        "",
        "- 前一日绿柱比再前一日更长：空仓股票在当日进入买入观察；前一日红柱比再前一日更长：已有持仓在当日进入卖出观察。",
        f"- 每根完整{summary['intraday_check_interval']}K线收盘后，只把当前价格作为当天的临时日线收盘价，并从上一日冻结的日线状态独立计算 MACD(20,100,30)。",
        f"- 临时柱体相对前一日缩短至少 `{summary['minimum_shrink_ratio']:.2%}`，连续 `{summary['confirmation_bars']}` 根成立后发出信号，下一根{summary['intraday_check_interval']}K线开盘撮合。",
        "- 买入参考价达到涨停价则取消；卖出参考价达到跌停价则保留原信号并顺延；买入当日遵守T+1不能卖出。",
        "- `daily_histogram_after_close` 只用于收盘后复盘，不参与盘中决策，可据此识别盘中信号收盘后是否失效。",
        "- 回放窗口开始时为空仓，每只股票独立本金10万元。",
        "",
        "## 结果汇总",
        "",
        f"- 交易日：`{summary['start_date']}` 至 `{summary['end_date']}`，共 `{summary['daily_count']}` 日；完整{summary['intraday_check_interval']}数据 `{summary['complete_minute_day_count']}` 日。",
        f"- 真正进入盘中监控的 `{summary['monitored_day_count']}` 日要求每天拥有{summary['expected_intraday_bars_per_day']}根{summary['intraday_check_interval']}K线；监控日数据缺口：`{summary['incomplete_monitored_dates'] or '无'}`。",
        f"- 非监控日的{summary['intraday_check_interval']}缺口日期：`{summary['incomplete_minute_dates'] or '无'}`；这些日期仍保留完整日线判断，但不参与盘中信号与收益计算。",
        f"- 实际监控：买入 `{summary['monitored_buy_day_count']}` 日，卖出 `{summary['monitored_sell_day_count']}` 日。",
        f"- 盘中信号：买入 `{summary['buy_signal_count']}` 次，卖出 `{summary['sell_signal_count']}` 次；其中收盘后不再满足的盘中信号 `{summary['false_intraday_signal_count']}` 次。",
        f"- 成交：买入 `{summary['filled_buy_count']}` 次，卖出 `{summary['filled_sell_count']}` 次；涨停拒绝买入 `{summary['rejected_limit_up_buy_count']}` 次，跌停顺延尝试 `{summary['deferred_limit_down_attempt_count']}` 次。",
        f"- 已实现盈亏 `{summary['realized_pnl']:.2f}` 元；期末现金 `{summary['end_cash']:.2f}` 元，持仓市值 `{summary['end_market_value']:.2f}` 元。",
        f"- 期末总资产 `{summary['final_assets']:.2f}` 元，总盈亏 `{summary['total_pnl']:.2f}` 元，总收益率 `{summary['total_return']:.4%}`。",
        "",
        "## 每日完整判断",
        "",
        "| 日期 | 收盘/HIST | 前一日HIST | 原始观察 | 开盘账户 | 实际监控 | 盘中信号 | 收盘确认 | 当日成交 | 收盘账户/总资产 |",
        "| --- | ---: | ---: | --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for row in daily_rows:
        signal = (
            f"{str(row['signal_at'])[11:16]} {row['signal_final_status']}"
            if row["signal_at"]
            else "-"
        )
        fills = (
            f"{_text_action(row['filled_action_today'])} {str(row['filled_at_today'])[11:16]}"
            if row["filled_action_today"]
            else "-"
        )
        close_confirmation = (
            "是" if row["after_close_confirmed"] is True else "否"
            if row["after_close_confirmed"] is False
            else "-"
        )
        lines.append(
            "| {date} | {close:.2f}/{hist:+.6f} | {previous:+.6f} | {raw} | "
            "{open_state} | {watch} | {signal} | {confirmed} | {fills} | "
            "{close_state}/{assets:.2f} |".format(
                date=row["trade_date"],
                close=float(row["daily_close"]),
                hist=float(row["daily_histogram_after_close"]),
                previous=float(row["previous_histogram"]),
                raw=_text_action(row["raw_observation_action"]),
                open_state=row["account_state_at_open"],
                watch=_text_action(row["monitored_action"]),
                signal=signal,
                confirmed=close_confirmation,
                fills=fills,
                close_state=row["account_state_at_close"],
                assets=float(row["total_assets"]),
            )
        )

    lines.extend(["", "## 信号与成交", ""])
    if not signal_rows:
        lines.append("本区间没有信号。")
    else:
        events_by_signal = {item["signal_id"]: item for item in event_rows}
        lines.extend(
            [
                "| 方向 | 观察基准 | 信号时间 | 信号价/HIST | 收盘仍确认 | 最终状态 | 成交时间/价格 |",
                "| --- | --- | --- | ---: | --- | --- | ---: |",
            ]
        )
        for signal in signal_rows:
            event = events_by_signal.get(signal["signal_id"])
            execution = (
                f"{event['execution_at']} / {float(event['execution_price']):.6f}"
                if event
                else str(signal["execution_at"] or "-")
            )
            lines.append(
                f"| {_text_action(signal['action'])} | {signal['observation_date']} "
                f"{float(signal['reference_histogram']):+.6f} | {signal['signal_at']} | "
                f"{float(signal['signal_price']):.2f}/{float(signal['provisional_histogram']):+.6f} | "
                f"{'是' if signal['final_daily_confirmed'] else '否'} | {signal['final_status']} | {execution} |"
            )

    lines.extend(["", "## 闭合交易", ""])
    if not closed_rows:
        lines.append("本区间没有闭合交易，期末持仓只按收盘价估值。")
    else:
        lines.extend(
            [
                "| 笔次 | 买入成交 | 卖出成交 | 股数 | 净盈亏 | 净收益率 |",
                "| ---: | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for item in closed_rows:
            lines.append(
                f"| {item['trade_id']} | {item['entry_execution_at']} @ {float(item['entry_execution_price']):.6f} | "
                f"{item['exit_execution_at']} @ {float(item['exit_execution_price']):.6f} | "
                f"{item['shares']} | {float(item['net_pnl']):.2f} | {float(item['net_return']):.4%} |"
            )

    lines.extend(
        [
            "",
            "## 明细文件",
            "",
            "- `daily_judgements.csv`：每个交易日的日线条件、账户门控、盘中结果、收盘确认和资产。",
            f"- `intraday_{summary['intraday_check_interval']}_checks.csv`：所有实际观察日的每根{summary['intraday_check_interval']}临时日线 MACD、缩短比例和连续计数。",
            "- `signals.csv`：所有盘中确认信号及其最终成交/拒绝状态。",
            "- `execution_attempts.csv`：每次撮合尝试及涨跌停处理。",
            "- `trade_events.csv`、`closed_trades.csv`：真实成交和闭合交易。",
            "",
        ]
    )
    return "\n".join(lines)


def replay_official(*, market_dates: Sequence[str], **kwargs) -> dict[str, Any]:
    """正式回放固定启用ADX14买入与E2；研究对照继续显式调用replay。"""
    from app.quant.research.adx_exit import ExitController, ExitVariant
    from app.quant.strategies.provisional_daily_macd_3m.adx import buy_allowed, daily_adx_snapshot

    dates = sorted(set(market_dates))
    snapshots = {}
    for index, day in enumerate(dates):
        if kwargs["start_date"] <= day <= kwargs["end_date"] and index >= 4:
            snapshots[day] = daily_adx_snapshot(bars=kwargs["daily_bars"], trade_date=day,
                completed_date=dates[index - 1], comparison_date=dates[index - 4])
    result = replay(**kwargs,
        buy_signal_gate=lambda code, day: (buy_allowed(snapshots.get(day)), "ADX14≥20且较3日前上升"),
        exit_controller=ExitController(ExitVariant(14, "E2"), snapshots))
    result["summary"].update(strategy_version=STRATEGY_VERSION, strategy_name=STRATEGY_LABEL,
                             buy_filter="ADX14[t-1]>=20 and ADX14[t-1]>ADX14[t-4]", exit_policy="E2")
    for signal in result["signal_rows"]:
        day_index = dates.index(signal["signal_at"][:10])
        defaults = {"factor_gate_passed": None, "factor_gate_reason": None,
                    "exit_reason": None, "deferred_from": None, "estimated_net_return": None,
                    "observation_date": dates[day_index - 1] if day_index else None,
                    "observation_before_date": dates[day_index - 2] if day_index >= 2 else None,
                    "minimum_shrink_ratio": MINIMUM_SHRINK_RATIO}
        for key, value in defaults.items():
            signal.setdefault(key, value)
    for event in result["event_rows"]:
        event.setdefault("exit_reason", None)
        event.setdefault("deferred_from", None)
    return result


def run(args: argparse.Namespace) -> Path:
    if len(args.code) != 6 or not args.code.isdigit():
        raise ValueError("code必须是六位数字")
    if args.start_date > args.end_date:
        raise ValueError("start-date不能晚于end-date")

    config = official_backtest_config(code=args.code)
    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5_000)
    try:
        database = client[settings.mongo_db_name]
        daily_documents = _load_daily_documents(
            database[DAILY_HISTORY_COLLECTION],
            code=args.code,
            through_date=args.end_date.isoformat(),
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
            code=args.code,
            start_date=args.start_date.isoformat(),
            end_date=args.end_date.isoformat(),
        )
        name = str(daily_documents[-1].get("name") or args.code)
        market_dates = database[DAILY_HISTORY_COLLECTION].distinct("trade_date", {
            "adjust": DEFAULT_ADJUST, "trade_date": {"$lte": args.end_date.isoformat()}})
        result = replay_official(
            market_dates=market_dates,
            code=args.code,
            name=name,
            daily_bars=daily_bars,
            minute_bars_by_date=minute_bars,
            start_date=args.start_date.isoformat(),
            end_date=args.end_date.isoformat(),
            config=config,
        )
    finally:
        client.close()

    _exit_rows = result.get("exit_decision_rows", [])
    actual_start = result["summary"]["start_date"]
    actual_end = result["summary"]["end_date"]
    output_directory = (
        args.output_root
        / args.code
        / f"{actual_start}_{actual_end}"
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    _write_csv(output_directory / "exit_decisions.csv", _exit_rows)
    _write_csv(output_directory / "daily_judgements.csv", result["daily_rows"])
    _write_csv(
        output_directory / "intraday_3m_checks.csv",
        result["intraday_rows"],
    )
    _write_csv(output_directory / "signals.csv", result["signal_rows"])
    _write_csv(
        output_directory / "execution_attempts.csv", result["attempt_rows"]
    )
    _write_csv(output_directory / "trade_events.csv", result["event_rows"])
    _write_csv(
        output_directory / "closed_trades.csv", result["closed_trade_rows"]
    )
    (output_directory / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path = output_directory / f"{args.code}_trade_journey.md"
    report_path.write_text(render_report(result), encoding="utf-8")
    return report_path


def main() -> None:
    report_path = run(build_argument_parser().parse_args())
    print(f"quant_stock_replay_finished report={report_path}", flush=True)


if __name__ == "__main__":
    main()
