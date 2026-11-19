"""Build a factor panel and compare fixed-protocol portfolios with SSE Composite.

Explicit prepared files can be replayed without fetching data. Database writes
and changes to the production strategy are deliberately outside this CLI.
"""
from __future__ import annotations

import argparse
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import warnings

import pandas as pd

from app.quant.research.factor_features import build_daily_factors, signal_features
from app.quant.research.factor_context import build_observation_factors
from app.quant.research.factor_alpha_study import run_study
from app.quant.research.factor_alpha_report import render_report
from app.services.trading_calendar_service import get_a_share_calendar


def file_hash(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start-date',type=date.fromisoformat,required=True)
    p.add_argument('--end-date',type=date.fromisoformat,required=True)
    p.add_argument('--output-root',type=Path,required=True)
    p.add_argument('--benchmark',type=Path,help='Parquet: date,code,daily_return; sh.000001 and sh.000300')
    p.add_argument('--daily',type=Path,action='append',help='Warmup and daily Parquet inputs; repeatable')
    p.add_argument('--funds',type=Path)
    p.add_argument('--industry',type=Path)
    p.add_argument('--observations-dir',type=Path,help='Timestamped source JSON.GZ files')
    p.add_argument('--prepared-dir',type=Path,help='Explicit factor_panel/context/universe files instead of rebuilding')
    p.add_argument('--signals',type=Path,help='Optional genuine saved strategy signal Parquet for event-only diagnostics')
    p.add_argument('--annual-risk-free',type=float,default=0.)
    p.add_argument('--rebalance-sessions',type=int,default=5)
    p.add_argument('--report-only',action='store_true')
    return p


def main(argv=None) -> None:
    args=parser().parse_args(argv);root=args.output_root.resolve();root.mkdir(parents=True,exist_ok=True)
    if args.report_only:
        render_report(root);return
    if not args.benchmark:raise ValueError('--benchmark is required for a study')
    if bool(args.prepared_dir)==bool(args.daily):raise ValueError('Provide either --daily inputs or --prepared-dir')
    start,end=args.start_date.isoformat(),args.end_date.isoformat()
    input_paths=[args.benchmark]
    if args.prepared_dir:
        prepared=args.prepared_dir
        panel=pd.read_parquet(prepared/'factor_panel.parquet')
        stock=pd.read_parquet(prepared/'context_stock.parquet')
        market=pd.read_parquet(prepared/'context_market.parquet')
        specs=json.loads((prepared/'context_specs.json').read_text())
        universe=json.loads((prepared/'universe.json').read_text());codes=universe['codes']
        floor=universe['warmup_start']
        input_paths += [prepared/name for name in ['factor_panel.parquet','context_stock.parquet','context_market.parquet','context_specs.json','universe.json']]
    else:
        daily=pd.concat([pd.read_parquet(path) for path in args.daily],ignore_index=True)
        if daily[['code','trade_date']].duplicated().any():raise ValueError('Daily inputs overlap')
        floor=str(daily.trade_date.min());calendar=[d.strftime('%Y-%m-%d') for d in get_a_share_calendar().sessions_in_range(floor,end)]
        if start not in calendar or calendar.index(start)<130:raise ValueError('At least 130 pre-start daily sessions are required')
        previous=calendar[calendar.index(start)-1]
        codes=sorted(daily.loc[daily.trade_date.le(previous),'code'].unique())
        daily=daily[daily.code.isin(codes)]
        funds=pd.read_parquet(args.funds) if args.funds else None
        industry=pd.read_parquet(args.industry,columns=['date','code','industry','industry_update_date']) if args.industry else None
        output_start=calendar[max(0,calendar.index(start)-40)]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',pd.errors.PerformanceWarning)
            panel=build_daily_factors(daily,sessions=calendar,output_start=output_start,flow=funds,industry=industry,
                                     progress=lambda n,total:print(f'features {n}/{total}',flush=True))
        source={}
        if args.observations_dir:
            for name in ['market_sentiment_daily','daily_market_analysis','stock_ths_hot_rank_snapshots','stock_billboard_daily','short_sentiment_snapshots']:
                path=args.observations_dir/(name+'.json.gz')
                if path.exists():
                    with gzip.open(path,'rt') as f:source[name]=json.load(f)
                    input_paths.append(path)
        stock,market,specs,audit=build_observation_factors(dates=[d for d in calendar if d>=output_start],calendar=calendar,codes=codes,sources=source)
        # An absent auxiliary source still has an explicit empty key schema.
        if stock.empty:stock=pd.DataFrame(columns=['trade_date','code'])
        panel.to_parquet(root/'factor_panel.parquet',index=False);stock.to_parquet(root/'context_stock.parquet',index=False)
        market.to_parquet(root/'context_market.parquet',index=False)
        (root/'context_specs.json').write_text(json.dumps(specs,ensure_ascii=False,indent=2))
        (root/'context_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
        universe={'codes':codes,'count':len(codes),'warmup_start':floor,'basis':'observed on or before '+previous}
        (root/'universe.json').write_text(json.dumps(universe,ensure_ascii=False,indent=2))
        input_paths+=args.daily+[p for p in [args.funds,args.industry] if p]
    calendar=[d.strftime('%Y-%m-%d') for d in get_a_share_calendar().sessions_in_range(floor,end)]
    events=None
    if args.signals:
        signals=pd.read_parquet(args.signals)
        signals=signals[signals.action.eq('buy')&signals.status.eq('filled')&signals.execution_at.notna()].drop_duplicates('signal_id',keep='last')
        signals=signals[signals.execution_at.str[:10].between(start,end)]
        events=signal_features(signals.rename(columns={'execution_at':'entry_at','factor_completed_date':'factor_date'}),{d:i for i,d in enumerate(calendar)})
        events[['code','signal_at','entry_at','factor_date','dif_pct','shrink_ratio','factor_age_sessions','execution_delay_sessions','entry_session_minute']].to_csv(root/'信号事件指标.csv',index=False,encoding='utf-8-sig')
        input_paths.append(args.signals)
    benchmark=pd.read_parquet(args.benchmark)
    if args.benchmark.resolve()!=root/'benchmark.parquet':benchmark.to_parquet(root/'benchmark.parquet',index=False)
    provenance={str(p.resolve()):file_hash(p) for p in input_paths}
    (root/'input_hashes.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2))
    research=Path(__file__).resolve().parents[1]/'research'
    implementation=[Path(__file__).resolve(),*[research/name for name in [
        'alpha.py','factor_portfolio.py','factor_features.py','factor_context.py',
        'factor_catalog.json','factor_alpha_study.py','factor_alpha_report.py']]]
    (root/'implementation_hashes.json').write_text(json.dumps(
        {str(p):file_hash(p) for p in implementation},ensure_ascii=False,indent=2))
    protocol=dict(start=start,end=end,benchmark='000001.SH',benchmark_type='price_index',
                  rf_annual_assumption=args.annual_risk_free,rf_sensitivity=[.01,.02],group_count=5,
                  rebalance_sessions=args.rebalance_sessions,execution='previous completed factor; next session open',
                  capital='fixed NAV=1; fractional research units; no subscriptions',
                  costs={'commission_each_side':.0001,'sell_stamp_duty':.0005,'slippage_each_side':.0005},
                  sample_kind='retrospective_factor_portfolio_not_production_strategy_replay',
                  inference='daily HAC lag5; BH across all successful primary-benchmark regressions',
                  daily_vintage='historical observed vendor revisions; not guaranteed original receipt',
                  auxiliary_timing='receipt or generation <= decision open; missing remains missing')
    (root/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    summary=run_study(panel=panel,stock_context=stock,market_context=market,context_specs=specs,benchmark=benchmark,
                      calendar=calendar,codes=codes,start=start,end=end,output=root,
                      rebalance_sessions=args.rebalance_sessions,annual_risk_free=args.annual_risk_free,
                      event_features=events,
                      progress=lambda message:print(message,flush=True))
    render_report(root)
    print(json.dumps({k:summary[k] for k in ['sessions','registered_fields','portfolios','full_window_portfolios']},ensure_ascii=False))


if __name__=='__main__':main()
