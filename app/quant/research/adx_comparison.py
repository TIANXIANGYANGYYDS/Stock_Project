"""ADX周期分歧、入组交易观察与独立账户增量分解。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from statistics import mean, median
from typing import Any, Sequence

from app.quant.core.execution import money
from app.quant.research.evaluation import trade_metrics
from app.quant.research.factors import FactorSnapshot
from app.quant.research.scenarios import ADX_COMPARISON_SCENARIOS

ADX21, ADX14 = ADX_COMPARISON_SCENARIOS[:2]
CROSS_GROUPS = ("both_pass", "only_adx14", "only_adx21", "both_reject", "missing_factor")


def adx_cross_group(snapshot: FactorSnapshot | None) -> str:
    if not ADX14.is_available(snapshot) or not ADX21.is_available(snapshot):
        return "missing_factor"
    accepted14, accepted21 = ADX14.accepts(snapshot), ADX21.accepts(snapshot)
    if accepted14 and accepted21:
        return "both_pass"
    if accepted14:
        return "only_adx14"
    return "only_adx21" if accepted21 else "both_reject"


def cohort_trades(
    result: dict[str, Any], *, entry_start: str, entry_end: str,
    observation_end: str, minute_bars: dict[str, Sequence[Any]],
) -> list[dict[str, Any]]:
    """按原信号日期入组；开仓未退出者保留实际市值，不制造退出交易。"""
    rows = []
    closed_ids = {row["entry_signal_at"] for row in result["closed_trade_rows"]}
    for row in result["closed_trade_rows"]:
        if entry_start <= row["entry_signal_at"][:10] <= entry_end:
            rows.append({
                **row, "outcome": "closed", "observation_end": observation_end,
                "mark_date": None, "market_value": None, "marked_return": None,
                "unrealized_pnl": None, "asof_return": row["net_return"],
            })
    open_buys = [
        row for row in result["event_rows"]
        if row["action"] == "buy" and row["signal_at"] not in closed_ids
    ]
    if len(open_buys) != int(result["summary"]["end_holding"]):
        raise ValueError("未闭合买入与原回放期末持仓不一致")
    for buy in open_buys:
        if not entry_start <= buy["signal_at"][:10] <= entry_end:
            continue
        summary = result["summary"]
        price = float(buy["execution_price"])
        bars = [bar for day, items in minute_bars.items()
                if buy["execution_at"][:10] <= day <= observation_end
                for bar in items if bar.trade_date >= buy["execution_at"]]
        low = min([price] + [bar.low for bar in bars])
        high = max([price] + [bar.high for bar in bars])
        cost = money(buy["notional"] + buy["commission"])
        unrealized = money(summary["end_market_value"] - cost)
        holding_dates = [row["trade_date"] for row in result["daily_rows"]
                         if row["trade_date"] >= buy["execution_at"][:10]]
        rows.append({
            "code": buy["code"], "name": buy["name"],
            "entry_signal_at": buy["signal_at"], "entry_execution_at": buy["execution_at"],
            "entry_execution_price": price, "shares": buy["shares"],
            "entry_notional": buy["notional"], "buy_commission": buy["commission"],
            "exit_execution_at": None, "net_pnl": None, "net_return": None,
            "outcome": "open", "observation_end": observation_end,
            "mark_date": result["daily_rows"][-1]["trade_date"],
            "mark_price": summary["end_mark_price"], "market_value": summary["end_market_value"],
            "unrealized_pnl": unrealized, "marked_return": unrealized / cost,
            "asof_return": unrealized / cost, "mae_return": low / price - 1,
            "mfe_return": high / price - 1, "holding_trading_days": len(holding_dates),
            "holding_calendar_days": (date.fromisoformat(holding_dates[-1]) - date.fromisoformat(buy["execution_at"][:10])).days,
        })
    return rows


def cohort_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if row["outcome"] == "closed"]
    opened = [row for row in rows if row["outcome"] == "open"]
    losses = sum(float(row["net_return"]) < -.1 for row in closed)
    return {
        "entry_trade_count": len(rows), "stock_count": len({row["code"] for row in rows}),
        "closed_count": len(closed), "open_count": len(opened),
        "closed_share": len(closed) / len(rows) if rows else None,
        **{f"closed_{key}": value for key, value in trade_metrics(closed).items()},
        "closed_loss_over_10pct_count": losses,
        "closed_loss_over_10pct_rate": losses / len(closed) if closed else None,
        "open_mean_marked_return": mean(row["marked_return"] for row in opened) if opened else None,
        "open_median_marked_return": median(row["marked_return"] for row in opened) if opened else None,
        "open_market_value": sum(row["market_value"] for row in opened),
        "open_unrealized_pnl": sum(row["unrealized_pnl"] for row in opened),
        "all_mean_asof_return": mean(row["asof_return"] for row in rows) if rows else None,
        "all_median_asof_return": median(row["asof_return"] for row in rows) if rows else None,
    }


def cross_diagnostics(
    rows: Sequence[dict[str, Any]], snapshots: dict[str, dict[str, FactorSnapshot]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assignments = []
    for row in rows:
        snapshot = snapshots.get(row["code"], {}).get(row["entry_signal_at"][:10])
        assignments.append({
            **row, "cross_group": adx_cross_group(snapshot),
            "factor_completed_date": snapshot.completed_date if snapshot else None,
            "factor_values": json.dumps({key: snapshot.value(key) if snapshot else None
                                         for key in ADX14.required_fields + ADX21.required_fields}, sort_keys=True),
        })
    metrics = [{"cross_group": group, **cohort_metrics([
        row for row in assignments if row["cross_group"] == group
    ])} for group in CROSS_GROUPS]
    return assignments, metrics


def account_contributions(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """分母始终为全部账户；状态分组只描述，不作为交易门控。"""
    if not pairs:
        raise ValueError("账户贡献需要非空配对表")
    rows = []
    for dimension, key in (("ever_bought", "filled_buy_count"), ("end_holding", "end_holding")):
        for base in (False, True):
            for candidate in (False, True):
                group = [row for row in pairs if bool(row[f"baseline_{key}"]) == base
                         and bool(row[f"candidate_{key}"]) == candidate]
                rows.append({
                    "dimension": dimension, "baseline_state": base, "candidate_state": candidate,
                    "account_count": len(group), "denominator": len(pairs),
                    "return_delta_contribution": sum(row["return_delta"] for row in group) / len(pairs),
                    "within_group_mean_return_delta": mean(row["return_delta"] for row in group) if group else None,
                })
    for key in ("realized_return", "unrealized_return"):
        if all(f"baseline_{key}" in row and f"candidate_{key}" in row for row in pairs):
            rows.append({
                "dimension": key, "baseline_state": None, "candidate_state": None,
                "account_count": len(pairs), "denominator": len(pairs),
                "return_delta_contribution": mean(row[f"candidate_{key}"] - row[f"baseline_{key}"] for row in pairs),
                "within_group_mean_return_delta": None,
            })
    return rows


def equal_time_weight_diagnostic(diagnostics: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """复核用户指定的旧F1—F3统一基线权重，不新增筛选门槛。"""
    grouped = defaultdict(dict)
    for row in diagnostics:
        if row["fold"] in (1, 2, 3):
            grouped[row["scenario"]][row["fold"], row["group"]] = row
    output = []
    for scenario, data in grouped.items():
        totals = [data[f, "retained"]["trade_count"] + data[f, "rejected"]["trade_count"] for f in (1, 2, 3)]
        valid = all(data[f, g]["mean_net_return"] is not None for f in (1, 2, 3) for g in ("retained", "rejected"))
        pooled = {}
        for group in ("retained", "rejected"):
            count = sum(data[f, group]["trade_count"] for f in (1, 2, 3))
            pooled[group] = sum((data[f, group]["mean_net_return"] or 0) * data[f, group]["trade_count"] for f in (1, 2, 3)) / count if count else None
        output.append({
            "scenario": scenario, "baseline_trade_count": sum(totals),
            "fold_weights": json.dumps([total / sum(totals) for total in totals]),
            "pooled_mean_difference": pooled["retained"] - pooled["rejected"] if all(v is not None for v in pooled.values()) else None,
            "equal_time_weight_difference": sum(total / sum(totals) * (data[f, "retained"]["mean_net_return"] - data[f, "rejected"]["mean_net_return"]) for f, total in zip((1, 2, 3), totals)) if valid else None,
        })
    return output


def checkpoint_account(
    result: dict[str, Any] | None, *, code: str, name: str,
    initial_cash: float, checkpoint: str,
) -> dict[str, Any]:
    from app.quant.research.purification import account_result

    days = [row for row in result["daily_rows"] if row["trade_date"] <= checkpoint] if result else []
    if not days:
        account = account_result(code=code, name=name, initial_cash=initial_cash, result=None)
        return {**account, "realized_return": 0., "unrealized_return": 0., "last_valuation_date": None}
    last = days[-1]
    account = account_result(code=code, name=name, initial_cash=initial_cash, result={
        "summary": {
            "final_assets": last["total_assets"],
            "filled_buy_count": sum(row["action"] == "buy" and row["execution_at"][:10] <= checkpoint for row in result["event_rows"]),
            "end_holding": int(last["shares_at_close"]) > 0,
        },
        "daily_rows": days,
        "closed_trade_rows": [row for row in result["closed_trade_rows"] if row["exit_execution_at"][:10] <= checkpoint],
    })
    return {
        **account, "realized_return": last["realized_pnl_cumulative"] / initial_cash,
        "unrealized_return": last["unrealized_pnl"] / initial_cash,
        "last_valuation_date": last["trade_date"],
    }


def entry_day_sensitivity(
    assignments: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """事后时间权重敏感性；不作为协议晋级条件，不悄悄删除空组日期。"""
    dates = sorted({row['entry_signal_at'][:10] for row in assignments})
    modes = sorted({row['cost_mode'] for row in assignments})
    grouped = defaultdict(list)
    for row in assignments:
        grouped[row['cost_mode'], row['entry_signal_at'][:10], row['cross_group']].append(row)
    daily = [{'cost_mode': mode, 'entry_date': day, 'cross_group': group,
              **cohort_metrics(grouped[mode, day, group])}
             for mode in modes for day in dates for group in CROSS_GROUPS]
    indexed = {(row['cost_mode'], row['entry_date'], row['cross_group']): row for row in daily}
    output = []
    for mode in modes:
        for measure, count_key, mean_key in (
            ('closed', 'closed_count', 'closed_mean_net_return'),
            ('closed_or_marked', 'entry_trade_count', 'all_mean_asof_return'),
        ):
            total, weighted, positive, valid = 0, 0., 0, 0
            counts, sums = {'only_adx14': 0, 'only_adx21': 0}, {'only_adx14': 0., 'only_adx21': 0.}
            for day in dates:
                by_group = {group: indexed[mode, day, group] for group in CROSS_GROUPS}
                weight = sum(row[count_key] for row in by_group.values())
                total += weight
                for group in counts:
                    row = by_group[group]
                    counts[group] += row[count_key]
                    sums[group] += (row[mean_key] or 0.) * row[count_key]
                if all(by_group[group][mean_key] is not None for group in counts):
                    delta = by_group['only_adx21'][mean_key] - by_group['only_adx14'][mean_key]
                    weighted += weight * delta
                    positive += delta > 0
                    valid += 1
            output.append({
                'cost_mode': mode, 'measure': measure, 'baseline_weight_denominator': total,
                'pooled_difference': sums['only_adx21'] / counts['only_adx21'] - sums['only_adx14'] / counts['only_adx14'] if all(counts.values()) else None,
                'same_entry_day_weight_difference': weighted / total if total and valid == len(dates) else None,
                'positive_difference_day_count': positive, 'common_sample_day_count': valid, 'day_count': len(dates),
                'role': 'posthoc_descriptive_sensitivity_not_promotion_rule',
            })
    return daily, output
