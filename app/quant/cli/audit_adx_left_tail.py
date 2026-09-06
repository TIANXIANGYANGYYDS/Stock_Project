"""只读第三轮产物，复核分歧原因与日期加权左尾风险；不回放或访问数据库。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from app.quant.cli.replay_adx_comparison import read_csv
from app.quant.cli.replay_factor_experiments import _write_csv
from app.quant.research.adx_left_tail import (
    FINALIST_SCENARIO_KEYS, daily_weighted_risk, disagreement_reasons,
)
from app.quant.research.scenarios import FACTOR_SCENARIOS

FORWARD_PROTOCOL = Path(__file__).resolve().parents[1] / 'research/ADX_FORWARD_PROTOCOL.md'


def render_report(comparisons, reasons, summary) -> str:
    def percent(value):
        return '—' if value is None else f'{value * 100:.4f}%'

    labels = {'mean_net_return': '闭合平均净收益', 'win_rate': '闭合胜率',
              'loss_over_10pct_rate': '最终亏损超过10%的比例', 'mean_mae_return': '闭合平均MAE'}
    lines = [
        '# 第三轮补充审计：ADX分歧原因与日期加权左尾风险', '',
        f"原入组窗口{summary['entry_start']}至{summary['entry_end']}；观察至{summary['observation_end']}。本轮只读旧产物，不产生新OOS。",
        '权重为每天全部基线已闭合交易数/全期基线已闭合数，包含缺失指标组。自然退出统计与未平仓市值分开，空分歧组不填零、不选择性删除日期。', '',
        '| 成本 | 指标 | 原14 | 原21 | 加权14 | 加权21 | 加权21−14 | 正/负/相同日期 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for row in comparisons:
        lines.append(
            f"| {row['cost_mode']} | {labels[row['metric']]} | {percent(row['raw_adx14'])} | {percent(row['raw_adx21'])} | "
            f"{percent(row['weighted_adx14'])} | {percent(row['weighted_adx21'])} | {('—' if row['weighted_difference_21_minus_14'] is None else format(row['weighted_difference_21_minus_14'] * 100, '.4f'))} | "
            f"{row['positive_difference_dates']}/{row['negative_difference_dates']}/{row['equal_difference_dates']} |"
        )
    lines += ['', '差值列为百分点；收益与MAE差值为正表示ADX21较高，亏损率差值为负表示ADX21较低。MAE为负收益值，较高意味着不利波动较小。', '',
        '## 分歧原因（描述性分组，不是新条件）', '',
        '| 成本 | 独有组 | 未通过原因 | 入组/闭合/未平仓 | 闭合均值 | 闭合亏损超10% |',
        '| --- | --- | --- | ---: | ---: | ---: |',
    ]
    for row in reasons:
        lines.append(f"| {row['cost_mode']} | {row['cross_group']} | {row['rejection_reason']} | {row['entry_trade_count']}/{row['closed_count']}/{row['open_count']} | {percent(row['closed_mean_net_return'])} | {percent(row['closed_loss_over_10pct_rate'])} |")
    lines += ['', '## 结论边界', '',
        '若收益均值差经日期加权后减弱而大亏损率/MAE差仍保留，只能说明当前数据存在左尾风险提纯迹象，不能声称已确认未来概率或因果机制。7个入组日不是数千次独立试验；成本压力不是新OOS。',
        'ADX14账户领先是真实历史结果，第三轮连续账户含此前研究过的7—8月新交易，不能与前两轮作为独立胜利重复计票。',
        '“ADX14先恢复、ADX21趋势成熟”等表述属于机制假说；审计只证实指标阈值及上升条件的数值分解。',
        '下阶段仅冻结MACD基线、ADX21、ADX14。先比较全部独立账户收益，再看坏交易率、MAE、ES95及入组日期稳定性。旧75%金额平台不改。',
        '前瞻入组窗口与观察截止日尚未登记，新OOS尚未运行；具体日期与研究停止规则须在未来结果出现前登记。冻结规则协议及待登记字段见forward_plan.json。', '',
    ]
    return '\n'.join(lines)


def run(source: Path, output: Path) -> Path:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError('补充审计应写入独立目录，不能覆盖或混入第三轮原始产物')
    inputs = ['baseline_cross_assignments.csv', 'summary.json', 'protocol.json']
    before = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in inputs}
    rows = read_csv(source / inputs[0])
    summary = json.loads((source / 'summary.json').read_text())
    identities = [(row['cost_mode'], row['code'], row['entry_signal_at']) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError('基线交易重复，不能继续审计')
    for row in rows:
        if not summary['entry_start'] <= row['entry_signal_at'][:10] <= summary['entry_end']:
            raise ValueError('交易不在原入组区间')
        if row['outcome'] not in ('closed', 'open'):
            raise ValueError('交易必须分为自然退出或未平仓')
    assignments, reasons = disagreement_reasons(rows)
    daily, comparisons = daily_weighted_risk(rows)
    output.mkdir(parents=True, exist_ok=False)
    for name, values in (
        ('disagreement_assignments.csv', assignments), ('disagreement_reason_metrics.csv', reasons),
        ('daily_left_tail.csv', daily), ('weighted_left_tail.csv', comparisons),
    ):
        _write_csv(output / name, values)
    rules = {item.key: item for item in FACTOR_SCENARIOS}
    protocol_text = FORWARD_PROTOCOL.read_text()
    plan = {
        'freeze_date': '2026-09-05', 'status': 'rules_frozen_window_not_registered_oos_not_started',
        'scenario_keys': list(FINALIST_SCENARIO_KEYS),
        'candidate_parameters': {key: dict(rules[key].parameters) for key in FINALIST_SCENARIO_KEYS[1:]},
        'primary_metric': 'mean_return_of_all_independent_stock_accounts',
        'secondary_metrics': ['closed_loss_over_10pct_rate', 'mae_return', 'es95_return'],
        'independent_unit_for_future_inference': 'entry_date_or_preregistered_time_block',
        'entry_start': None, 'entry_end': None, 'observation_end': None,
        'both_entry_and_outcome_paths_must_be_unused': True,
        'entry_must_follow_rule_freeze': True, 'daily_factor_cutoff': 't-1',
        'initial_cash_per_stock': 100000, 'independent_compounding_all_in_all_out': True,
        'past_search_scenario_count': 67, 'old_75pct_platform_rule_unchanged': True,
        'protocol_sha256': hashlib.sha256(protocol_text.encode()).hexdigest(), 'protocol_text': protocol_text,
    }
    (output / 'forward_plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    after = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in inputs}
    if before != after:
        raise RuntimeError('审计期间原始输入发生变化')
    validation = {
        'status': 'passed', 'source': str(source), 'input_sha256': before, 'source_inputs_unchanged': True,
        'trade_counts_by_cost': dict(Counter(row['cost_mode'] for row in rows)),
        'disagreement_counts_by_cost': dict(Counter(row['cost_mode'] for row in assignments)),
        'unique_trade_identity_verified': True, 'cross_group_recomputed_from_factor_values': True,
        'weighted_baseline_denominator_by_cost': {row['cost_mode']: row['baseline_closed_denominator'] for row in comparisons},
        'new_oos_run': False,
    }
    (output / 'validation.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2) + '\n')
    report = output / 'report.md'
    report.write_text(render_report(comparisons, reasons, summary), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(run(args.source, args.output))


if __name__ == '__main__':
    main()
