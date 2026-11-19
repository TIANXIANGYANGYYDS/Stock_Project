"""Run all supported stock factors and market timing controls under one protocol."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable

import numpy as np
import pandas as pd

from app.quant.research.alpha import benchmark_metrics, benjamini_hochberg
from app.quant.research.factor_features import CATALOG
from app.quant.research.factor_portfolio import PortfolioCosts, simulate_portfolio, target_schedule


def run_study(
    *, panel: pd.DataFrame, stock_context: pd.DataFrame, market_context: pd.DataFrame,
    context_specs: list[dict], benchmark: pd.DataFrame, calendar: list[str], codes: list[str],
    start: str, end: str, output: Path, rebalance_sessions: int = 5,
    annual_risk_free: float = 0., progress: Callable[[str],None] | None = None,
    event_features: pd.DataFrame | None = None,
) -> dict:
    dates=[d for d in calendar if start<=d<=end]
    if not dates or dates[0]!=start or dates[-1]!=end:raise ValueError('Start/end must be completed sessions')
    indices={d:i for i,d in enumerate(calendar)}
    previous=[calendar[indices[d]-1] for d in dates]
    if indices[start]==0:raise ValueError('Need pre-start reference session')
    if panel[['trade_date','code']].duplicated().any():raise ValueError('Duplicate factor keys')
    output.mkdir(parents=True,exist_ok=True)
    in_scope=panel[panel.trade_date.isin(set(dates+previous)) & panel.code.isin(codes)].copy()
    def matrix(frame,key,days):
        if key not in frame:return np.full((len(days),len(codes)),np.nan)
        return frame.pivot(index='trade_date',columns='code',values=key).reindex(index=days,columns=codes).to_numpy(dtype=float)
    opens=matrix(in_scope,'normalized_open',dates);closes=matrix(in_scope,'normalized_close',dates)
    prior_close=matrix(in_scope,'normalized_close',previous)
    prior_volume=matrix(in_scope,'volume',previous)
    eligible=np.isfinite(prior_close)&(prior_volume>0)
    current_volume=matrix(in_scope,'volume',dates)
    initial=prior_close[0]
    code_boards=np.array([.3 if c.startswith(('4','8','920')) else .2 if c.startswith(('300','301','688','689')) else .1 for c in codes])
    rates=np.broadcast_to(code_boards,opens.shape).copy()
    if 'name' in in_scope:
        names=in_scope.pivot(index='trade_date',columns='code',values='name').reindex(index=previous,columns=codes).fillna('')
        st=names.apply(lambda c:c.astype(str).str.contains('ST',case=False,regex=False)).to_numpy()
        rates[st&(rates==.1)]=.05
    known_marks=in_scope.pivot(index='trade_date',columns='code',values='normalized_close').reindex(index=sorted(set(dates+previous)),columns=codes).ffill()
    opening_return=opens/known_marks.reindex(previous).to_numpy(dtype=float)-1
    # Conservative limit approximation; no intraday fill or order-book claim.
    raw_close=matrix(in_scope,'close',previous)
    tolerance=np.maximum(.0001,.011/np.where(raw_close>0,raw_close,np.inf))
    quote=np.isfinite(opens)&(current_volume>0)
    can_buy=quote&np.isfinite(opening_return)&(opening_return<rates-tolerance)
    can_sell=quote&np.isfinite(opening_return)&(opening_return>-rates+tolerance)
    reference={}
    for symbol in ['sh.000001','sh.000300']:
        b=benchmark[benchmark.code.eq(symbol)].set_index('date')
        if b.index.has_duplicates or not set(dates)<=set(b.index):raise ValueError('Incomplete benchmark calendar')
        reference[symbol]=b.reindex(dates).daily_return.astype(float)
        if not np.isfinite(reference[symbol]).all():raise ValueError('Invalid benchmark returns')
    specs={r['key']:dict(r) for r in CATALOG}
    for r in context_specs:
        if r['key'] in specs:specs[r['key']].update(availability_basis=r.get('availability_basis'))
        else:specs[r['key']]=r
    stock_context_keys=set(stock_context)-{'trade_date','code'}
    macro=market_context.set_index('trade_date') if not market_context.empty else pd.DataFrame()
    results=[];curves=[];statuses=[];decisions_audit=[];monthly=[]

    def evaluate(key,label,family,group,targets,audit,kind):
        if not targets:return
        name=f'{key}::{group}'
        kwargs=dict(dates=dates,open_prices=opens,close_prices=closes,initial_marks=initial,
                    can_buy=can_buy,can_sell=can_sell,targets=targets)
        curve=simulate_portfolio(**kwargs)
        active=np.flatnonzero(curve.holding_count.to_numpy()>0)
        if not len(active):return
        # Start at the first known decision, including valid cash decisions;
        # starting at the first profitable/risky holding would select outcomes.
        first=min(targets);evaluated_curve=curve.iloc[first:]
        ret=evaluated_curve.set_index('trade_date').daily_return
        # Same dates for every metric, including a partial-history factor.
        bm=reference['sh.000001'].iloc[first:]
        metrics=benchmark_metrics(ret,bm,annual_risk_free=annual_risk_free)
        alternative=benchmark_metrics(ret,reference['sh.000300'].iloc[first:],annual_risk_free=annual_risk_free)
        doubled=simulate_portfolio(**kwargs,costs=PortfolioCosts(.0002,.001,.001))
        double_metrics=benchmark_metrics(doubled.set_index('trade_date').daily_return.iloc[first:],bm,annual_risk_free=annual_risk_free)
        usable=sum(a['usable'] for a in audit);expected=len(audit)
        full=first==0 and usable>=.8*expected
        row=dict(scenario=name,factor=key,label=label,family=family,group=group,kind=kind,
                 comparable_full_window=full,usable_rebalances=usable,scheduled_rebalances=expected,
                 requested_start=start,requested_end=end,**metrics,
                 csi300_excess_return=alternative['cumulative_excess_return'],csi300_beta=alternative['beta'],
                 csi300_alpha_daily=alternative['alpha_daily'],double_cost_return=double_metrics['total_return'],
                 double_cost_excess=double_metrics['cumulative_excess_return'],
                 total_cost_nav=float(evaluated_curve.cost.sum()),mean_cash_weight=float(evaluated_curve.cash_weight.mean()),
                 mean_holdings=float(evaluated_curve.holding_count.mean()),max_stale_mark_weight=float(evaluated_curve.stale_mark_weight.max()),
                 stale_mark_days=int(evaluated_curve.stale_mark_weight.gt(1e-10).sum()),
                 blocked_buys=int(evaluated_curve.blocked_buys.sum()),blocked_sells=int(evaluated_curve.blocked_sells.sum()))
        for assumption in [.01,.02]:
            sensitivity=benchmark_metrics(ret,bm,annual_risk_free=assumption)
            row[f'alpha_daily_rf_{int(assumption*100)}pct']=sensitivity['alpha_daily']
        results.append(row)
        curve['scenario']=name;curve['benchmark_nav']=np.cumprod(1+reference['sh.000001'].to_numpy())
        curve['relative_nav']=curve.nav/curve.benchmark_nav
        curves.append(curve)
        for month,grouped in evaluated_curve.groupby(evaluated_curve.trade_date.str[:7]):
            d=grouped.trade_date
            own=float((1+grouped.daily_return).prod()-1);bench=float((1+reference['sh.000001'].loc[d]).prod()-1)
            monthly.append(dict(scenario=name,month=month,return_value=own,benchmark_return=bench,excess=own-bench,sessions=len(d)))
        decisions_audit.extend(dict(scenario=name,trade_date=dates[a['day_index']],**a) for a in audit)

    # Constant-universe equal-weight baseline controls for broad small-cap and
    # exchange exposure; it is distinct from a capitalization-weighted index.
    all_targets={};all_audit=[]
    for i in range(0,len(dates),rebalance_sessions):
        good=eligible[i];usable=good.sum()>=20
        if usable:all_targets[i]=good.astype(float)/good.sum()
        all_audit.append(dict(day_index=i,eligible_stocks=int(good.sum()),factor_stocks=int(good.sum()),coverage=1.,selected_stocks=int(good.sum()),usable=bool(usable)))
    evaluate('equal_weight_universe','期初股票池等权基线','基线','all',all_targets,all_audit,'baseline')

    for number,(key,spec) in enumerate(specs.items(),1):
        role=spec['role'];status=dict(spec)
        status.update(portfolio_count=0,available_decision_dates=0,coverage_basis='open decision; daily stock factors from previous completed session')
        before=len(results)
        if role=='cross_section':
            frame=stock_context if key in stock_context_keys else in_scope
            values=matrix(frame,key,dates if key in stock_context_keys else previous)
            available=np.isfinite(values)&eligible
            status.update(available_decision_dates=int(available.any(axis=1).sum()),available_stock_days=int(available.sum()),
                          coverage_fraction=float(available.sum()/max(1,eligible.sum())))
            for group in range(1,6):
                targets,audit=target_schedule(values,eligible,group=group,rebalance_sessions=rebalance_sessions)
                evaluate(key,spec['label'],spec['family'],f'Q{group}',targets,audit,'stock_factor')
            status['status']='evaluated' if len(results)>before else 'insufficient_coverage_or_cross_sectional_variation'
        elif role=='market_context' and key in macro:
            series=pd.to_numeric(macro[key],errors='coerce')
            # The threshold uses only 20 earlier available calendar decisions,
            # with at least 10 observations. Never choose direction by returns.
            threshold=series.shift(1).rolling(20,min_periods=10).median()
            current=series.reindex(dates);cut=threshold.reindex(dates)
            status['available_decision_dates']=int(current.notna().sum())
            for direction in ['high','low']:
                targets={};audit=[]
                for i,day in enumerate(dates):
                    known=pd.notna(current.loc[day]) and pd.notna(cut.loc[day]) and eligible[i].sum()>=20
                    allocate=known and (current.loc[day]>cut.loc[day] if direction=='high' else current.loc[day]<cut.loc[day])
                    if known:targets[i]=eligible[i].astype(float)/eligible[i].sum() if allocate else np.zeros(len(codes))
                    audit.append(dict(day_index=i,eligible_stocks=int(eligible[i].sum()),factor_stocks=int(eligible[i].sum()) if known else 0,
                                      coverage=float(known),selected_stocks=int(eligible[i].sum()) if allocate else 0,usable=bool(known)))
                evaluate(key,spec['label'],spec['family'],direction,targets,audit,'market_timing')
            status['status']='evaluated' if len(results)>before else 'insufficient_prior_context'
        elif role in ['signal_event','execution_diagnostic','data_quality']:
            status['status']='implemented_event_only' if role=='signal_event' else 'implemented_control_not_return_signal'
            if event_features is not None and key in event_features:
                usable=event_features[key].notna()
                status['available_event_rows']=int(usable.sum())
                status['available_decision_dates']=int(event_features.loc[usable,'entry_at'].str[:10].nunique())
            elif key in macro:
                status['available_decision_dates']=int(macro[key].reindex(dates).notna().sum())
        else:status['status']='source_history_unavailable'
        status['portfolio_count']=len(results)-before;statuses.append(status)
        if progress and number%10==0:progress(f'evaluated {number}/{len(specs)} fields; {len(results)} portfolios')
    scores=pd.DataFrame(results)
    scores['alpha_bh_q']=benjamini_hochberg(scores.alpha_pvalue.tolist())
    all_curves=pd.concat(curves,ignore_index=True)
    equal=all_curves[all_curves.scenario.eq('equal_weight_universe::all')].set_index('trade_date')
    for i,row in scores.iterrows():
        dates_for=row.start_date<=equal.index
        matched=equal.loc[dates_for,'daily_return']
        scores.loc[i,'same_universe_excess']=row.total_return-float((1+matched).prod()-1)
    scores.to_csv(output/'组合收益与Alpha.csv',encoding='utf-8-sig',index=False)
    scores.to_parquet(output/'portfolio_metrics.parquet',index=False)
    all_curves.to_parquet(output/'portfolio_curves.parquet',index=False)
    all_curves.to_csv(output/'逐日净值.csv',encoding='utf-8-sig',index=False)
    pd.DataFrame(statuses).to_csv(output/'全部因子实现与覆盖.csv',encoding='utf-8-sig',index=False)
    pd.DataFrame(decisions_audit).to_parquet(output/'rebalance_audit.parquet',index=False)
    pd.DataFrame(monthly).to_csv(output/'逐月表现.csv',encoding='utf-8-sig',index=False)
    summary=dict(start=start,end=end,sessions=len(dates),universe_codes=len(codes),registered_fields=len(specs),
                 catalog_fields=len(CATALOG),portfolios=len(scores),full_window_portfolios=int(scores.comparable_full_window.sum()),
                 status_counts=pd.Series([r['status'] for r in statuses]).value_counts().to_dict(),
                 primary_benchmark_return=float(np.prod(1+reference['sh.000001'])-1),
                 equal_weight_baseline=scores.iloc[0].to_dict())
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str))
    return summary
