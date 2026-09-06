"""第三轮分歧原因及按买点日期加权的左尾风险审计；不生成策略。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from statistics import mean
from typing import Any, Sequence

from app.quant.research.adx_comparison import ADX14, ADX21, adx_cross_group, cohort_metrics
from app.quant.research.factors import FactorSnapshot

# 只冻结原规则，不产生新阈值或组合条件。
FINALIST_SCENARIO_KEYS = ('baseline', ADX21.key, ADX14.key)
DISAGREEMENT_GROUPS = ('only_adx14', 'only_adx21')
WEIGHTED_METRICS = (
    'mean_net_return', 'win_rate', 'loss_over_10pct_rate', 'mean_mae_return',
)


def closed_risk_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    closed = [row for row in rows if row['outcome'] == 'closed']
    losses = sum(row['net_return'] < -.1 for row in closed)
    return {
        'entry_count': len(rows), 'closed_count': len(closed),
        'open_count': len(rows) - len(closed),
        'mean_net_return': mean(row['net_return'] for row in closed) if closed else None,
        'win_rate': mean(row['net_return'] > 0 for row in closed) if closed else None,
        'loss_over_10pct_count': losses,
        'loss_over_10pct_rate': losses / len(closed) if closed else None,
        'mean_mae_return': mean(row['mae_return'] for row in closed) if closed else None,
    }


def disagreement_reasons(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assignments, grouped = [], defaultdict(list)
    for row in rows:
        values = json.loads(row['factor_values'])
        snapshot = FactorSnapshot(row['entry_signal_at'][:10], row['factor_completed_date'], values)
        if adx_cross_group(snapshot) != row['cross_group']:
            raise ValueError('保存的交叉组与买点指标值不一致')
        if row['factor_completed_date'] and row['factor_completed_date'] >= row['entry_signal_at'][:10]:
            raise ValueError('指标完成日期必须早于买点日期')
        if row['cross_group'] not in DISAGREEMENT_GROUPS:
            continue
        failing_period = 21 if row['cross_group'] == 'only_adx14' else 14
        current = values[f'adx_{failing_period}']
        previous = values[f'adx_{failing_period}_3_days_ago']
        reason = ('below_20' if current < 20 else 'at_least_20') + (
            '_and_rising' if current > previous else '_and_not_rising'
        )
        assignment = {
            **row, **values, 'rejected_period': failing_period, 'rejection_reason': reason,
            'adx14_change_3d': values['adx_14'] - values['adx_14_3_days_ago'],
            'adx21_change_3d': values['adx_21'] - values['adx_21_3_days_ago'],
        }
        assignments.append(assignment)
        grouped[row['cost_mode'], row['cross_group'], reason].append(assignment)
    summaries = []
    for (mode, group, reason), selected in grouped.items():
        total = sum(len(items) for (m, g, _), items in grouped.items() if m == mode and g == group)
        summaries.append({
            'cost_mode': mode, 'cross_group': group, 'rejection_reason': reason,
            'share_of_disagreement_group': len(selected) / total,
            'mean_adx14': mean(row['adx_14'] for row in selected),
            'mean_adx21': mean(row['adx_21'] for row in selected),
            'mean_adx14_change_3d': mean(row['adx14_change_3d'] for row in selected),
            'mean_adx21_change_3d': mean(row['adx21_change_3d'] for row in selected),
            **cohort_metrics(selected),
        })
    return assignments, summaries


def daily_weighted_risk(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """全部基线当日闭合笔数作权重，含缺失组；空分歧组不填零、不重归一。"""
    modes = sorted({row['cost_mode'] for row in rows})
    dates = sorted({row['entry_signal_at'][:10] for row in rows})
    buckets = defaultdict(list)
    for row in rows:
        buckets[row['cost_mode'], row['entry_signal_at'][:10]].append(row)
    daily, comparisons = [], []
    for mode in modes:
        baseline_total = sum(row['outcome'] == 'closed' for row in rows if row['cost_mode'] == mode)
        by_day = {}
        for day in dates:
            all_rows = buckets[mode, day]
            baseline_count = sum(row['outcome'] == 'closed' for row in all_rows)
            day_metrics = {}
            for group in DISAGREEMENT_GROUPS:
                metrics = closed_risk_metrics([row for row in all_rows if row['cross_group'] == group])
                day_metrics[group] = metrics
                daily.append({
                    'cost_mode': mode, 'entry_date': day, 'cross_group': group,
                    'baseline_closed_count': baseline_count,
                    'common_weight': baseline_count / baseline_total if baseline_total else None,
                    **metrics,
                })
            by_day[day] = (baseline_count, day_metrics)
        pooled = {
            group: closed_risk_metrics([row for row in rows if row['cost_mode'] == mode and row['cross_group'] == group])
            for group in DISAGREEMENT_GROUPS
        }
        for metric in WEIGHTED_METRICS:
            weighted = {group: 0. for group in DISAGREEMENT_GROUPS}
            eligible, missing, positive, negative, ties = 0, 0, 0, 0, 0
            for count, groups in by_day.values():
                if count == 0:
                    continue
                eligible += 1
                if any(groups[group][metric] is None for group in DISAGREEMENT_GROUPS):
                    missing += 1
                    continue
                for group in DISAGREEMENT_GROUPS:
                    weighted[group] += count / baseline_total * groups[group][metric]
                delta = groups['only_adx21'][metric] - groups['only_adx14'][metric]
                if math.isclose(delta, 0., abs_tol=1e-12):
                    ties += 1
                elif delta > 0:
                    positive += 1
                else:
                    negative += 1
            valid = baseline_total > 0 and missing == 0
            raw14, raw21 = (pooled[group][metric] for group in DISAGREEMENT_GROUPS)
            comparisons.append({
                'cost_mode': mode, 'metric': metric,
                'raw_adx14': raw14, 'raw_adx21': raw21,
                'raw_difference_21_minus_14': raw21 - raw14 if raw14 is not None and raw21 is not None else None,
                'weighted_adx14': weighted['only_adx14'] if valid else None,
                'weighted_adx21': weighted['only_adx21'] if valid else None,
                'weighted_difference_21_minus_14': weighted['only_adx21'] - weighted['only_adx14'] if valid else None,
                'baseline_closed_denominator': baseline_total,
                'eligible_entry_date_count': eligible, 'missing_pair_date_count': missing,
                'positive_difference_dates': positive, 'negative_difference_dates': negative,
                'equal_difference_dates': ties,
                'better_direction_for_adx21': 'negative' if metric == 'loss_over_10pct_rate' else 'positive',
                'role': 'posthoc_descriptive_audit_no_significance_claim',
            })
    return daily, comparisons
