"""Compare ADX14 E2, original MACD and buy-and-hold with independent accounts."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from time import monotonic

from pymongo import MongoClient

from app.core.config import get_settings
from app.quant.cli.replay_adx_exits import compact_decisions, read_rows
from app.quant.cli.replay_factor_experiments import _effective_replay_start, _write_csv
from app.quant.cli.replay_sample import _sample_universe, maximum_drawdown
from app.quant.cli.replay_stock import (
    _buy_size, _execution_price, _load_daily_documents, _load_minute_bars, replay,
)
from app.quant.core.execution import money
from app.quant.core.models import Bar
from app.quant.data.market_data import DAILY_HISTORY_COLLECTION, THREE_MINUTE_HISTORY_COLLECTION
from app.quant.research.adx_comparison import ADX14, checkpoint_account
from app.quant.research.adx_exit import ExitController, ExitVariant
from app.quant.research.factors import FactorBar, calculate_factor_snapshots
from app.quant.runtime.daily_flow import at_daily_price_limit
from app.quant.strategies.provisional_daily_macd_3m import official_backtest_config

DEFAULT_SOURCE = Path('.local/quant/provisional_daily_macd_3m_v1/exit_experiments/adx_exit_v1_full')
LABELS = {'adx14_E2': 'MACD＋ADX14买入＋E2延迟卖出', 'baseline': '原MACD', 'buy_hold': '期初买入持有'}


def buy_and_hold(*, code, name, daily_bars, minutes, start_date, end_date, config):
    """Enter at the first tradable bar open; round/charge exactly like replay.

    The buy instruction exists before the window. Limit-up bars are retried;
    insufficient cash leaves the account flat. No sale or terminal sale fee.
    Minute timestamps, as in the existing engine, identify the bar's END.
    """
    cash, shares, cost = config.initial_cash, 0, 0.
    entry = None
    rows = []
    attempts = []
    unaffordable = False
    for index, day in enumerate(daily_bars):
        if not start_date <= day.trade_date <= end_date:
            continue
        if not shares and not unaffordable:
            for bar in minutes.get(day.trade_date, []):
                if index == 0:
                    attempts.append({'bar_end': bar.trade_date, 'status': 'missing_previous_close'})
                    break
                if at_daily_price_limit(action='buy', code=code, name=name,
                        trade_date=day.trade_date, previous_close=daily_bars[index - 1].close,
                        price=bar.open):
                    attempts.append({'bar_end': bar.trade_date, 'status': 'limit_up'})
                    continue
                price = _execution_price(action='buy', reference=bar.open, bar=bar,
                                         slippage_rate=config.slippage_rate)
                shares, notional, commission = _buy_size(cash=cash, execution_price=price, config=config)
                if not shares:
                    unaffordable = True
                    attempts.append({'bar_end': bar.trade_date, 'status': 'insufficient_cash_for_one_lot'})
                    break
                cost = money(notional + commission)
                cash = money(cash - cost)
                entry = {'code': code, 'name': name, 'entry_bar_end': bar.trade_date,
                         'reference_open': bar.open, 'execution_price': price,
                         'shares': shares, 'notional': notional, 'commission': commission,
                         'cash_after': cash, 'delayed_from_start': day.trade_date != start_date,
                         'prior_attempt_count': len(attempts)}
                break
        market_value = money(shares * day.close)
        rows.append({'trade_date': day.trade_date, 'total_assets': money(cash + market_value),
                     'cash_at_close': cash, 'market_value': market_value,
                     'unrealized_pnl': money(market_value - cost) if shares else 0.,
                     'realized_pnl_cumulative': 0., 'shares_at_close': shares})
    if not rows:
        raise ValueError(f'{code}: no daily prices in the comparison window')
    account = {'code': code, 'name': name, 'initial_cash': config.initial_cash,
               'final_assets': rows[-1]['total_assets'],
               'total_return': rows[-1]['total_assets'] / config.initial_cash - 1,
               'maximum_drawdown': maximum_drawdown([config.initial_cash] + [r['total_assets'] for r in rows]),
               'filled_buy_count': int(bool(shares)), 'closed_trade_count': 0,
               'mean_net_return': None, 'end_holding': bool(shares), 'realized_return': 0.,
               'unrealized_return': rows[-1]['unrealized_pnl'] / config.initial_cash,
               'last_valuation_date': rows[-1]['trade_date']}
    return {'account': account, 'daily_rows': rows, 'entry': entry, 'attempts': attempts}


def init_worker(source, output, protocol):
    global DB, SOURCE, OUTPUT, PROTOCOL
    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10000)
    DB = client[settings.mongo_db_name]
    SOURCE, OUTPUT, PROTOCOL = Path(source), Path(output), protocol


def process_stock(stock):
    code = stock['code']
    start, end = PROTOCOL['start_date'], PROTOCOL['end_date']
    documents = _load_daily_documents(DB[DAILY_HISTORY_COLLECTION], code=code, through_date=end)
    name = str(documents[-1].get('name') or stock['name_at_start'])
    bars = [Bar(str(r['trade_date']), *(float(r[k]) for k in ('open', 'high', 'low', 'close'))) for r in documents]
    minutes = _load_minute_bars(DB[THREE_MINUTE_HISTORY_COLLECTION], code=code, start_date=start, end_date=end)
    with gzip.open(SOURCE / 'stocks' / f'{code}.json.gz', 'rt') as f:
        prior = json.load(f)
    input_hash = hashlib.sha256(json.dumps({'daily': [asdict(b) for b in bars],
        'minutes': {d: [asdict(b) for b in m] for d, m in minutes.items()}}, sort_keys=True).encode()).hexdigest()
    if input_hash != prior['input_sha256']:
        raise ValueError(f'{code}: historical input changed since the frozen exit study')
    daily_dates = {b.trade_date for b in bars}
    covered = [d for d in PROTOCOL['signal_dates'] if d in daily_dates]
    if any(len(minutes.get(d, [])) != 80 for d in covered):
        raise ValueError(f'{code}: incomplete 3-minute history')
    factor_dates = PROTOCOL['factor_dates']
    factor_bars = [FactorBar(b.trade_date, b.high, b.low, b.close, 0) for b in bars
                   if factor_dates[0] <= b.trade_date <= factor_dates[-1]]
    snapshots = calculate_factor_snapshots(factor_bars, market_dates=factor_dates,
                                          signal_dates=PROTOCOL['signal_dates'])
    effective = _effective_replay_start(bars, requested_start=start, end_date=end)
    config = official_backtest_config(code=code)
    results, accounts, cache = {}, [], {}
    for scenario in ('baseline', 'adx14_E2'):
        gate = None if scenario == 'baseline' else lambda c, day: (ADX14.accepts(snapshots.get(day)), ADX14.label)
        controller = ExitController(ExitVariant(14, 'E2'), snapshots) if scenario == 'adx14_E2' else None
        result = replay(code=code, name=name, daily_bars=bars, minute_bars_by_date=minutes,
            start_date=effective, end_date=end, config=config, buy_signal_gate=gate,
            exit_controller=controller, market_cache=cache, record_intraday=False) if effective else None
        old = prior['results']['normal'][scenario]
        if (old is None) != (result is None):
            raise AssertionError(f'{code}: replay availability changed')
        if result:
            for key in ('summary', 'daily_rows', 'signal_rows', 'event_rows', 'attempt_rows', 'closed_trade_rows'):
                if result[key] != old[key]:
                    raise AssertionError(f'{code} {scenario}: prior {key} mismatch')
            if controller:
                result['exit_decision_rows'] = compact_decisions(controller.rows)
                if result['exit_decision_rows'] != old['exit_decision_rows']:
                    raise AssertionError(f'{code}: E2 decisions changed')
        account = checkpoint_account(result, code=code, name=name, initial_cash=config.initial_cash, checkpoint=end)
        accounts.append({'scenario': scenario, **account})
        results[scenario] = result
    hold = buy_and_hold(code=code, name=name, daily_bars=bars, minutes=minutes,
                       start_date=start, end_date=end, config=config)
    accounts.append({'scenario': 'buy_hold', **hold.pop('account')})
    results['buy_hold'] = hold
    payload = {'code': code, 'name': name, 'input_sha256': input_hash, 'accounts': accounts,
               'results': results, 'covered_stock_days': len(covered),
               'missing_market_dates': [d for d in PROTOCOL['signal_dates'] if d not in daily_dates]}
    path = OUTPUT / 'stocks' / f'{code}.json.gz'
    temporary = path.with_suffix('.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=3) as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False)
    temporary.replace(path)
    return code


def aggregate(output, stocks, protocol):
    accounts, entries, comparisons, anomalies = [], [], [], []
    daily = defaultdict(lambda: defaultdict(float))
    covered_days = 0
    for stock in stocks:
        with gzip.open(output / 'stocks' / f'{stock["code"]}.json.gz', 'rt') as f:
            payload = json.load(f)
        accounts.extend(payload['accounts'])
        covered_days += payload['covered_stock_days']
        mapping = {r['scenario']: r for r in payload['accounts']}
        row = {'code': payload['code'], 'name': payload['name']}
        for scenario, account in mapping.items():
            for field in ('final_assets', 'total_return', 'maximum_drawdown', 'filled_buy_count', 'end_holding'):
                row[f'{scenario}_{field}'] = account[field]
            if money(account['final_assets'] - account['initial_cash']) != money(
                    (account['realized_return'] + account['unrealized_return']) * account['initial_cash']):
                raise AssertionError(f'{payload["code"]}: cash reconciliation failed')
            result = payload['results'][scenario]
            by_day = {r['trade_date']: r for r in result['daily_rows']} if result else {}
            state = {'total_assets': account['initial_cash'], 'cash_at_close': account['initial_cash'],
                     'market_value': 0., 'realized_pnl_cumulative': 0., 'unrealized_pnl': 0.}
            for day in protocol['signal_dates']:
                state = by_day.get(day, state)
                for field in state.keys() & {'total_assets', 'cash_at_close', 'market_value', 'realized_pnl_cumulative', 'unrealized_pnl'}:
                    daily[scenario, day][field] += state[field]
        for left, right in (('adx14_E2', 'baseline'), ('adx14_E2', 'buy_hold'), ('baseline', 'buy_hold')):
            row[f'{left}_minus_{right}_pnl'] = money(mapping[left]['final_assets'] - mapping[right]['final_assets'])
        comparisons.append(row)
        entry = payload['results']['buy_hold']['entry']
        entries.append(entry or {'code': payload['code'], 'name': payload['name'], 'entry_bar_end': None})
        anomalies.extend({'code': payload['code'], 'trade_date': d, 'issue': 'no_daily_bar_carry_last_mark'}
                         for d in payload['missing_market_dates'])
    capital = money(len(stocks) * 100000)
    curve, metrics = [], []
    for scenario, label in LABELS.items():
        selected = [r for r in accounts if r['scenario'] == scenario]
        if len(selected) != len(stocks) or len({r['code'] for r in selected}) != len(stocks):
            raise AssertionError('All three scenarios must include the entire same universe')
        for day in protocol['signal_dates']:
            state = {k: money(v) for k, v in daily[scenario, day].items()}
            if abs(state['total_assets'] - state['cash_at_close'] - state['market_value']) > .011:
                raise AssertionError('Aggregate cash and market value do not reconcile')
            curve.append({'scenario': scenario, 'trade_date': day, **state,
                          'total_return': state['total_assets'] / capital - 1})
        final = money(sum(r['final_assets'] for r in selected))
        if final != money(daily[scenario, protocol['end_date']]['total_assets']):
            raise AssertionError('Daily equity does not reconcile to stock accounts')
        realized = money(sum(r['realized_return'] * r['initial_cash'] for r in selected))
        pnl = money(final - capital)
        metrics.append({'scenario': scenario, 'label': label, 'stock_count': len(selected),
            'initial_capital': capital, 'final_assets': final, 'total_pnl': pnl, 'total_return': pnl / capital,
            'mean_pnl_per_stock': pnl / len(selected), 'median_account_return': median(r['total_return'] for r in selected),
            'realized_pnl': realized, 'unrealized_pnl': money(pnl - realized),
            'portfolio_maximum_drawdown': maximum_drawdown([capital] + [daily[scenario, d]['total_assets'] for d in protocol['signal_dates']]),
            'mean_stock_maximum_drawdown': mean(r['maximum_drawdown'] for r in selected),
            'profitable_accounts': sum(r['total_return'] > 1e-12 for r in selected),
            'losing_accounts': sum(r['total_return'] < -1e-12 for r in selected),
            'unchanged_accounts': sum(abs(r['total_return']) <= 1e-12 for r in selected),
            'no_buy_accounts': sum(r['filled_buy_count'] == 0 for r in selected),
            'holding_accounts': sum(r['end_holding'] for r in selected),
            'buy_count': sum(r['filled_buy_count'] for r in selected),
            'sell_count': sum(r['closed_trade_count'] for r in selected)})
    paired = []
    for left, right in (('adx14_E2', 'baseline'), ('adx14_E2', 'buy_hold'), ('baseline', 'buy_hold')):
        differences = [r[f'{left}_minus_{right}_pnl'] for r in comparisons]
        paired.append({'left': left, 'right': right, 'total_pnl_delta': money(sum(differences)),
                       'return_delta': sum(differences) / capital,
                       'improved_accounts': sum(x > 0 for x in differences),
                       'equal_accounts': sum(x == 0 for x in differences),
                       'worsened_accounts': sum(x < 0 for x in differences)})
    validation = {'passed': True, 'stock_count': len(stocks), 'independent_account_count': len(accounts),
                  'all_accounts_initial_100000': all(r['initial_cash'] == 100000 for r in accounts),
                  'input_hashes_match_prior': len(stocks), 'strategy_full_result_matches_prior': len(stocks) * 2,
                  'complete_80_bar_stock_days': covered_days, 'missing_daily_stock_days': len(anomalies),
                  'daily_equity_and_pnl_reconcile': True,
                  'buy_hold_bought_on_start_date': sum(bool(r['entry_bar_end']) and r['entry_bar_end'][:10] == protocol['start_date'] for r in entries),
                  'buy_hold_bought_after_start_date': sum(bool(r['entry_bar_end']) and r['entry_bar_end'][:10] > protocol['start_date'] for r in entries),
                  'buy_hold_no_buy_accounts': sum(not r['entry_bar_end'] for r in entries)}
    summary = {'start_date': protocol['start_date'], 'end_date': protocol['end_date'],
               'requested_asof_date': protocol['requested_asof_date'], 'trade_day_count': len(protocol['signal_dates']),
               'stock_count': len(stocks), 'initial_cash_per_stock': 100000,
               'initial_capital_per_scenario': capital, 'metrics': metrics, 'paired': paired,
               'data_availability': protocol['data_availability'], 'validation': validation}
    for filename, rows in (('accounts.csv', accounts), ('stock_comparison.csv', comparisons),
            ('scenario_metrics.csv', metrics), ('paired_summary.csv', paired), ('daily_equity.csv', curve),
            ('buy_hold_entries.csv', entries), ('missing_daily_bars.csv', anomalies)):
        _write_csv(output / filename, rows)
    for filename, obj in (('summary.json', summary), ('validation.json', validation)):
        (output / filename).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
    (output / 'report.md').write_text(render_report(summary), encoding='utf-8')
    return summary


def render_report(s):
    lines = ['# 全股票三组独立账户收益比较', '',
        f"区间：{s['start_date']}至{s['end_date']}，{s['trade_day_count']}个交易日。期初股票{s['stock_count']:,}只，每只独立100,000元，每组本金{s['initial_capital_per_scenario']:,.2f}元。",
        f"请求截至{s['requested_asof_date']}；同口径全市场日线和三分钟数据只完整覆盖到{s['end_date']}，未把后续少数股票当作全市场，也未虚构缺失日期收益。", '',
        '三组使用相同qfq行情、期初股票池、100股整手、佣金万一、买卖不利滑点万五；实际卖出收取万五印花税。账户资金不跨股票转移，未交易账户仍计入分母；策略账户盈亏在本股内累计。',
        '期初买入持有组在开始日第一根三分钟柱开盘买入；涨停时等待首根非涨停柱，买不起一手则保留现金。使用原引擎的滑点及价格范围约束。记录中的三分钟时间戳是柱结束标记，开盘实际早3分钟。',
        '期末不强制卖出，持仓按最后可用日线收盘价估值，不预扣未发生卖出费用；三组总收益均包含已实现和未实现盈亏。缺少当日日线的股票沿用最后估值并保留异常明细。', '',
        '| 方案 | 期末资产（元） | 总盈亏（元） | 总收益率 | 每股平均盈亏（元） |',
        '| --- | ---: | ---: | ---: | ---: |']
    for r in s['metrics']:
        lines.append(f"| {r['label']} | {r['final_assets']:,.2f} | {r['total_pnl']:+,.2f} | {r['total_return']:.4%} | {r['mean_pnl_per_stock']:+,.2f} |")
    lines += ['', '| 方案 | 已实现盈亏（元） | 未实现盈亏（元） | 汇总日终净值最大回撤 | 平均单股回撤 | 盈利/亏损/持平账户 | 未买入/期末持仓 |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in s['metrics']:
        lines.append(f"| {r['label']} | {r['realized_pnl']:+,.2f} | {r['unrealized_pnl']:+,.2f} | {r['portfolio_maximum_drawdown']:.4%} | {r['mean_stock_maximum_drawdown']:.4%} | {r['profitable_accounts']}/{r['losing_accounts']}/{r['unchanged_accounts']} | {r['no_buy_accounts']}/{r['holding_accounts']} |")
    lines += ['', '汇总回撤由全部独立账户每日资产之和计算，单股平均回撤是另一口径；均未计盘中净值回撤。', '',
              '| 比较 | 盈亏差额（元） | 收益率差（百分点） | 逐股提高/持平/降低 |',
              '| --- | ---: | ---: | ---: |']
    for r in s['paired']:
        lines.append(f"| {LABELS[r['left']]} − {LABELS[r['right']]} | {r['total_pnl_delta']:+,.2f} | {r['return_delta']*100:+.4f} | {r['improved_accounts']}/{r['equal_accounts']}/{r['worsened_accounts']} |")
    v = s['validation']
    lines += ['', '新策略：原MACD有效买点加ADX14[t−1]≥20且ADX14[t−1]>ADX14[t−4]。原卖点形成后，仅Strong、估算清算净收益>0、临时日线DIF>0且H>0时进入E2延期；延期条件任一失效即提交退出。沿用原信号次柱撮合及T+1，不启用E1提前退出。',
        f"买入持有执行核对：起点日买入{v['buy_hold_bought_on_start_date']}只，后续日期才买入{v['buy_hold_bought_after_start_date']}只，未买入{v['buy_hold_no_buy_accounts']}只。",
        f"独立重新回放两组策略，共{v['strategy_full_result_matches_prior']:,}条路径与既有研究的日终账户、信号、撮合尝试、实际成交和闭合交易逐项一致；全部{v['input_hashes_match_prior']:,}只行情输入哈希一致。三组逐股及每日资产、现金和盈亏已对账。",
        '该窗口已参与策略研究，属于历史开发回测。新规则只用于本次比较运行，正式影子盘配置未在本次修改。', '',
        '逐股结果：stock_comparison.csv；独立账户：accounts.csv；每日曲线：daily_equity.csv；买入持有成交：buy_hold_entries.csv；检查结果：validation.json；逐股重放：stocks/*.json.gz。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--codes', help='Optional smoke-test subset; omit for full universe')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve()
    prior = json.loads((source / 'protocol.json').read_text())
    if not prior['complete'] or not json.loads((source / 'validation.json').read_text())['passed']:
        raise ValueError('Source study must be complete and validated')
    stocks = read_rows(source / 'sampled_stocks.csv')
    settings = get_settings()
    with MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10000) as client:
        db = client[settings.mongo_db_name]
        universe = _sample_universe(db[DAILY_HISTORY_COLLECTION], start_date=prior['start_date'])
        if {s[0] for s in universe} != {r['code'] for r in stocks}:
            raise ValueError('Start-date universe changed')
        availability = {}
        for name in (DAILY_HISTORY_COLLECTION, THREE_MINUTE_HISTORY_COLLECTION):
            availability[name] = list(db[name].aggregate([
                {'$match': {'adjust': 'qfq', 'trade_date': {'$gte': prior['end_date']}}},
                {'$group': {'_id': '$trade_date', 'row_count': {'$sum': 1}}}, {'$sort': {'_id': 1}}]))
    if args.codes:
        codes = set(args.codes.split(','))
        stocks = [s for s in stocks if s['code'] in codes]
        if len(stocks) != len(codes):
            raise ValueError('Unknown stock code in smoke subset')
    stocks.sort(key=lambda r: r['code'])
    output = args.output.resolve()
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in Path('app/quant').rglob('*.py')}
    protocol = {k: prior[k] for k in ('start_date', 'end_date', 'factor_dates', 'signal_dates')}
    protocol.update(requested_asof_date=date.today().isoformat(), source=str(source),
                    source_protocol_sha256=hashlib.sha256((source / 'protocol.json').read_bytes()).hexdigest(),
                    source_hashes=hashes, stocks=[s['code'] for s in stocks],
                    data_availability=availability, complete=False)
    if (output / 'protocol.json').exists():
        old = json.loads((output / 'protocol.json').read_text())
        if not args.resume or any(old[k] != protocol[k] for k in ('source_hashes', 'stocks', 'source_protocol_sha256')):
            raise ValueError('Use a new output directory or resume unchanged code and inputs')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'stocks').mkdir(exist_ok=True)
    (output / 'protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + '\n')
    _write_csv(output / 'sampled_stocks.csv', stocks)
    start = monotonic()
    todo = [s for s in stocks if not (output / 'stocks' / f'{s["code"]}.json.gz').exists()]
    done = len(stocks) - len(todo)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker,
            initargs=(str(source), str(output), protocol)) as pool:
        futures = [pool.submit(process_stock, s) for s in todo]
        for future in as_completed(futures):
            future.result()
            done += 1
            if done % 100 == 0 or done == len(stocks):
                print(f'comparison completed={done}/{len(stocks)} elapsed={monotonic()-start:.1f}s', flush=True)
    summary = aggregate(output, stocks, protocol)
    protocol['complete'] = True
    (output / 'protocol.json').write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(output), 'metrics': summary['metrics'],
                      'validation': summary['validation'], 'elapsed_s': monotonic() - start}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
