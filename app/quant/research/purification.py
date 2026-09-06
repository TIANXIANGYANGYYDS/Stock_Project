"""固定独立账户模型下的基线交易分组与完整回放配对。"""

from __future__ import annotations

import json
from statistics import mean, median
from typing import Any, Sequence

from app.quant.research.evaluation import (
    PLATFORM_EXPECTANCY_SIMILARITY,
    chronological_folds,
    maximum_drawdown,
    percentile,
    trade_metrics,
)
from app.quant.research.factors import FactorSnapshot
from app.quant.research.scenarios import FactorScenario, are_grid_neighbors


def account_result(
    *, code: str, name: str, initial_cash: float, result: dict[str, Any] | None
) -> dict[str, Any]:
    """无回放历史和无交易账户也纳入；期末持仓使用原回放估值。"""
    summary = result["summary"] if result else {}
    trades = result["closed_trade_rows"] if result else []
    assets = [initial_cash] + (
        [float(row["total_assets"]) for row in result["daily_rows"]]
        if result else []
    )
    return {
        "code": code,
        "name": name,
        "initial_cash": initial_cash,
        "final_assets": float(summary.get("final_assets", initial_cash)),
        "total_return": float(summary.get("final_assets", initial_cash)) / initial_cash - 1,
        "maximum_drawdown": maximum_drawdown(assets),
        "filled_buy_count": int(summary.get("filled_buy_count", 0)),
        "closed_trade_count": len(trades),
        "mean_net_return": mean(float(row["net_return"]) for row in trades) if trades else None,
        "end_holding": bool(summary.get("end_holding", False)),
    }


