"""Aggregate independent strategy-1 paths; separate selection from cash effects."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.quant.research.alpha import benchmark_metrics, benjamini_hochberg
from app.quant.research.strategy1_filters import FILTERS, gate_result


def aggregate_and_report(root: Path, factor_root: Path) -> None:
    protocol=json.loads((root/'protocol.json').read_text())
    expected_hash=hashlib.sha256(json.dumps(protocol,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    dates=protocol['dates'];codes=protocol['codes'];capital=len(codes)*100000.
    keys=[f'{mode}::{key}' for mode in ['normal','double_cost'] for key,_,_ in FILTERS]
    totals={key:np.zeros((len(dates),5)) for key in keys}
    clean_totals={key:np.zeros((len(dates),5)) for key,_,_ in FILTERS}
    clean_count=0
    accounts=[];trades=[];signals=[];coverage=[];checks=0
    for code in codes:
        with gzip.open(root/'stocks'/f'{code}.json.gz','rt') as f:
            stock=json.load(f)
        assert stock['study_hash']==expected_hash, 'Mixed source/protocol checkpoints'
        complete=(not any(r['has_daily'] and not r['complete'] for r in stock['coverage'])
                  and not any(r['incomplete_held_days'] for r in stock['results'].values()))
        clean_count+=int(complete)
        coverage.extend(dict(code=code,**r) for r in stock['coverage'])
        assert set(stock['results'])==set(keys)
        for scenario,result in stock['results'].items():
            values=np.array(result['curve'],dtype=float)
            assert values.shape==(len(dates),5) and np.isfinite(values).all()
            np.testing.assert_allclose(values[:,0],values[:,1]+values[:,2],atol=.011,rtol=0)
            assert values[:,1].min()>=0
            unrealized=sum(t.get('unrealized_pnl',0) for t in result['trades'] if t['outcome']=='open')
            np.testing.assert_allclose(values[-1,0]-100000,values[-1,3]+unrealized,atol=.021,rtol=0)
            totals[scenario]+=values;checks+=1
            mode,key=scenario.split('::')
            if complete and mode=='normal':
                clean_totals[key]+=values
            accounts.append(dict(cost_mode=mode,scenario=key,code=code,final_assets=values[-1,0],
                total_return=values[-1,0]/100000-1,realized_return=values[-1,3]/100000,
                unrealized_return=unrealized/100000,buy_count=sum(e['action']=='buy' for e in result['events']),
                closed_count=sum(t['outcome']=='closed' for t in result['trades']),
                end_holding=values[-1,2]>0,incomplete_held_days=result['incomplete_held_days']))
            trades.extend(dict(cost_mode=mode,scenario=key,**t) for t in result['trades'])
            signals.extend(dict(cost_mode=mode,scenario=key,**s) for s in result['signals'])
    account=pd.DataFrame(accounts);trade=pd.DataFrame(trades);signal=pd.DataFrame(signals)
    if len(trade):
        trade['net_return']=pd.to_numeric(trade.net_return,errors='coerce')
    coverage=pd.DataFrame(coverage)
    benchmark=pd.read_parquet(factor_root/'benchmark.parquet')
    ref=benchmark[benchmark.code.eq('sh.000001')].set_index('date').daily_return.reindex(dates)
    other=benchmark[benchmark.code.eq('sh.000300')].set_index('date').daily_return.reindex(dates)
    if ref.isna().any() or other.isna().any():
        raise ValueError('Incomplete matched benchmark')
    clean_rows=[]
    if clean_count:
        clean_base=clean_totals['baseline'][-1,0]/(clean_count*100000)-1
        for key,values in clean_totals.items():
            nav=values[:,0]/(clean_count*100000)
            ret=pd.Series(nav/np.r_[1.,nav[:-1]]-1,index=dates)
            clean_metrics=benchmark_metrics(ret,ref)
            clean_rows.append(dict(scenario=key,common_stocks=clean_count,excluded_stocks=len(codes)-clean_count,
                total_return=clean_metrics['total_return'],versus_strategy_return=clean_metrics['total_return']-clean_base,
                maximum_drawdown=clean_metrics['maximum_drawdown'],mean_exposure=float((values[:,2]/values[:,0]).mean())))
    all_curves=[];metrics=[];monthly=[];folds=[];paired=[]
    labels={key:label for key,label,_ in FILTERS}
    for scenario,values in totals.items():
        mode,key=scenario.split('::')
        nav=values[:,0]/capital
        returns=pd.Series(nav/np.r_[1.,nav[:-1]]-1,index=dates)
        cash=values[:,1]/values[:,0];exposure=values[:,2]/values[:,0]
        base_values=totals[f'{mode}::baseline'];base_nav=base_values[:,0]/capital
        base_ret=pd.Series(base_nav/np.r_[1.,base_nav[:-1]]-1,index=dates)
        base_exp=base_values[:,2]/base_values[:,0]
        ratio=exposure.mean()/base_exp.mean() if base_exp.mean()>0 else 0.
        cash_match=(1+base_ret*ratio).prod()-1
        m=benchmark_metrics(returns,ref)
        incremental=benchmark_metrics(returns-base_ret,ref)
        selected=account[account.cost_mode.eq(mode)&account.scenario.eq(key)]
        tt=trade[trade.cost_mode.eq(mode)&trade.scenario.eq(key)] if len(trade) else pd.DataFrame()
        closed=tt[tt.outcome.eq('closed')] if len(tt) else pd.DataFrame()
        cr=pd.to_numeric(closed.net_return,errors='coerce') if len(closed) else pd.Series(dtype=float)
        row=dict(cost_mode=mode,scenario=key,label=labels[key],**m,
            versus_strategy_return=m['total_return']-(base_nav[-1]-1),
            incremental_alpha_daily=incremental['alpha_daily'],incremental_alpha_pvalue=incremental['alpha_pvalue'],
            incremental_alpha_ci_low=incremental['alpha_ci95_low'],incremental_alpha_ci_high=incremental['alpha_ci95_high'],
            mean_exposure=float(exposure.mean()),mean_cash=float(cash.mean()),
            exposure_matched_baseline_return=float(cash_match),beyond_cash_reduction=m['total_return']-cash_match,
            csi300_excess=benchmark_metrics(returns,other)['cumulative_excess_return'],
            buy_count=int(selected.buy_count.sum()),closed_count=int(selected.closed_count.sum()),
            end_holding_count=int(selected.end_holding.sum()),traded_accounts=int(selected.buy_count.gt(0).sum()),
            realized_return=float(selected.realized_return.mean()),unrealized_return=float(selected.unrealized_return.mean()),
            closed_win_rate=float(cr.gt(0).mean()) if len(cr) else None,
            closed_mean_return=float(cr.mean()) if len(cr) else None,
            closed_es95=float(cr.nsmallest(max(1,int(np.ceil(len(cr)*.05)))).mean()) if len(cr) else None,
            max_stale_weight=float(np.max(values[:,4]/values[:,0])),
            incomplete_held_days=int(selected.incomplete_held_days.sum()))
        metrics.append(row)
        curve=pd.DataFrame(dict(trade_date=dates,cost_mode=mode,scenario=key,nav=nav,daily_return=returns.to_numpy(),
                                cash_weight=cash,exposure=exposure,stale_weight=values[:,4]/values[:,0],
                                benchmark_nav=np.cumprod(1+ref.to_numpy()),
                                excess_nav=nav/base_nav,relative_sse_nav=nav/np.cumprod(1+ref.to_numpy())))
        all_curves.append(curve)
        for month,part in curve.groupby(curve.trade_date.str[:7]):
            day=part.trade_date;own=(1+part.daily_return).prod()-1
            base=(1+base_ret.loc[day]).prod()-1;index=(1+ref.loc[day]).prod()-1
            monthly.append(dict(cost_mode=mode,scenario=key,month=month,sessions=len(day),return_value=own,
                                baseline_return=base,versus_strategy_return=own-base,benchmark_return=index,
                                excess_return=own-index,mean_exposure=part.exposure.mean()))
        for fold,positions in enumerate(np.array_split(np.arange(len(dates)),4),1):
            day=[dates[i] for i in positions]
            own=(1+returns.loc[day]).prod()-1;base=(1+base_ret.loc[day]).prod()-1
            folds.append(dict(cost_mode=mode,scenario=key,fold=fold,start_date=day[0],end_date=day[-1],
                              return_value=own,baseline_return=base,versus_strategy_return=own-base,
                              note='continuous account; descriptive time blocks, not new OOS'))
        reference=account[account.cost_mode.eq(mode)&account.scenario.eq('baseline')].set_index('code')
        paired.extend(dict(cost_mode=mode,scenario=key,code=r.code,
                           return_delta=r.total_return-reference.loc[r.code,'total_return'],
                           realized_delta=r.realized_return-reference.loc[r.code,'realized_return'],
                           unrealized_delta=r.unrealized_return-reference.loc[r.code,'unrealized_return']) for r in selected.itertuples())
    metrics=pd.DataFrame(metrics);curves=pd.concat(all_curves,ignore_index=True)
    # Seven primary incremental tests; previous selection history is disclosed.
    primary=metrics.cost_mode.eq('normal')&metrics.scenario.ne('baseline')
    metrics.loc[primary,'incremental_alpha_bh_q']=benjamini_hochberg(metrics.loc[primary,'incremental_alpha_pvalue'].tolist())
    normal=metrics.cost_mode.eq('normal')
    metrics.loc[normal,'alpha_bh_q']=benjamini_hochberg(metrics.loc[normal,'alpha_pvalue'].tolist())
    pairs=pd.DataFrame(paired);cohorts=[];retention=[];assignments=[]
    ranks=pd.read_parquet(root/'rank_inputs.parquet').set_index(['code','trade_date'])
    prior_dates=ranks.index.get_level_values('trade_date')
    previous={dates[i]: dates[i-1] if i else prior_dates[prior_dates<dates[0]].max() for i in range(len(dates))}
    if len(trade):
        for mode in ['normal','double_cost']:
            base=trade[trade.cost_mode.eq(mode)&trade.scenario.eq('baseline')].copy()
            winners=base[base.outcome.eq('closed')].nlargest(max(1,int(np.ceil(base.outcome.eq('closed').sum()*.05))),'net_return')
            for key,label,rules in FILTERS[1:]:
                mask=[];reasons=[]
                for t in base.itertuples():
                    prior=previous[t.entry_signal_at[:10]]
                    values=ranks.loc[(t.code,prior)].to_dict() if (t.code,prior) in ranks.index else {}
                    keep,reason=gate_result(rules,values);mask.append(keep);reasons.append(reason)
                    assignments.append(dict(cost_mode=mode,scenario=key,code=t.code,entry_signal_at=t.entry_signal_at,
                        factor_date=prior,retained=keep,reason=reason,outcome=t.outcome,asof_return=t.asof_return,
                        net_return=t.net_return))
                mask=pd.Series(mask,index=base.index,dtype=bool)
                candidate=trade[trade.cost_mode.eq(mode)&trade.scenario.eq(key)]
                base_ids=set(zip(base.code,base.entry_signal_at));candidate_ids=set(zip(candidate.code,candidate.entry_signal_at))
                for keep in [True,False]:
                    group=base[mask.eq(keep)];closed=group[group.outcome.eq('closed')]
                    cohorts.append(dict(cost_mode=mode,scenario=key,group='retained' if keep else 'rejected',
                        count=len(group),closed_count=len(closed),open_count=int(group.outcome.eq('open').sum()),
                        mean_asof_return=group.asof_return.mean(),mean_closed_return=closed.net_return.mean(),
                        closed_win_rate=closed.net_return.gt(0).mean() if len(closed) else None,
                        missing_factor_count=sum(reason.startswith('missing_factor') for reason,k in zip(reasons,mask) if k==keep)))
                retention.append(dict(cost_mode=mode,scenario=key,baseline_entries=len(base),
                    signal_retention_rate=float(mask.mean()),baseline_winners=len(winners),
                    winner_retention_rate=float(mask.reindex(winners.index).mean()) if len(winners) else None,
                    candidate_entries=len(candidate),new_entry_count=len(candidate_ids-base_ids),
                    shared_entry_count=len(candidate_ids&base_ids)))
    retention=pd.DataFrame(retention)
    metrics=metrics.merge(retention,on=['cost_mode','scenario'],how='left') if len(retention) else metrics
    frames={'策略对照结果':metrics,'连续每日净值':curves,'逐月对照':pd.DataFrame(monthly),
            '四段时间对照':pd.DataFrame(folds),'同批买点保留剔除':pd.DataFrame(cohorts),
            '买点及赢家保留':retention,'逐股账户':account,'逐股配对差值':pairs,
            '真实路径交易':trade,'信号审计':signal,'行情覆盖审计':coverage,'基线交易分组':pd.DataFrame(assignments)}
    frames['共同完整行情子样本敏感性']=pd.DataFrame(clean_rows)
    for name,frame in frames.items():
        frame.to_parquet(root/(name+'.parquet'),index=False)
        if name not in ['信号审计','行情覆盖审计']:
            frame.to_csv(root/(name+'.csv'),index=False,encoding='utf-8-sig')
    with pd.ExcelWriter(root/'策略1过滤条件验证.xlsx',engine='openpyxl') as writer:
        for name in ['策略对照结果','连续每日净值','逐月对照','四段时间对照','同批买点保留剔除','买点及赢家保留','共同完整行情子样本敏感性','逐股账户','逐股配对差值','真实路径交易']:
            frames[name].to_excel(writer,sheet_name=name,index=False)
            sheet=writer.sheets[name];sheet.freeze_panes='D2';sheet.auto_filter.ref=sheet.dimensions
    quality=dict(account_paths_verified=checks,stock_count=len(codes),sessions=len(dates),
        coverage_rows=len(coverage),complete_stock_days=int(coverage.complete.sum()) if len(coverage) else 0,
        incomplete_with_daily=int((~coverage.complete & coverage.has_daily).sum()) if len(coverage) else 0,
        source_counts=coverage.source.value_counts().to_dict() if len(coverage) else {},
        reference_counts=coverage.reference_method.value_counts().to_dict() if len(coverage) else {},
        historical_basis_verified_days=int(coverage.reference_method.eq('prior_day_affine_basis_verified').sum()) if len(coverage) else 0,
        switches_above_half_pct=int(coverage.source_switch_gap.abs().gt(.005).sum()) if len(coverage) else 0,
        largest_source_switch_gap=float(coverage.source_switch_gap.abs().max()) if len(coverage) and coverage.source_switch_gap.notna().any() else None,
        passed_incremental_alpha_count=int((metrics.loc[primary,'incremental_alpha_daily'].gt(0)&metrics.loc[primary,'incremental_alpha_bh_q'].lt(.05)).sum()))
    (root/'validation.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
    render(root,metrics,curves,frames,quality,protocol)
    print(json.dumps(quality,ensure_ascii=False),flush=True)


def render(root,metrics,curves,frames,quality,protocol):
    labels={key:label for key,label,_ in FILTERS}
    normal=metrics[metrics.cost_mode.eq('normal')].copy()
    double=metrics[metrics.cost_mode.eq('double_cost')].set_index('scenario')
    normal['double_cost_return']=normal.scenario.map(double.total_return)
    display=normal[['label','total_return','versus_strategy_return','maximum_drawdown','mean_exposure',
        'buy_count','closed_win_rate','closed_es95','alpha_daily','incremental_alpha_daily',
        'incremental_alpha_bh_q','beyond_cash_reduction','double_cost_return','max_stale_weight']].copy()
    for k in ['total_return','versus_strategy_return','maximum_drawdown','mean_exposure','closed_win_rate','closed_es95','beyond_cash_reduction','double_cost_return','max_stale_weight']:
        display[k]*=100
    for k in ['alpha_daily','incremental_alpha_daily']:
        display[k]*=10000
    display=display.rename(columns={'label':'规则','total_return':'净收益%','versus_strategy_return':'比原策略多赚_百分点',
        'maximum_drawdown':'最大回撤%','mean_exposure':'平均股票仓位%','buy_count':'买入笔数','closed_win_rate':'闭合胜率%',
        'closed_es95':'闭合最差5%均值%','alpha_daily':'每日Alpha_基点','incremental_alpha_daily':'增量Alpha_基点',
        'incremental_alpha_bh_q':'增量检验q','beyond_cash_reduction':'超过降仓位对照_百分点',
        'double_cost_return':'双倍成本收益%','max_stale_weight':'最大旧价估值%'})
    def table(frame):
        return '<div class="scroll">'+frame.to_html(index=False,float_format=lambda x:f'{x:.4f}',na_rep='—')+'</div>'
    base=normal[normal.scenario.eq('baseline')].iloc[0]
    candidates=normal[normal.scenario.ne('baseline')]
    interpretation=''
    if len(candidates) and candidates.versus_strategy_return.lt(0).all():
        interpretation='<p class="notice"><strong>本轮7项买入过滤均降低回撤，也均降低累计收益。</strong>它们更接近减少风险敞口的条件，尚未证明能增加原策略收益；不能因上一轮低波动股票组合表现好，就直接加到MACD策略上。</p>'
    summary=f'''<h1>策略1：低波动、低换手的增量验证</h1>
<p>{protocol['start']} — {protocol['end']} · {quality['sessions']}个交易日 · {quality['stock_count']:,}个固定独立账户</p>
<p>保留原策略MACD20/100/30、三根确认、ADX14、E2退出、T+1、下一根三分钟开盘及100股整手规则。每股期初10万元，全股票池含无交易现金作为分母，不跨股票调钱。7项过滤条件各自重放完整账户，正常及双倍成本共16条组合。</p>
<p class="notice">原策略重放收益 <b>{base.total_return:.2%}</b>，同期上证 <b>{base.benchmark_return:.2%}</b>。增量Alpha通过本轮7候选多重检验的正值数量为 <b>{quality['passed_incremental_alpha_count']}</b>。本窗口已用于此前因子选择，结果属于开发研究，不是独立样本外证明。</p>
<p>日线因子仅用t−1；排名在期初冻结股票池计算。20%与40%是本轮事先登记的邻近阈值。过滤只在ADX已通过的原买入信号处发生，已提交的跨日订单继续执行。</p>
<p><a href="策略1过滤条件验证.xlsx">Excel全部明细</a> · <a href="策略对照结果.csv">统计CSV</a> · <a href="protocol.json">冻结规则和源码哈希</a> · <a href="validation.json">核验与覆盖</a></p>'''
    css='''body{font-family:system-ui,"Noto Sans CJK SC",sans-serif;background:#f3f6f8;color:#173042;margin:0}main{max-width:1450px;margin:auto;padding:28px}p,li{line-height:1.8}h1{font-size:30px}h2{margin-top:30px}.notice{background:#fff1d5;padding:16px;border-left:4px solid #a97919}table{border-collapse:collapse;font-size:13px;background:white}th,td{padding:10px;border:1px solid #d4dfe6;min-width:100px}th{background:#164c65;color:white;position:sticky;top:0}.scroll{overflow:auto;max-height:580px}select{font-size:16px;padding:10px;margin:8px}#chart{width:100%;background:white;border:1px solid #ccd8e0}a{color:#086991}'''
    html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>策略1过滤条件验证</title><style>'+css+'</style><main>'+summary+interpretation
    html+='<h2>完整路径结果</h2>'+table(display)
    html+='<p>累计增量是候选净收益减原策略；增量Alpha是每日收益差对上证收益的回归截距。无风险假设年化0%，Alpha为每日基点，5阶HAC区间，q对本轮7个主检验校正，不能抹去此前大范围筛选的影响。</p>'
    html+='<h2>连续账户净值</h2><select id="mode"><option value="nav">组合净值</option><option value="excess_nav">相对原策略净值</option><option value="relative_sse_nav">相对上证净值</option></select><select id="cost"><option value="normal">正常成本</option><option value="double_cost">双倍成本</option></select><svg id="chart" viewBox="0 0 1200 450"></svg><div id="legend"></div>'
    html+='<h2>区分选股改善与降低仓位</h2><p>过滤买点会增加现金。“超过降仓位对照”先把原策略每日收益按候选/原策略的全期平均仓位比例缩放，再与候选比较。这是事后归因诊断，比例用了全期信息，不是一条可提前交易的策略。</p>'
    clean_path=root/'共同完整行情子样本敏感性.csv'
    if clean_path.exists():
        clean=pd.read_csv(clean_path)
        html+='<h2>行情缺口敏感性</h2><p>额外使用所有场景共同的完整行情股票子样本，检查结论是否由缺口推动。剔除任一有日线却缺完整分钟/前收的股票，以及任一场景曾持仓遇到缺口的股票。该选择用了事后数据质量，可能有存续偏差，只是敏感性检查，不替代全股票池主结果。下表收益为小数。</p>'+table(clean)
    html+='<h2>逐月及连续四段</h2><p>沿用上一段账户，不重置本金、不强制清仓。这些分段均已观察，不是新的留出集。下表收益为小数比例。</p>'+table(frames['逐月对照'].query("cost_mode == 'normal'"))+table(frames['四段时间对照'].query("cost_mode == 'normal'"))
    cohort=frames['同批买点保留剔除'];retention=frames['买点及赢家保留']
    html+='<h2>同批买点质量与大赢家保留</h2><p>同批原买点分组只作诊断；完整路径收益来自独立重跑。新增买点来自过滤后账户状态变化，无法通过删除原交易得到。未平仓按市值单列；闭合胜率及最差5%只用已卖出交易。大赢家指原策略闭合收益最高5%。</p>'+table(cohort.query("cost_mode == 'normal'") if len(cohort) else cohort)+table(retention.query("cost_mode == 'normal'") if len(retention) else retention)
    html+=f'''<h2>数据与解释边界</h2><ul><li>本轮从7月1日空仓启动策略2.0。正式账本8月20日才开始，不能把本轮余额冒充正式账本。</li>
<li>8月20日前的历史三分钟按前一交易日确定的平移/缩放转换到已保存日线价格口径，并核验当日完整OHLC；后续使用实际一分钟聚合或通过重叠核验的历史源。来源切换前收差超过0.5%的记录{quality['switches_above_half_pct']}条。转换后的历史价格不是当时首次采集版本，仍不能当作实盘撮合证明。</li>
<li>从每股最早可能通过原MACD/ADX买点起审计。有日线但三分钟/前收参考不完整的股票日{quality['incomplete_with_daily']}条。缺失不填造；持仓旧价估值比例已披露。这不是无缺口的实盘成交审计。</li>
<li>未模拟真实订单簿排队、容量、额外冲击；期末持仓不强制卖出。无生产配置修改、无数据库写入。</li></ul>
<p>方法参考：<a href="https://www.nber.org/papers/w21329">NBER：多信号回测的选择偏差</a>；<a href="https://rpc.cfainstitute.org/-/media/documents/code/gips/measures-risk-adjusted-return.pdf">CFA Institute：Jensen Alpha</a>。</p></main>'''
    data={}
    for (mode,key),frame in curves.groupby(['cost_mode','scenario']):
        data.setdefault(mode,{})[key]={'label':labels[key],'rows':json.loads(frame.to_json(orient='records'))}
    html+='<script>const DATA='+json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')+''';
const colors=['#222','#1b739a','#61a4c0','#b0563b','#d9935f','#4d8852','#91b477','#8056a0'];
function draw(){const mode=document.getElementById('mode').value,cost=document.getElementById('cost').value,svg=document.getElementById('chart');svg.replaceChildren();let series=Object.values(DATA[cost]).map(s=>({label:s.label,rows:s.rows.map(r=>({date:r.trade_date,value:r[mode]}))}));if(mode==='nav')series.push({label:'上证指数',rows:Object.values(DATA[cost])[0].rows.map(r=>({date:r.trade_date,value:r.benchmark_nav}))});else series.push({label:'基准=1',rows:series[0].rows.map(r=>({date:r.date,value:1}))});const dates=series[0].rows.map(r=>r.date),values=series.flatMap(s=>s.rows.map(r=>r.value));let lo=Math.min(1,...values),hi=Math.max(1,...values),pad=Math.max(.002,(hi-lo)*.08);lo-=pad;hi+=pad;const X=d=>80+dates.indexOf(d)/Math.max(1,dates.length-1)*1080,Y=v=>390-(v-lo)/(hi-lo)*340;function node(tag,attrs,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;svg.appendChild(e);}for(let i=0;i<6;i++){const v=lo+(hi-lo)*i/5;node('line',{x1:80,x2:1160,y1:Y(v),y2:Y(v),stroke:'#dce5eb'});node('text',{x:72,y:Y(v)+5,'text-anchor':'end','font-size':14},v.toFixed(3));}for(let i=0;i<5;i++){const d=dates[Math.round(i*(dates.length-1)/4)];node('text',{x:X(d),y:420,'text-anchor':'middle','font-size':14},d.slice(5));}series.forEach((s,i)=>node('polyline',{points:s.rows.map(r=>X(r.date)+','+Y(r.value)).join(' '),fill:'none',stroke:colors[i%8],'stroke-width':2,'stroke-dasharray':i===series.length-1?'7 4':'none'}));const legend=document.getElementById('legend');legend.replaceChildren();series.forEach((s,i)=>{const el=document.createElement('span');el.textContent='━ '+s.label+'　';el.style.color=colors[i%8];legend.appendChild(el);});}
document.getElementById('mode').addEventListener('change',draw);document.getElementById('cost').addEventListener('change',draw);draw();</script></html>'''
    (root/'策略1过滤条件验证.html').write_text(html,encoding='utf-8')
