"""Run the frozen nine ADX exit variants and matched-entry shadow diagnostics."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from statistics import mean, median
from time import monotonic

from pymongo import MongoClient

from app.core.config import get_settings
from app.quant.cli.replay_factor_experiments import _effective_replay_start, _factor_calendar, _write_csv
from app.quant.cli.replay_sample import _sample_universe
from app.quant.cli.replay_stock import _load_daily_documents, _load_minute_bars, replay
from app.quant.core.models import Bar
from app.quant.data.market_data import DAILY_HISTORY_COLLECTION, THREE_MINUTE_HISTORY_COLLECTION
from app.quant.research.adx_comparison import ADX14, ADX21, checkpoint_account, cohort_trades
from app.quant.research.adx_exit import EXIT_GRID, ExitController
from app.quant.research.adx_exit_metrics import paired_exit, paired_exit_metrics, quality
from app.quant.research.factors import FactorBar, calculate_factor_snapshots
from app.quant.research.purification import paired_account_results
from app.quant.strategies.provisional_daily_macd_3m import official_backtest_config

PRIOR=Path('.local/quant/provisional_daily_macd_3m_v1/factor_experiments/adx_comparison5/2026-06-22_2026-06-30_observe_2026-08-31/nall_seed20260903')
START,END='2026-06-22','2026-08-31'
ROOT_MODULE=Path(__file__).resolve().parents[1]
MODES=('normal','double_cost')


def read_rows(path):
    with path.open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def init_worker(output, factor_dates, signal_dates, before_path):
    global DB, OUTPUT, FACTOR_DATES, SIGNAL_DATES, ORIGINAL
    settings=get_settings()
    client=MongoClient(settings.mongo_uri,serverSelectionTimeoutMS=10000)
    DB=client[settings.mongo_db_name]
    OUTPUT=Path(output);FACTOR_DATES=factor_dates;SIGNAL_DATES=signal_dates
    spec=importlib.util.spec_from_file_location('adx_exit_original_replay',before_path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    ORIGINAL=module.replay


def process_stock(stock):
    code=stock['code'];documents=_load_daily_documents(DB[DAILY_HISTORY_COLLECTION],code=code,through_date=END)
    name=str(documents[-1].get('name') or stock['name_at_start']) if documents else stock['name_at_start']
    bars=[Bar(str(r['trade_date']),float(r['open']),float(r['high']),float(r['low']),float(r['close'])) for r in documents]
    minutes=_load_minute_bars(DB[THREE_MINUTE_HISTORY_COLLECTION],code=code,start_date=START,end_date=END)
    factor_bars=[FactorBar(b.trade_date,b.high,b.low,b.close,0) for b in bars if FACTOR_DATES[0]<=b.trade_date<=FACTOR_DATES[-1]]
    snapshots=calculate_factor_snapshots(factor_bars,market_dates=FACTOR_DATES,signal_dates=SIGNAL_DATES)
    replay_start=_effective_replay_start(bars,requested_start=START,end_date=END)
    cache={};accounts=[];trades=[];pairs=[];results={};shadows=[];checks=0
    config=official_backtest_config(code=code)
    coverage=[{'code':code,'trade_date':day,'minute_count':len(minutes.get(day,[])),
               'complete':len(minutes.get(day,[]))==80} for day in SIGNAL_DATES if day in {b.trade_date for b in bars}]
    if any(not r['complete'] for r in coverage):
        raise ValueError(f'{code}: incomplete 3m history; inspect data before interpreting exits')
    for mode in MODES:
        cfg=config if mode=='normal' else replace(config,commission_rate=config.commission_rate*2,
                stamp_duty_rate=config.stamp_duty_rate*2,slippage_rate=config.slippage_rate*2)
        mode_results={}
        for variant in EXIT_GRID:
            scenario=ADX14 if variant.period==14 else ADX21
            gate=None if variant.period is None else lambda code,day,sc=scenario:(sc.accepts(snapshots.get(day)),sc.label)
            controller=ExitController(variant,snapshots) if variant.version!='E0' else None
            kwargs=dict(code=code,name=name,daily_bars=bars,minute_bars_by_date=minutes,start_date=replay_start,end_date=END,
                        config=cfg,buy_signal_gate=gate,official_strategy_configuration=mode=='normal')
            result=replay(**kwargs,exit_controller=controller,market_cache=cache,record_intraday=False) if replay_start else None
            if variant.version=='E0' and replay_start:
                old=ORIGINAL(**kwargs)
                del old['intraday_rows']
                new={k:v for k,v in result.items() if k!='intraday_rows'}
                if old!=new:raise AssertionError(f'E0 full regression failed {code} {mode} {variant.key}')
                checks+=1
            meta={'cost_mode':mode,'scenario':variant.key,'period':variant.period,'exit_version':variant.version}
            account=checkpoint_account(result,code=code,name=name,initial_cash=cfg.initial_cash,checkpoint=END)
            if result:
                account['exit_state_at_end']=result.get('exit_state_at_end','HOLDING' if account['end_holding'] else 'FLAT')
                stock_trades=cohort_trades(result,entry_start=START,entry_end=END,observation_end=END,minute_bars=minutes)
            else:
                account['exit_state_at_end']='FLAT';stock_trades=[]
            accounts.append({**meta,**account});trades.extend({**meta,**t} for t in stock_trades)
            mode_results[variant.key]=result
        for n in (14,21):
            ref=mode_results[f'adx{n}_E0']
            if not ref:continue
            ref_trades=cohort_trades(ref,entry_start=START,entry_end=END,observation_end=END,minute_bars=minutes)
            for reference in ref_trades:
                event=next(e for e in ref['event_rows'] if e['action']=='buy' and e['execution_at']==reference['entry_execution_at'])
                signal=next(s for s in ref['signal_rows'] if s['signal_id']==event['signal_id'])
                for variant in [v for v in EXIT_GRID if v.period==n and v.version!='E0']:
                    controller=ExitController(variant,snapshots)
                    shadow=replay(code=code,name=name,daily_bars=bars,minute_bars_by_date=minutes,
                        start_date=event['execution_at'][:10],end_date=END,
                        config=replace(cfg,initial_cash=event['cash_before']),
                        fixed_entry={'event':event,'signal':signal},exit_controller=controller,
                        market_cache=cache,record_intraday=False,official_strategy_configuration=False)
                    shadow_trades=cohort_trades(shadow,entry_start=START,entry_end=END,observation_end=END,minute_bars=minutes)
                    assert len(shadow_trades)==1 and shadow['event_rows'][0]==event
                    candidate=shadow_trades[0]
                    assert all(candidate[k]==reference[k] for k in ['entry_execution_at','entry_execution_price','shares','entry_notional','buy_commission'])
                    row=paired_exit(reference,candidate,reference_result=ref,candidate_result=shadow,minutes=minutes,market_dates=SIGNAL_DATES)
                    pairs.append({'cost_mode':mode,'period':n,'scenario':variant.key,'exit_version':variant.version,**row})
                    shadows.append({'cost_mode':mode,'scenario':variant.key,'entry_at':event['execution_at'],
                        'summary':shadow['summary'],'signal_rows':shadow['signal_rows'],'event_rows':shadow['event_rows'],
                        'attempt_rows':shadow['attempt_rows'],'exit_decision_rows':compact_decisions(controller.rows)})
        for result in mode_results.values():
            if result and 'exit_decision_rows' in result:
                result['exit_checked_bar_count']=len(result['exit_decision_rows'])
                result['exit_decision_rows']=compact_decisions(result['exit_decision_rows'])
        results[mode]=mode_results
    payload={'code':code,'name':name,'accounts':accounts,'trades':trades,'exit_pairs':pairs,'results':results,'shadows':shadows,
             'coverage':coverage,'e0_exact_checks':checks,
             'input_sha256':hashlib.sha256(json.dumps({'daily':[asdict(b) for b in bars],
                             'minutes':{d:[asdict(b) for b in m] for d,m in minutes.items()}},sort_keys=True).encode()).hexdigest(),
             'factor_snapshots':[{'signal_date':d,'completed_date':s.completed_date,
                  **{k:s.value(k) for k in ADX14.required_fields+ADX21.required_fields}} for d,s in snapshots.items()]}
    path=OUTPUT/'stocks'/f'{code}.json.gz';tmp=path.with_suffix('.tmp')
    with gzip.open(tmp,'wt',encoding='utf-8',compresslevel=3) as f:json.dump(payload,f,ensure_ascii=False,allow_nan=False)
    tmp.replace(path)
    return code,checks,len(pairs)


def compact_decisions(rows):
    """Save all actions/anomalies and one regular hold check per trading day."""
    seen=set();out=[]
    for row in rows:
        key=(row['entry_at'],row['at'][:10],row['state_after'])
        if row['action']!='hold' or row['data_anomaly'] or key not in seen:out.append(row)
        seen.add(key)
    return out


def aggregate(output, stocks, prior, protocol):
    accounts=[];trades=[];pairs=[];coverage=[];signals=[];events=[];decisions=[];checks=0;curve=defaultdict(float)
    for stock in stocks:
        with gzip.open(output/'stocks'/f'{stock["code"]}.json.gz','rt',encoding='utf-8') as f:p=json.load(f)
        accounts+=p['accounts'];trades+=p['trades'];pairs+=p['exit_pairs'];coverage+=p['coverage'];checks+=p['e0_exact_checks']
        for mode,rs in p['results'].items():
            for key,r in rs.items():
                meta={'cost_mode':mode,'scenario':key,'code':p['code']}
                if r:
                    signals.extend({**meta,**s} for s in r['signal_rows'])
                    events.extend({**meta,**e} for e in r['event_rows'])
                    decisions.extend({**meta,**x} for x in r.get('exit_decision_rows',[]) if x['action']!='hold' or x['data_anomaly'])
                by_day={d['trade_date']:d['total_assets'] for d in r['daily_rows']} if r else {}
                current=100000.
                for day in protocol['signal_dates']:
                    current=by_day.get(day,current);curve[mode,key,day]+=current
    mapping={'baseline':'baseline','adx14_E0':ADX14.key,'adx21_E0':ADX21.key}
    prior_pairs=read_rows(prior/'stock_paired_results.csv')
    previous={}
    for r in prior_pairs:
        if r['checkpoint']!=END or r['reference']!='baseline':continue
        for key,oldkey in mapping.items():
            if key!='baseline' and r['candidate']!=oldkey:continue
            prefix='baseline' if key=='baseline' else 'candidate'
            previous[r['cost_mode'],key,r['code']]={field:r[prefix+'_'+field] for field in ['final_assets','total_return','filled_buy_count','closed_trade_count','maximum_drawdown']}
    prior_checks=0
    for a in accounts:
        assert abs(a['total_return']-a['realized_return']-a['unrealized_return'])<1e-10
        if a['scenario'] in mapping:
            old=previous[a['cost_mode'],a['scenario'],a['code']]
            for field,value in old.items():
                assert abs(a[field]-float(value))<1e-9,(a['code'],a['cost_mode'],a['scenario'],field)
            prior_checks+=1
    prevsignals=defaultdict(list)
    for s in read_rows(prior/'signal_decisions.csv'):
        prevsignals[s['cost_mode'],s['scenario'],s['code']].append((s['signal_at'],s['final_status']))
    new_signals=defaultdict(list)
    for s in signals:
        if s['scenario'] in mapping and s['action']=='buy':new_signals[s['cost_mode'],mapping[s['scenario']],s['code']].append((s['signal_at'],s['final_status']))
    for a in accounts:
        if a['scenario'] in mapping:
            k=(a['cost_mode'],mapping[a['scenario']],a['code'])
            assert new_signals[k]==prevsignals[k],('prior buy signals',k)
    account_metrics=[];stock_pairs=[];pair_summaries=[];interaction=[];exit_stats=[]
    for mode in MODES:
        for variant in EXIT_GRID:
            group=[a for a in accounts if a['cost_mode']==mode and a['scenario']==variant.key]
            rows=[t for t in trades if t['cost_mode']==mode and t['scenario']==variant.key]
            assert len(group)==len(stocks)
            metric={'cost_mode':mode,'scenario':variant.key,'period':variant.period,'exit_version':variant.version,
                'account_count':len(group),'mean_account_return':mean(a['total_return'] for a in group),
                'median_account_return':median(a['total_return'] for a in group),
                'mean_account_drawdown':mean(a['maximum_drawdown'] for a in group),
                'mean_realized_return':mean(a['realized_return'] for a in group),
                'mean_unrealized_return':mean(a['unrealized_return'] for a in group),
                'no_buy_account_count':sum(a['filled_buy_count']==0 for a in group),
                'holding_account_count':sum(a['end_holding'] for a in group),
                'deferred_account_count':sum(a['exit_state_at_end']=='DEFERRED_EXIT' for a in group),
                'pending_exit_account_count':sum(a['exit_state_at_end']=='EXIT_PENDING' for a in group),**quality(rows)}
            account_metrics.append(metric)
        for n in (14,21):
            base=[a for a in accounts if a['cost_mode']==mode and a['scenario']==f'adx{n}_E0']
            for version in ['E1','E2','E3']:
                key=f'adx{n}_{version}'
                cand=[a for a in accounts if a['cost_mode']==mode and a['scenario']==key]
                # Metadata is not an account field: strip it before pairing.
                strip=lambda rs:[{k:v for k,v in r.items() if k not in ['cost_mode','scenario','period','exit_version']} for r in rs]
                details,summary=paired_account_results(strip(base),strip(cand),scenario=key,scenario_label=key)
                meta={'cost_mode':mode,'period':n,'scenario':key,'reference':f'adx{n}_E0'}
                stock_pairs.extend({**meta,**r} for r in details)
                summary.update(mean_realized_delta=mean(r['candidate_realized_return']-r['baseline_realized_return'] for r in details),
                               mean_unrealized_delta=mean(r['candidate_unrealized_return']-r['baseline_unrealized_return'] for r in details))
                pair_summaries.append({**meta,**summary})
                g=[r for r in pairs if r['cost_mode']==mode and r['scenario']==key]
                for mechanism in ['all','early','deferred','original_or_untriggered']:
                    h=g if mechanism=='all' else [r for r in g if r['mechanism']==mechanism]
                    exit_stats.append({**meta,'mechanism':mechanism,**paired_exit_metrics(h)})
            by_key={(a['scenario'],a['code']):a for a in accounts if a['cost_mode']==mode and a['period']==n}
            for stock in stocks:
                vals={e:by_key[f'adx{n}_{e}',stock['code']]['total_return'] for e in ['E0','E1','E2','E3']}
                interaction.append({'cost_mode':mode,'period':n,'code':stock['code'],
                    'interaction':vals['E3']-vals['E1']-vals['E2']+vals['E0'],
                    'E3_minus_E1':vals['E3']-vals['E1'],'E3_minus_E2':vals['E3']-vals['E2']})
    outputs={'accounts.csv':accounts,'account_metrics.csv':account_metrics,'stock_paired_results.csv':stock_pairs,
             'account_paired_summary.csv':pair_summaries,'trades.csv':trades,'exit_pairs.csv':pairs,
             'exit_paired_summary.csv':exit_stats,'interactions.csv':interaction,'data_coverage.csv':coverage,
             'signals.csv':signals,'events.csv':events,'exit_decisions.csv':decisions,
             'daily_equity.csv':[{'cost_mode':m,'scenario':s,'trade_date':d,'total_assets':v,'mean_return':v/(len(stocks)*100000)-1} for (m,s,d),v in sorted(curve.items())]}
    for name,rows in outputs.items():_write_csv(output/name,rows)
    validation={'passed':True,'sample_size':len(stocks),'scenario_count':9,'cost_modes':list(MODES),
        'e0_full_result_regression_checks':checks,'prior_report_account_checks':prior_checks,
        'prior_report_buy_signals_match':True,'same_entry_exact_fields_checked':True,
        'all_accounts_included':len(accounts)==len(stocks)*18,'factor_cutoff':'t-1 vs t-4',
        'all_available_daily_rows_have_80_bars':all(r['complete'] for r in coverage),
        'realized_plus_unrealized_reconciles':True,'shadow_pairs_not_account_trades':True,
        'normal_and_double_cost_decisions_independently_replayed':True}
    (output/'validation.json').write_text(json.dumps(validation,ensure_ascii=False,indent=2)+'\n')
    interaction_stats=[{'cost_mode':m,'period':n,**{k:mean(r[k] for r in interaction if r['cost_mode']==m and r['period']==n) for k in ['interaction','E3_minus_E1','E3_minus_E2']}} for m in MODES for n in [14,21]]
    summary={'validation':validation,'account_metrics':account_metrics,'account_paired_summary':pair_summaries,
             'exit_paired_summary':exit_stats,'interaction_summary':interaction_stats}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (output/'report.md').write_text(render_report(summary),encoding='utf-8')
    protocol['complete']=True
    (output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    return summary


def render_report(s):
    pct=lambda v:'—' if v is None else f'{v:.4%}'
    lines=['# ADX退出九组开发回测','',f"区间{START}至{END}，{s['validation']['sample_size']}只期初股票，各10万元独立资金；正常与双倍成本分别完整回放。开发窗口，不是独立OOS。",'',
        '## 一级：完整账户','', '| 成本 | 版本 | 平均账户收益 | 已实现/未实现 | 平均单股回撤 | 闭合/未完成 |', '| --- | --- | ---: | ---: | ---: | ---: |']
    for r in s['account_metrics']:lines.append(f"| {r['cost_mode']} | {r['scenario']} | {pct(r['mean_account_return'])} | {pct(r['mean_realized_return'])}/{pct(r['mean_unrealized_return'])} | {pct(r['mean_account_drawdown'])} | {r['closed_count']}/{r['open_count']} |")
    lines+=['','## 同周期相对E0的账户增量','', '| 成本 | 候选 | 均值/中位增量 | 提高/不变/降低 | 已实现/未实现贡献 | 平均单股回撤差 |','| --- | --- | ---: | ---: | ---: | ---: |']
    for r in s['account_paired_summary']:lines.append(f"| {r['cost_mode']} | {r['scenario']} | {pct(r['mean_return_delta'])}/{pct(r['median_return_delta'])} | {pct(r['improved_account_rate'])}/{pct(r['unchanged_account_rate'])}/{pct(r['worsened_account_rate'])} | {pct(r['mean_realized_delta'])}/{pct(r['mean_unrealized_delta'])} | {pct(r['mean_maximum_drawdown_delta'])} |")
    lines+=['','增量百分比均为百分点差。无交易账户保留在全部账户分母中；无闭合交易的单笔收益为空。','',
        '## 二级：同买入退出诊断','', '每笔固定E0实际买入的时点、价格、股数和费用，单独模拟退出，不进行后续买入；可重叠影子交易不计入账户。闭合差只在双方自然退出子样本计算。全样本差包含共同截止日未卖出市值，没有预扣未发生卖出费用。','',
        '| 成本 | 版本 | 机制 | 配对/双方闭合 | 闭合覆盖率 | 闭合差均值/中位 | 全样本估值差 | 候选未完成 |','| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for r in s['exit_paired_summary']:
        if r['mechanism']!='original_or_untriggered':lines.append(f"| {r['cost_mode']} | {r['scenario']} | {r['mechanism']} | {r['pair_count']}/{r['both_closed_count']} | {pct(r['both_closed_coverage'])} | {pct(r['closed_mean_delta'])}/{pct(r['closed_median_delta'])} | {pct(r['asof_mean_delta'])} | {r['candidate_open_count']} |")
    lines+=['','## 交易质量与尾部','', '| 成本 | 版本 | 闭合均值/中位 | 闭合亏损>10%/ES95 | 全样本估值亏损>10%/ES95 | 全样本MAE均值 |','| --- | --- | ---: | ---: | ---: | ---: |']
    for r in s['account_metrics']:lines.append(f"| {r['cost_mode']} | {r['scenario']} | {pct(r['closed_mean_net_return'])}/{pct(r['closed_median_net_return'])} | {pct(r['closed_loss_over10_rate'])}/{pct(r['closed_es95'])} | {pct(r['all_asof_loss_over10_rate'])}/{pct(r['all_asof_es95'])} | {pct(r['all_mean_mae'])} |")
    lines+=['','## 提前与延期的交互','', '| 成本 | 周期 | Interaction | E3−E1 | E3−E2 |', '| --- | --- | ---: | ---: | ---: |']
    for r in s['interaction_summary']:lines.append(f"| {r['cost_mode']} | {r['period']} | {pct(r['interaction'])} | {pct(r['E3_minus_E1'])} | {pct(r['E3_minus_E2'])} |")
    lines+=['','所有触发与成交分别记录在signals.csv/events.csv；延期/提前状态在exit_decisions.csv。每股stocks/*.json.gz还保存每日账户、全部信号、成交尝试、每交易日状态检查及每个影子交易的审计记录。常规持续持有检查按日留一条，状态动作与数据异常全部保留。',
            '延期盈利转亏、额外不利波动、实际早卖/晚卖、延期时长及未完成数量见exit_paired_summary.csv及exit_pairs.csv；不得用估计清算报价代替实际成交。',
            '研究模块默认关闭，未接入实盘。没有为“尾部明显恶化”事后设置阈值，也没有自动晋级。', '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--prior',type=Path,default=PRIOR)
    parser.add_argument('--reference-source',type=Path,default=Path('.local/adx_exit_v1_source/replay_stock_before.py'))
    parser.add_argument('--workers',type=int,default=6)
    parser.add_argument('--codes',help='仅用于开发烟雾测试；正式运行留空')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args();start=monotonic()
    stocks=read_rows(args.prior/'sampled_stocks.csv')
    if len(stocks)!=5496 or len({r['code'] for r in stocks})!=5496:raise ValueError('原期初股票池数量不符')
    settings=get_settings();client=MongoClient(settings.mongo_uri,serverSelectionTimeoutMS=10000)
    try:
        daily=client[settings.mongo_db_name][DAILY_HISTORY_COLLECTION]
        universe=_sample_universe(daily,start_date=START)
        assert {s[0] for s in universe}=={r['code'] for r in stocks},'期初股票池发生变化'
        factor_dates,signal_dates=_factor_calendar(daily,start_date=START,end_date=END)
    finally:client.close()
    if args.codes:
        codes=set(args.codes.split(','));stocks=[s for s in stocks if s['code'] in codes]
        assert len(stocks)==len(codes),'unknown smoke code'
    output=args.output;output.mkdir(parents=True,exist_ok=True);(output/'stocks').mkdir(exist_ok=True)
    source_paths=[Path(__file__),ROOT_MODULE/'cli/replay_stock.py',ROOT_MODULE/'research/adx_exit.py',
                  ROOT_MODULE/'research/adx_exit_metrics.py',ROOT_MODULE/'research/ADX_EXIT_RESEARCH_V1.md',
                  ROOT_MODULE/'research/ADX_EXIT_GRID_V1.json',args.reference_source,
                  args.prior/'sampled_stocks.csv',args.prior/'protocol.json',args.prior/'validation.json']
    hashes={str(p):digest(p) for p in source_paths}
    protocol={'start_date':START,'end_date':END,'sample_size':len(stocks),'development_not_oos':True,
              'prior':str(args.prior.resolve()),'source_hashes':hashes,'factor_dates':factor_dates,'signal_dates':signal_dates,
              'grid':[dict(asdict(v),key=v.key) for v in EXIT_GRID], 'complete':False}
    if (output/'protocol.json').exists():
        old=json.loads((output/'protocol.json').read_text())
        if not args.resume:raise ValueError('结果目录已存在，请使用新目录或--resume')
        assert old['source_hashes']==hashes and old['sample_size']==len(stocks),'不能混用不同代码/股票池的分片'
    (output/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    _write_csv(output/'sampled_stocks.csv',stocks)
    (output/'ADX_EXIT_RESEARCH_V1.md').write_bytes((ROOT_MODULE/'research/ADX_EXIT_RESEARCH_V1.md').read_bytes())
    (output/'ADX_EXIT_GRID_V1.json').write_bytes((ROOT_MODULE/'research/ADX_EXIT_GRID_V1.json').read_bytes())
    todo=[s for s in stocks if not (output/'stocks'/f'{s["code"]}.json.gz').exists()]
    done=len(stocks)-len(todo)
    with ProcessPoolExecutor(max_workers=args.workers,initializer=init_worker,
            initargs=(str(output),factor_dates,signal_dates,str(args.reference_source.resolve()))) as pool:
        futures=[pool.submit(process_stock,s) for s in todo]
        for future in as_completed(futures):
            future.result();done+=1
            if done%25==0 or done==len(stocks):print(f'adx_exit completed={done}/{len(stocks)} elapsed={monotonic()-start:.1f}s',flush=True)
    summary=aggregate(output,stocks,args.prior,protocol)
    print(json.dumps({'output':str(output.resolve()),'validation':summary['validation'],'elapsed_s':monotonic()-start},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