def paired_account_results(
    baseline: Sequence[dict[str, Any]], candidate: Sequence[dict[str, Any]],
    *, scenario: str, scenario_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = {row["code"]: row for row in baseline}
    selected = {row["code"]: row for row in candidate}
    if not base or base.keys() != selected.keys() or len(base) != len(baseline) or len(selected) != len(candidate):
        raise ValueError("逐股配对必须覆盖同一批非空且不重复的股票账户")
    rows = []
    for code, original in base.items():
        filtered = selected[code]
        if original["initial_cash"] != filtered["initial_cash"]:
            raise ValueError("同股两侧初始资金必须相同")
        delta = filtered["total_return"] - original["total_return"]
        row = {"scenario": scenario, "scenario_label": scenario_label, "code": code, "name": original["name"]}
        for key in original:
            if key not in {"code", "name"}:
                row[f"baseline_{key}"] = original[key]
                row[f"candidate_{key}"] = filtered[key]
        row.update(
            return_delta=delta,
            return_direction="unchanged" if abs(delta) < 1e-12 else ("improved" if delta > 0 else "worsened"),
            closed_trade_count_delta=filtered["closed_trade_count"] - original["closed_trade_count"],
            filled_buy_count_delta=filtered["filled_buy_count"] - original["filled_buy_count"],
            maximum_drawdown_delta=filtered["maximum_drawdown"] - original["maximum_drawdown"],
        )
        rows.append(row)
    deltas = [row["return_delta"] for row in rows]
    summary = {
        "scenario": scenario, "scenario_label": scenario_label, "account_count": len(rows),
        "baseline_mean_account_return": mean(row["total_return"] for row in baseline),
        "candidate_mean_account_return": mean(row["total_return"] for row in candidate),
        "mean_return_delta": mean(deltas), "median_return_delta": median(deltas),
        "return_delta_p10": percentile(deltas, .1), "return_delta_p90": percentile(deltas, .9),
        "mean_maximum_drawdown_delta": mean(row["maximum_drawdown_delta"] for row in rows),
        "median_maximum_drawdown_delta": median(row["maximum_drawdown_delta"] for row in rows),
        "baseline_no_buy_account_count": sum(row["filled_buy_count"] == 0 for row in baseline),
        "candidate_no_buy_account_count": sum(row["filled_buy_count"] == 0 for row in candidate),
    }
    for direction in ("improved", "unchanged", "worsened"):
        count = sum(row["return_direction"] == direction for row in rows)
        summary[f"{direction}_account_count"] = count
        summary[f"{direction}_account_rate"] = count / len(rows)
    return rows, summary


def _group_metrics(trades: Sequence[dict[str, Any]], *, account_count: int) -> dict[str, Any]:
    returns = [float(row["net_return"]) for row in trades]
    stock_count = len({row["code"] for row in trades})
    losses = [value for value in returns if value < 0]
    metrics = {
        **trade_metrics(trades),
        "stock_count": stock_count,
        "stock_coverage_rate": stock_count / account_count,
        "loss_count": len(losses),
        "mean_losing_return": mean(losses) if losses else None,
        "median_losing_return": median(losses) if losses else None,
    }
    for label, lower, upper in (
        ("loss_lt_minus20pct", float("-inf"), -.2),
        ("loss_minus20_to_minus10pct", -.2, -.1),
        ("loss_minus10_to_minus5pct", -.1, -.05),
        ("loss_minus5_to_zero", -.05, 0),
    ):
        count = sum(lower <= value < upper for value in returns)
        metrics[f"{label}_count"] = count
        metrics[f"{label}_rate"] = count / len(trades) if trades else None
    return metrics


def baseline_trade_diagnostics(
    *, baseline_trades: Sequence[dict[str, Any]], scenarios: Sequence[FactorScenario],
    snapshots: dict[str, dict[str, FactorSnapshot]], market_dates: Sequence[str],
    account_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按原买点时刻分组；退出、费用、投入金额均保留基线结果，不能视作新路径。"""
    folds = chronological_folds(market_dates)
    fold_by_date = {day: index for index, days in enumerate(folds, 1) for day in days}
    assignments, metrics = [], []
    for scenario in scenarios:
        groups: dict[str, list[dict[str, Any]]] = {"retained": [], "rejected": []}
        for trade in baseline_trades:
            code = str(trade["code"])
            signal_date = str(trade["entry_signal_at"])[:10]
            snapshot = snapshots.get(code, {}).get(signal_date)
            available = scenario.is_available(snapshot)
            group = "retained" if scenario.accepts(snapshot) else "rejected"
            row = {
                **trade, "scenario": scenario.key, "scenario_label": scenario.label,
                "group": group, "factor_available": available,
                "rejection_reason": "" if group == "retained" else ("threshold" if available else "missing_factor"),
                "factor_completed_date": snapshot.completed_date if snapshot else None,
                "entry_fold": fold_by_date[signal_date],
                "factor_values": json.dumps({key: snapshot.value(key) if snapshot else None for key in scenario.required_fields}, sort_keys=True),
            }
            groups[group].append(row)
            assignments.append(row)
        for fold in range(5):
            dates = market_dates if fold == 0 else folds[fold - 1]
            for group, trades in groups.items():
                chosen = trades if fold == 0 else [row for row in trades if row["entry_fold"] == fold]
                metrics.append({
                    "scenario": scenario.key, "scenario_label": scenario.label,
                    "fold": fold, "start_date": dates[0], "end_date": dates[-1], "group": group,
                    "missing_factor_count": sum(not row["factor_available"] for row in chosen),
                    **_group_metrics(chosen, account_count=account_count),
                })
    return assignments, metrics


def parameter_comparisons(
    scenarios: Sequence[FactorScenario], metrics: Sequence[dict[str, Any]],
    diagnostics: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """并列展示旧金额平台条件和同批交易的收益率提纯增量，不自动选优。"""
    by_key = {row["scenario"]: row for row in metrics}
    diagnostic = {(row["scenario"], row["group"]): row for row in diagnostics if row["fold"] == 0}
    rows = []
    for index, first in enumerate(scenarios):
        for second in scenarios[index + 1:]:
            if not are_grid_neighbors(first, second):
                continue
            gains, spreads = [], []
            for scenario in (first, second):
                item = by_key[scenario.key]
                gains.append(item["oos_net_expectancy"] - item["oos_baseline_net_expectancy"] if item["oos_available"] else None)
                kept = diagnostic[(scenario.key, "retained")]["mean_net_return"]
                rejected = diagnostic[(scenario.key, "rejected")]["mean_net_return"]
                spreads.append(kept - rejected if kept is not None and rejected is not None else None)
            similarity = min(gains) / max(gains) if all(gain is not None and gain > 0 for gain in gains) else None
            rows.append({
                "first_scenario": first.key, "second_scenario": second.key,
                "legacy_first_oos_amount_gain": gains[0], "legacy_second_oos_amount_gain": gains[1],
                "legacy_amount_gain_similarity": similarity,
                "legacy_75pct_platform_pass": similarity is not None and similarity >= PLATFORM_EXPECTANCY_SIMILARITY,
                "first_retained_minus_rejected_mean_return": spreads[0],
                "second_retained_minus_rejected_mean_return": spreads[1],
            })
    return rows


def render_purification_report(
    summary: dict[str, Any], metrics: Sequence[dict[str, Any]],
    diagnostics: Sequence[dict[str, Any]], pairs: Sequence[dict[str, Any]],
    neighbors: Sequence[dict[str, Any]],
) -> str:
    def pct(value: Any) -> str:
        return "—" if value is None else f"{value:.2%}"

    def number(value: Any) -> str:
        return "—" if value is None else f"{value:.2f}"

    lines = [
        "# MACD买点提纯：12组独立账户验证", "",
        f"样本：{summary['sample_size']}只；{summary['start_date']}至{summary['end_date']}，{summary['market_trade_day_count']}个交易日。",
        "每股相同初始资金、资金仅用于本股、按原规则全仓买卖并累计盈亏。辅助指标只门控原MACD有效买点；原观察、确认、信号有效期、卖出和撮合不变。日线辅助指标截至t−1，RS与RTOV沿用本地公式。",
        "无交易账户仍纳入账户收益均值，单笔收益为空。账户期末收益包含未平仓估值；单笔统计仅包括本窗口闭合交易。单股回撤使用含初始资金的日终资产序列，交易MAE沿用原盘中柱计算。",
        "全部账户汇总收益率等于各股收益率算术平均，不发生跨股调配或再平衡。",
        f"{summary['delayed_replay_start_stock_count']}只股票因前置日线不足延后回放，其中{summary['flat_insufficient_history_stock_count']}只全程保持空仓，均保留在账户统计中。表中—表示无样本或不可计算。", "",
        "## 1. 同批基线交易的提纯诊断", "",
        "按基线原买入信号当天的指标分为保留/剔除，沿用原交易退出与费用结果。指标缺失计入剔除并单列数量，不能解释成阈值区分能力。此表仅诊断，不能代替过滤后的完整回放。",
        "| 规则 | 保留/剔除笔数 | 保留/剔除均值 | 保留/剔除中位数 | 保留/剔除胜率 | 保留/剔除盈亏比 | 均值差/百分点 | 缺失剔除 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    grouped = {(row['scenario'], row['fold'], row['group']): row for row in diagnostics}
    candidates = [row for row in metrics if row['scenario'] != 'baseline']
    for item in candidates:
        kept, rejected = (grouped[(item['scenario'], 0, group)] for group in ('retained', 'rejected'))
        spread = kept['mean_net_return'] - rejected['mean_net_return'] if kept['trade_count'] and rejected['trade_count'] else None
        lines.append(
            f"| {item['scenario_label']} | {kept['trade_count']}/{rejected['trade_count']} | "
            f"{pct(kept['mean_net_return'])}/{pct(rejected['mean_net_return'])} | "
            f"{pct(kept['median_net_return'])}/{pct(rejected['median_net_return'])} | "
            f"{pct(kept['win_rate'])}/{pct(rejected['win_rate'])} | "
            f"{number(kept['payoff_ratio'])}/{number(rejected['payoff_ratio'])} | "
            f"{number(spread * 100 if spread is not None else None)} | {rejected['missing_factor_count']} |"
        )
    lines.extend([
        "", "## 2. 各配置完整回放的交易质量", "",
        "按单笔净收益率比较，不按单笔盈利金额选优。诊断分组笔数与完整回放笔数可不同，因为跳过买入会改变本股后续状态、可响应信号和累计资金。",
        "| 配置 | 闭合交易 | 均值 | 中位数 | 胜率 | 盈亏比 | MAE均值 | ES95 | 全账户汇总收益 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in metrics:
        lines.append(f"| {row['scenario_label']} | {row['trade_count']} | {pct(row['mean_net_return'])} | {pct(row['median_net_return'])} | {pct(row['win_rate'])} | {number(row['payoff_ratio'])} | {pct(row['mean_mae_return'])} | {pct(row['es95_return'])} | {pct(row['total_return'])} |")
    lines.extend([
        "", "## 3. 逐股完整账户配对", "",
        "收益差为辅助策略减基线；下表增量和回撤差使用百分点。回撤差小于零表示改善。提高/不变/降低的分母均为全部股票。",
        "| 配置 | 收益提高 | 不变 | 降低 | 收益增量均值/中位数 | 增量P10/P90 | 平均回撤差 | 辅助策略未买入账户 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in pairs:
        lines.append(f"| {row['scenario_label']} | {pct(row['improved_account_rate'])} | {pct(row['unchanged_account_rate'])} | {pct(row['worsened_account_rate'])} | {number(row['mean_return_delta'] * 100)}/{number(row['median_return_delta'] * 100)} | {number(row['return_delta_p10'] * 100)}/{number(row['return_delta_p90'] * 100)} | {number(row['mean_maximum_drawdown_delta'] * 100)} | {row['candidate_no_buy_account_count']} |")
    lines.extend([
        "", "## 4. 分时段的区分能力", "",
        f"{summary['fold_ranges']}。按原买入信号日期归折，统计截至窗口末已闭合交易；后段尚未平仓的交易未纳入，存在截尾影响。第四折已经参与第一轮研究与本轮候选选择，不是新的独立OOS。",
        "每格为保留均值减剔除均值（百分点），括号内为保留/剔除笔数；空组不填零。",
        "| 配置 | F1 | F2 | F3 | F4 |", "| --- | ---: | ---: | ---: | ---: |",
    ])
    for item in candidates:
        cells = []
        for fold in range(1, 5):
            kept, rejected = (grouped[(item['scenario'], fold, group)] for group in ('retained', 'rejected'))
            spread = (kept['mean_net_return'] - rejected['mean_net_return']) * 100 if kept['trade_count'] and rejected['trade_count'] else None
            cells.append(f"{number(spread)} ({kept['trade_count']}/{rejected['trade_count']})")
        lines.append(f"| {item['scenario_label']} | " + " | ".join(cells) + " |")
    lines.extend([
        "", "## 5. 原参数平台判定的解释", "",
        "旧平台按第四折（以退出日期归折）的每笔净盈利金额增量计算：同族相邻参数均为正增量，且较小增量/较大增量≥75%。此判定受本股投入资金路径影响，仅用于解释旧门槛，不作为第二轮统一指标选择结论。旧规则在本轮12组范围内重算，未入选的邻点不参与，因此此处晋级表不能替代原67组晋级表。",
        "| 相邻配置 | 旧金额增量（元） | 相似度 | 旧75%门槛 |", "| --- | ---: | ---: | --- |",
    ])
    for row in neighbors:
        lines.append(f"| {row['first_scenario']} / {row['second_scenario']} | {number(row['legacy_first_oos_amount_gain'])}/{number(row['legacy_second_oos_amount_gain'])} | {pct(row['legacy_amount_gain_similarity'])} | {'通过' if row['legacy_75pct_platform_pass'] else '未通过'} |")
    lines.extend([
        "", "## 6. 结论边界与明细", "",
        "单笔质量改善只说明提纯迹象；逐股收益改善说明它进一步转化为账户收益，两者分别判断。当前窗口及其中各折已被研究使用，不能确认长期优势，暂不自动指定最终规则，也不按个股历史最优参数选型。",
        "- `baseline_trade_assignments.csv`：每笔基线交易的保留/剔除、指标时点、原始交易结果。",
        "- `purification_diagnostics.csv`：全期及四个买点时间折的分组质量、股票覆盖、MAE、ES、最终亏损分布。",
        "- `stock_paired_results.csv`、`paired_summary.csv`：逐股收益、收益差、交易次数与日终回撤变化，含未交易账户。",
        "- `parameter_comparisons.csv`：相邻参数旧金额平台原因及本轮收益率区分差。",
        "- `scenario_metrics.csv`、`time_fold_metrics.csv`：原有完整回放及双倍成本数据；`promotion_assessment.csv`仅作旧规则在本轮12组范围内的审计。",
        "- `closed_trades.csv`、`signal_decisions.csv`、`factor_snapshots.csv`：完整路径与指标审计。", "",
    ])
    # 段落与表格之间留空行，方便标准Markdown渲染。
    rendered = []
    for line in lines:
        if line.startswith("| ") and rendered and rendered[-1] and not rendered[-1].startswith("| "):
            rendered.append("")
        rendered.append(line)
    return "\n".join(rendered)
