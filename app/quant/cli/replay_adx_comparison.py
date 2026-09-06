"""冻结五组，分开买点入组窗口与自然退出观察期，比较ADX14/21。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from app.core.config import get_settings
from app.quant.cli.replay_factor_experiments import (
    BASELINE, DEFAULT_OUTPUT_ROOT, ScenarioAccumulator, _add_flat_account, _add_result,
    _effective_replay_start, _factor_calendar, _load_ranked_factor_snapshots,
    _load_turnover, _write_csv,
)
from app.quant.cli.replay_sample import _market_dates, _sample_universe, sample_stocks
from app.quant.cli.replay_stock import _load_daily_documents, _load_minute_bars, replay
from app.quant.core.models import Bar
from app.quant.data.market_data import DAILY_HISTORY_COLLECTION, THREE_MINUTE_HISTORY_COLLECTION
from app.quant.research.adx_comparison import (
    ADX14, ADX21, account_contributions, checkpoint_account, cohort_metrics,
    cohort_trades, cross_diagnostics, entry_day_sensitivity, equal_time_weight_diagnostic,
)
from app.quant.research.evaluation import portfolio_metrics, trade_metrics
from app.quant.research.factors import FactorSnapshot
from app.quant.research.purification import paired_account_results
from app.quant.research.scenarios import ADX_COMPARISON_SCENARIOS
from app.quant.strategies.provisional_daily_macd_3m import official_backtest_config

PROTOCOL = Path(__file__).resolve().parents[1] / 'research/ADX_PROTOCOL.md'


def read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        for key, value in row.items():
            if key == 'code':
                continue
            if value == '':
                row[key] = None
            elif value in ('True', 'False'):
                row[key] = value == 'True'
            else:
                try:
                    row[key] = float(value)
                except ValueError:
                    pass
    return rows


def audit_prior_report(source: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    summary = json.loads((source / 'summary.json').read_text())
    diagnostics = read_csv(source / 'purification_diagnostics.csv')
    pair_groups = defaultdict(list)
    for row in read_csv(source / 'stock_paired_results.csv'):
        pair_groups[row['scenario']].append(row)
    contributions = []
    for scenario, rows in pair_groups.items():
        contributions.extend({'scenario': scenario, **row} for row in account_contributions(rows))
    _write_csv(output / 'account_contributions.csv', contributions)
    _write_csv(output / 'equal_time_weight_diagnostic.csv', equal_time_weight_diagnostic(diagnostics))
    assignments = read_csv(source / 'baseline_trade_assignments.csv')
    snapshots, baseline = {}, []
    for row in assignments:
        if row['scenario'] not in (ADX14.key, ADX21.key):
            continue
        day = row['entry_signal_at'][:10]
        existing = snapshots.setdefault(row['code'], {}).get(day)
        values = dict(existing.values) if existing else {}
        values.update(json.loads(row['factor_values']))
        snapshots[row['code']][day] = FactorSnapshot(day, row['factor_completed_date'], values)
        if row['scenario'] == ADX14.key:
            baseline.append({**row, 'outcome': 'closed', 'asof_return': row['net_return']})
    cross_rows, cross_metrics = cross_diagnostics(baseline, snapshots)
    _write_csv(output / 'baseline_cross_assignments.csv', cross_rows)
    _write_csv(output / 'baseline_cross_metrics.csv', cross_metrics)
    tail_rows = []
    for row in diagnostics:
        if row['fold'] == 0:
            count = row['loss_lt_minus20pct_count'] + row['loss_minus20_to_minus10pct_count']
            tail_rows.append({'scenario': row['scenario'], 'group': row['group'],
                              'trade_count': row['trade_count'], 'loss_over_10pct_count': count,
                              'loss_over_10pct_rate': count / row['trade_count'] if row['trade_count'] else None})
    _write_csv(output / 'closed_loss_over_10pct.csv', tail_rows)
    fourteen = {row['code']: row for row in pair_groups[ADX14.key]}
    only21 = [row['code'] for row in pair_groups[ADX21.key]
              if row['candidate_filled_buy_count'] and not fourteen[row['code']]['candidate_filled_buy_count']]
    result = {
        'source': str(source.resolve()), 'source_sample_size': summary['sample_size'],
        'baseline_closed_count': len(baseline), 'cross_total': sum(row['entry_trade_count'] for row in cross_metrics),
        'adx21_bought_adx14_never_bought_count': len(only21),
        'status': 'old_window_descriptive_audit_not_new_sample',
    }
    (output / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry-start', required=True, type=date.fromisoformat)
    parser.add_argument('--entry-end', required=True, type=date.fromisoformat)
    parser.add_argument('--observation-end', required=True, type=date.fromisoformat)
    parser.add_argument('--sample-size', type=int, default=None, help='默认全部期初股票')
    parser.add_argument('--seed', type=int, default=20260903)
    parser.add_argument('--output-root', type=Path, default=DEFAULT_OUTPUT_ROOT / 'adx_comparison5')
    return parser


def run(args: argparse.Namespace) -> Path:
    if args.sample_size is not None and args.sample_size <= 0:
        raise ValueError('sample-size必须大于0')
    if not args.entry_start <= args.entry_end <= args.observation_end:
        raise ValueError('必须满足entry-start≤entry-end≤observation-end')
    start, entry_end, end = (value.isoformat() for value in (args.entry_start, args.entry_end, args.observation_end))
    output = args.output_root / f'{start}_{entry_end}_observe_{end}' / f'n{args.sample_size or "all"}_seed{args.seed}'
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'summary.json').exists():
        raise ValueError('已有完成结果，请用新output-root保留旧实验')
    scenarios = (BASELINE,) + ADX_COMPARISON_SCENARIOS
    protocol = {
        'entry_start': start, 'entry_end': entry_end, 'observation_end': end,
        'scenario_keys': [item.key for item in scenarios],
        'protocol_sha256': hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
        'protocol_text': PROTOCOL.read_text(), 'seed': args.seed,
        'complete': False,
    }
    (output / 'protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + '\n')
    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    checkpoint_rows = defaultdict(list)
    cohorts = defaultdict(list)
    coverage = []
    try:
        db = client[settings.mongo_db_name]
        daily = db[DAILY_HISTORY_COLLECTION]
        dates = _market_dates(daily, start_date=start, end_date=end)
        if any(day not in dates for day in (start, entry_end, end)):
            raise ValueError('入组起止与观察终点必须为市场交易日')
        universe = _sample_universe(daily, start_date=start)
        stocks = sample_stocks(universe, sample_size=args.sample_size or len(universe), seed=args.seed)
        factor_dates, signal_dates = _factor_calendar(daily, start_date=start, end_date=end)
        turnover = _load_turnover(db['stock_daily_detail'], codes=[stock.code for stock in stocks],
                                  start_date=factor_dates[0], end_date=factor_dates[-1])
        snapshots, _ = _load_ranked_factor_snapshots(daily, sample_codes={stock.code for stock in stocks},
                    factor_dates=factor_dates, signal_dates=signal_dates, turnover=turnover)
        accumulators = {(mode, scenario.key): ScenarioAccumulator(scenario, dates)
                        for mode in ('normal', 'double_cost') for scenario in scenarios}
        for index, stock in enumerate(stocks, 1):
            documents = _load_daily_documents(daily, code=stock.code, through_date=end)
            bars = [Bar(str(row['trade_date']), float(row['open']), float(row['high']),
                        float(row['low']), float(row['close'])) for row in documents]
            minutes = _load_minute_bars(db[THREE_MINUTE_HISTORY_COLLECTION], code=stock.code,
                                       start_date=start, end_date=end)
            replay_start = _effective_replay_start(bars, requested_start=start, end_date=end)
            name = str(documents[-1].get('name') or stock.name_at_start)
            config = official_backtest_config(code=stock.code)
            stock_dates = {bar.trade_date for bar in bars}
            for day in dates:
                if day in stock_dates:
                    coverage.append({'code': stock.code, 'trade_date': day, 'minute_count': len(minutes.get(day, [])),
                                     'complete_80_bars': len(minutes.get(day, [])) == 80})
            for mode in ('normal', 'double_cost'):
                chosen_config = config if mode == 'normal' else replace(config,
                    commission_rate=config.commission_rate * 2, stamp_duty_rate=config.stamp_duty_rate * 2,
                    slippage_rate=config.slippage_rate * 2)
                for scenario in scenarios:
                    accumulator = accumulators[mode, scenario.key]
                    gate = None if scenario.key == 'baseline' else lambda code, day, selected=scenario: (
                        selected.accepts(snapshots.get(code, {}).get(day)), selected.label)
                    result = replay(code=stock.code, name=name, daily_bars=bars, minute_bars_by_date=minutes,
                        start_date=replay_start, end_date=end, config=chosen_config, buy_signal_gate=gate,
                        official_strategy_configuration=mode == 'normal') if replay_start else None
                    if result:
                        _add_result(accumulator, code=stock.code, name=name, result=result,
                                    snapshots=snapshots.get(stock.code, {}), initial_cash=config.initial_cash,
                                    store_decisions=True)
                        cohorts[mode, scenario.key].extend(cohort_trades(result, entry_start=start,
                            entry_end=entry_end, observation_end=end, minute_bars=minutes))
                    else:
                        _add_flat_account(accumulator, code=stock.code, name=name, initial_cash=config.initial_cash)
                    for checkpoint in sorted({entry_end, end}):
                        checkpoint_rows[mode, checkpoint, scenario.key].append(checkpoint_account(result,
                            code=stock.code, name=name, initial_cash=config.initial_cash, checkpoint=checkpoint))
            if index % 100 == 0 or index == len(stocks):
                print(f'adx_comparison_progress completed={index}/{len(stocks)}', flush=True)
    finally:
        client.close()
    capital = len(stocks) * official_backtest_config(code='000000').initial_cash
    full_metrics, cohort_quality, cross_rows, cross_quality = [], [], [], []
    for mode in ('normal', 'double_cost'):
        for scenario in scenarios:
            accumulator = accumulators[mode, scenario.key]
            meta = {'cost_mode': mode, 'scenario': scenario.key, 'scenario_label': scenario.label}
            full_metrics.append({**meta, **trade_metrics(accumulator.closed_trades),
                **portfolio_metrics([accumulator.daily_assets[day] for day in dates], capital_base=capital)})
            cohort_quality.append({**meta, **cohort_metrics(cohorts[mode, scenario.key])})
        rows, metrics = cross_diagnostics(cohorts[mode, 'baseline'], snapshots)
        cross_rows.extend({'cost_mode': mode, **row} for row in rows)
        cross_quality.extend({'cost_mode': mode, **row} for row in metrics)
    pairs, pair_summaries, contributions = [], [], []
    comparisons = [(scenario.key, 'baseline') for scenario in ADX_COMPARISON_SCENARIOS] + [(ADX21.key, ADX14.key)]
    for mode in ('normal', 'double_cost'):
        for checkpoint in sorted({entry_end, end}):
            for candidate, reference in comparisons:
                key = candidate if reference == 'baseline' else 'adx21_vs_adx14'
                rows, summary = paired_account_results(checkpoint_rows[mode, checkpoint, reference],
                    checkpoint_rows[mode, checkpoint, candidate], scenario=key, scenario_label=key)
                meta = {'cost_mode': mode, 'checkpoint': checkpoint, 'reference': reference, 'candidate': candidate}
                pairs.extend({**meta, **row} for row in rows)
                pair_summaries.append({**meta, **summary})
                contributions.extend({**meta, 'scenario': key, **row} for row in account_contributions(rows))
    direct = []
    for mode in ('normal', 'double_cost'):
        for scenario in ADX_COMPARISON_SCENARIOS:
            for group, accepted in (('retained', True), ('rejected', False)):
                rows = [row for row in cohorts[mode, 'baseline'] if
                        scenario.accepts(snapshots.get(row['code'], {}).get(row['entry_signal_at'][:10])) == accepted]
                direct.append({'cost_mode': mode, 'scenario': scenario.key, 'group': group, **cohort_metrics(rows)})
    cross_daily, time_sensitivity = entry_day_sensitivity(cross_rows)
    outputs = {
        "cross_by_entry_date.csv": cross_daily,
        "same_entry_day_weight_sensitivity.csv": time_sensitivity,
        'baseline_cross_assignments.csv': cross_rows, 'baseline_cross_metrics.csv': cross_quality,
        'cohort_quality.csv': cohort_quality, 'baseline_filter_quality.csv': direct,
        'stock_paired_results.csv': pairs, 'paired_summary.csv': pair_summaries,
        'account_contributions.csv': contributions, 'scenario_metrics.csv': full_metrics,
        'data_coverage.csv': coverage,
        'sampled_stocks.csv': [vars(stock) for stock in stocks],
        'cohort_trades.csv': [{'cost_mode': mode, 'scenario': scenario, **row}
                            for (mode, scenario), rows in cohorts.items() for row in rows],
        'signal_decisions.csv': [{'cost_mode': mode, **row}
                                for (mode, scenario), accumulator in accumulators.items() for row in accumulator.signal_decisions],
        'closed_trades.csv': [{'cost_mode': mode, **row}
                             for (mode, scenario), accumulator in accumulators.items() for row in accumulator.closed_trades],
    }
    for filename, rows in outputs.items():
        _write_csv(output / filename, rows)
    summary = {**protocol, 'complete': True, 'sample_size': len(stocks), 'market_day_count': len(dates),
        'entry_day_count': sum(day <= entry_end for day in dates),
        'followup_day_count': sum(day > entry_end for day in dates),
        'missing_minute_stock_days': sum(not row['complete_80_bars'] for row in coverage),
        'independent_strategy_oos': False, 'observation_overlaps_prior_research': True,
        'scenario_metrics': full_metrics, 'cohort_quality': cohort_quality,
        'cross_metrics': cross_quality, 'paired_summaries': pair_summaries,
        'entry_day_sensitivity': time_sensitivity}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    (output / 'report.md').write_text(render_report(summary), encoding='utf-8')
    return output / 'report.md'


def render_report(summary: dict[str, Any]) -> str:
    def pct(value):
        return '—' if value is None else f'{value:.2%}'
    lines = ['# ADX14/21固定五组：入组与自然退出观察', '',
        f"入组：{summary['entry_start']}至{summary['entry_end']}（{summary['entry_day_count']}日）；观察至{summary['observation_end']}，入组后另有{summary['followup_day_count']}个交易日。期初股票数{summary['sample_size']}。",
        '每股相同初始资金，全仓买卖并累计盈亏，入组终点后正常交易。入组交易按原买入信号日归组；未平仓交易单列，未强制退出。',
        '本轮是额外历史入组验证，观察期间与已使用研究窗口重叠，不能称整套策略独立OOS。旧75%金额门槛不改，不自动选定赢家。',
        f"缺少完整80根三分钟柱的股票交易日：{summary['missing_minute_stock_days']}；明细见data_coverage.csv。", '',
        '## 同批基线买点的ADX交叉诊断', '',
        '共同通过/仅14/仅21/共同拒绝仅作诊断，不组成新策略。任一ADX指标缺失单列。净收益率与亏损比例仅统计已平仓；全入组市值均值混合自然退出净收益和未平仓市值收益，未预扣未发生的卖出成本。', '',
        '| 成本 | 交叉组 | 入组/闭合/未平仓 | 闭合均值/中位数 | 闭合胜率 | 亏损超10% | 未平仓市值均值 | 全入组市值均值 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in summary['cross_metrics']:
        lines.append(f"| {row['cost_mode']} | {row['cross_group']} | {row['entry_trade_count']}/{row['closed_count']}/{row['open_count']} | {pct(row['closed_mean_net_return'])}/{pct(row['closed_median_net_return'])} | {pct(row['closed_win_rate'])} | {pct(row['closed_loss_over_10pct_rate'])} | {pct(row['open_mean_marked_return'])} | {pct(row['all_mean_asof_return'])} |")
    lines += ['', '## 各配置入组交易的完整跟踪', '',
        '| 成本 | 配置 | 入组/闭合/未平仓 | 闭合均值/中位数 | 全入组市值均值 |',
        '| --- | --- | ---: | ---: | ---: |']
    for row in summary['cohort_quality']:
        lines.append(f"| {row['cost_mode']} | {row['scenario_label']} | {row['entry_trade_count']}/{row['closed_count']}/{row['open_count']} | {pct(row['closed_mean_net_return'])}/{pct(row['closed_median_net_return'])} | {pct(row['all_mean_asof_return'])} |")
    lines += ['', '## 连续独立账户的逐股配对', '',
        '全部账户保留，无交易单笔收益为空。均值增量为百分点；账户结果包含入组窗口后的正常交易，不能当作单独入组交易收益。ADX21_vs_ADX14行以ADX14为参考，其余以MACD基线为参考。', '',
        '| 成本 | 估值日 | 比较 | 候选账户收益 | 增量均值/中位数（百分点） | 提高/不变/降低 |',
        '| --- | --- | --- | ---: | ---: | ---: |']
    for row in summary['paired_summaries']:
        lines.append(f"| {row['cost_mode']} | {row['checkpoint']} | {row['scenario']} | {pct(row['candidate_mean_account_return'])} | {row['mean_return_delta']*100:.2f}/{row['median_return_delta']*100:.2f} | {pct(row['improved_account_rate'])}/{pct(row['unchanged_account_rate'])}/{pct(row['worsened_account_rate'])} |")
    lines += ['', '账户状态贡献及已实现/未实现增量见account_contributions.csv，分母始终为全部账户。状态分组是事后描述，不能当成实时筛选规则。入组交易及指标见cohort_trades.csv和baseline_cross_assignments.csv。',
              '原8月31日期末账户的后续演化尚无同口径全市场数据支持；本报告6月起始的连续账户不能替代原7月起始账户的后续验证。', '']
    lines += ['## 入组日期权重的补充敏感性诊断', '',
              '此项为事后描述性核查，不是新增晋级门槛。对ADX21独有组减ADX14独有组的均值差，使用每日全部基线入组交易数作为共同权重；闭合口径只用当日基线已闭合数量。任一日期缺少分歧组样本时，加权差留空，不选择性删除日期。', '',
              '| 成本 | 口径 | 直接合并均值差 | 同入组日权重均值差 | 正差日期/两组有样本日期 |',
              '| --- | --- | ---: | ---: | ---: |']
    for row in summary.get('entry_day_sensitivity', []):
        lines.append(f"| {row['cost_mode']} | {row['measure']} | {pct(row['pooled_difference'])} | {pct(row['same_entry_day_weight_difference'])} | {row['positive_difference_day_count']}/{row['common_sample_day_count']} |")
    lines += ['', 'closed_or_marked口径仍含未平仓市值，不能视为最终闭合收益。逐日样本数见cross_by_entry_date.csv。', '']
    return '\n'.join(lines)


def main() -> None:
    print(run(build_argument_parser().parse_args()), flush=True)


if __name__ == '__main__':
    main()
