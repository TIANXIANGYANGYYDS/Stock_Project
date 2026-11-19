"""Audit independent cash/share ledgers and render the shared-account study."""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import gzip
import hashlib
from html import escape
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.quant.research.shared_portfolio import POLICIES


ROOT=Path('.local/analysis/strategy1_shared_200k_5stocks_20260701_20261106')
INPUT=Path('.local/analysis/strategy1_filters_20260701_20261106_v2/inputs')
LABELS=dict(POLICIES)


def read(path):
    with gzip.open(path,'rt',encoding='utf-8') as f:
        return json.load(f)


@lru_cache(maxsize=256)
def market(code):
    data=read(INPUT/(code+'.json.gz'))
    return data, {b['end_at']:b for rows in data['bars'].values() for b in rows}


@lru_cache(maxsize=256)
def volumes(code):
    return read(ROOT/'volumes'/(code+'.json.gz'))


def audit_run(folder):
    from app.quant.research import shared_portfolio
    from app.quant.cli import replay_shared_portfolio
    implementation=json.loads((folder/'implementation.json').read_text())
    assert implementation['engine']==hashlib.sha256(Path(shared_portfolio.__file__).read_bytes()).hexdigest()
    assert implementation['runner']==hashlib.sha256(Path(replay_shared_portfolio.__file__).read_bytes()).hexdigest()
    fills,orders,curve=read(folder/'fills.json.gz'),read(folder/'orders.json.gz'),read(folder/'curve.json.gz')
    decisions=read(folder/'decisions.json.gz')
    ending=json.loads((folder/'ending_state.json').read_text())
    cash=200000.; shares=defaultdict(int); entry_dates={}; checks=0
    source_count=Counter(); participation=defaultdict(int)
    for f in fills:
        code=f['code'];b=market(code)[1][f['execution_end']]
        assert f['execution_start']>=f['signal_at'] and f['execution_end']>f['signal_at']
        assert b['low']-1e-7<=f['price']<=b['high']+1e-7
        assert f['price']*100==round(f['price']*100) or abs(f['price']*100-round(f['price']*100))<1e-7
        assert abs(f['notional']-f['price']*f['shares'])<.011
        assert f['volume']==volumes(code)[f['execution_end']] and f['volume']>0
        participation[(code,f['execution_end'])]+=f['shares']
        assert participation[(code,f['execution_end'])]<=f['volume']*ending['rules']['participation']+1e-7
        if f['action']=='buy':
            assert not shares[code]
            shares[code]+=f['shares'];entry_dates[code]=f['execution_end'][:10]
            cash-=f['notional']+f['fees']
        else:
            assert shares[code]>=f['shares'] and entry_dates[code]<f['execution_start'][:10]
            shares[code]-=f['shares'];cash+=f['notional']-f['fees']
        assert cash>=-.001 and sum(q>0 for q in shares.values())<=5
        assert abs(cash-f['cash_after'])<.02
        source_count[f['source']]+=1;checks+=1
    assert abs(cash-ending['cash'])<.02
    assert {c:q for c,q in shares.items() if q}=={c:p['shares'] for c,p in ending['positions'].items()}
    for o in orders:
        if o['action']=='buy':
            assert o['factor_date']<o['signal_at'][:10]
            assert o['budget']<=o['decision_nav']*.2+.011
    for d in decisions:
        assert d['factor_date']<d['at'][:10]
    for row in curve:
        assert abs(row['assets']-row['cash']-row['market_value'])<.02
        assert abs(row['assets']-200000-row['realized_pnl']-row['unrealized_pnl'])<.02
        assert row['cash']>=0 and row['holding_count']<=5
    if folder.name.startswith('preopen_'):
        watch=read(folder/'watchlists.json.gz')
        selected={(r['at'][:10],r['code']) for r in watch}
        assert all((o['signal_at'][:10],o['code']) in selected for o in orders if o['action']=='buy')
        assert all(r['factor_date']<r['at'][:10] and r['rank']<=5 for r in watch)
        if 'no_st' in folder.name:
            assert all('ST' not in r['name'].upper() and '退' not in r['name'] for r in watch)
    # Gaps are visible, not silently converted to filled bars or cash exits.
    quality=json.loads((folder/'quality.json').read_text())
    classified=[]
    for q in quality:
        data,_=market(q['code'])
        source=next((a for a in data['audit'] if a['trade_date']==q['day']),{})
        kind=('verified_suspension' if q['code']=='002731' and '2026-09-01'<=q['day']<'2026-10-23'
              else 'price_basis_rejected' if source.get('reference_method')=='current_day_basis_disagrees'
              else 'unresolved_missing_bars')
        classified.append(dict(run=folder.name,**q,classification=kind,
                               reference_method=source.get('reference_method'),has_daily=source.get('has_daily')))
    return dict(run=folder.name,passed=True,fills_checked=checks,days_checked=len(curve),
                decisions_checked=len(decisions),source_counts=dict(source_count)),classified


def table(frame, columns, formats=None):
    formats=formats or {}
    head=''.join(f'<th>{escape(label)}</th>' for key,label in columns)
    body=[]
    for row in frame.to_dict('records'):
        cells=[]
        for key,label in columns:
            value=row.get(key)
            if value is None or isinstance(value,float) and not np.isfinite(value):
                rendered='—'
            else:
                rendered=formats[key](value) if key in formats else str(value)
            cells.append('<td>'+escape(rendered)+'</td>')
        body.append('<tr>'+''.join(cells)+'</tr>')
    return '<div class="scroll"><table><thead><tr>'+head+'</tr></thead><tbody>'+''.join(body)+'</tbody></table></div>'


def main():
    summaries=[];data={};audits=[];quality=[];monthly=[]
    all_curves=[];all_fills=[];all_trades=[];all_holdings=[]
    for folder in sorted((ROOT/'runs').iterdir()):
        if not (folder/'summary.json').exists():
            continue
        summary=json.loads((folder/'summary.json').read_text());summary['label']=LABELS[summary['policy']]
        summary['mechanism']='盘前候选5只' if folder.name.startswith('preopen_') else '同刻信号排序'
        summary['risk_gate']='剔除当时ST/退市名称' if 'no_st' in folder.name else '原股票池'
        summary['stress']='stress' in folder.name
        audit,issues=audit_run(folder);audits.append(audit);quality.extend(issues)
        rows={k:read(folder/(k+'.json.gz')) for k in ('curve','fills','trades','holdings','orders','attempts')}
        ending=json.loads((folder/'ending_state.json').read_text())
        summary['start_date']=rows['curve'][0]['trade_date']
        summary['data_issue_days']=len([q for q in issues if q['classification']!='verified_suspension'])
        summary['suspension_held_days']=len([q for q in issues if q['classification']=='verified_suspension'])
        counts=Counter(d['decision'] for d in read(folder/'decisions.json.gz'))
        summary['selected_orders']=counts['selected']
        summary['slot_rejections']=counts['five_slots_occupied']
        summary['preopen_exclusions']=counts['outside_preopen_top5']
        summary['missing_factor_rejections']=counts['missing_ranking_factor']
        summary['rejected_buys']=sum(o['action']=='buy' and o['status'] not in ('filled','partial_filled_cancelled','pending') for o in rows['orders'])
        summaries.append(summary)
        holdings=[dict(p,market_value=round(p['shares']*p['mark'],2),
                       unrealized_pnl=round(p['shares']*p['mark']-p['remaining_cost'],2)) for p in ending['positions'].values()]
        data[folder.name]=dict(summary=summary,**rows,ending_holdings=holdings,cash=ending['cash'],quality=issues)
        for key,out in [('curve',all_curves),('fills',all_fills),('trades',all_trades),('holdings',all_holdings)]:
            out.extend(dict(run=folder.name,**r) for r in rows[key])
        previous=200000.
        for month,group in pd.DataFrame(rows['curve']).groupby(pd.DataFrame(rows['curve']).trade_date.str[:7],sort=True):
            last=float(group.iloc[-1].assets)
            monthly.append(dict(run=folder.name,month=month,start_assets=previous,end_assets=last,pnl=last-previous,return_pct=(last/previous-1)*100))
            previous=last
    frame=pd.DataFrame(summaries)
    frame.to_csv(ROOT/'账户方案对比.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(monthly).to_csv(ROOT/'分月账户收益.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(quality).to_csv(ROOT/'行情问题与停牌核查.csv',index=False,encoding='utf-8-sig')
    audit=dict(all_passed=all(a['passed'] for a in audits),runs=len(audits),
               fills_checked=sum(a['fills_checked'] for a in audits),
               account_days_checked=sum(a['days_checked'] for a in audits),runs_detail=audits)
    (ROOT/'validation.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
    with pd.ExcelWriter(ROOT/'20万五只持仓模拟成交.xlsx',engine='openpyxl') as writer:
        for name,values in [('方案比较',summaries),('每日资产现金',all_curves),('逐笔模拟成交',all_fills),
                            ('闭合交易盈亏',all_trades),('每日持仓',all_holdings),('分月收益',monthly),('行情核查',quality)]:
            pd.DataFrame(values).to_excel(writer,sheet_name=name,index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes='C2';sheet.auto_filter.ref=sheet.dimensions
            for col in sheet.columns:
                sheet.column_dimensions[col[0].column_letter].width=min(30,max(14,len(str(col[0].value))+2))
    # Standard plotting creates independently exportable figures, embedded locally.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    benchmark=pd.read_parquet('.local/analysis/factor_alpha_20260701_20261106/benchmark.parquet')
    b=benchmark[benchmark.code.eq('sh.000001')].set_index('date')
    dates=data['normal__fifo']['curve'];days=[r['trade_date'] for r in dates]
    ref=200000*(1+b.loc[days,'daily_return']).cumprod()
    for prefix,out in [('normal__','simultaneous_nav.svg'),('preopen_normal__','preopen_nav.svg')]:
        fig,ax=plt.subplots(figsize=(11,4.4))
        for policy,_ in POLICIES:
            name=prefix+policy
            if name in data:
                ax.plot(range(len(days)),[r['assets'] for r in data[name]['curve']],label=policy,linewidth=1.6)
        ax.plot(range(len(days)),ref.values,label='SSE index, 200k equivalent',color='#8993a3',ls='--')
        ticks=list(range(0,len(days),15))+[len(days)-1]
        ax.set_xticks(ticks,[days[i][5:] for i in ticks]);ax.set_ylabel('Account equity / CNY')
        ax.grid(alpha=.2);ax.legend(ncol=3,fontsize=8);fig.tight_layout();fig.savefig(ROOT/out);plt.close(fig)
    normal=frame[frame.run.str.startswith('normal__')].copy()
    pool=frame[frame.run.str.startswith('preopen_normal__')].copy()
    primary=pd.concat([normal,pool]).sort_values(['mechanism','return_pct'],ascending=[True,False])
    fmt={'final_assets':lambda v:f'{v:,.2f}','pnl':lambda v:f'{v:+,.2f}',
         'return_pct':lambda v:f'{v:+.2f}%','max_drawdown_pct':lambda v:f'{-v:.2f}%',
         'mean_exposure':lambda v:f'{v:.1%}','fees':lambda v:f'{v:,.2f}'}
    columns=[('mechanism','选股方式'),('label','排序'),('final_assets','期末资产/元'),('pnl','净盈亏/元'),
        ('return_pct','收益率'),('max_drawdown_pct','最大回撤'),('mean_exposure','平均股票仓位'),
        ('buys','买入笔数'),('closed_trades','闭合交易'),('data_issue_days','持仓行情问题日')]
    comparison=table(primary,columns,fmt)
    selected=frame[frame.policy.eq('low_vol')].sort_values(['risk_gate','mechanism','start_date','stress'])
    sensitivity=table(selected,[('mechanism','选股方式'),('risk_gate','风险股处理'),('start_date','从现金开始'),
        ('stress','压力撮合'),('pnl','净盈亏/元'),('return_pct','收益率'),('max_drawdown_pct','最大回撤'),
        ('mean_exposure','平均股票仓位'),('data_issue_days','持仓行情问题日')],fmt)
    best=data['preopen_normal__low_vol']['summary'];recent=data['preopen_normal_20260820__low_vol']['summary']
    stress=data['preopen_stress__low_vol']['summary']
    no_st=data.get('preopen_no_st_normal__low_vol',{}).get('summary')
    no_st_text=(f"剔除前一日名称中的ST/退市标记后，全区间净盈亏为{no_st['pnl']:+,.2f}元、最大回撤{-no_st['max_drawdown_pct']:.2f}%。"
                if no_st else '')
    display_data={k:{field:v[field] for field in ('summary','fills','ending_holdings','cash','quality')}
                  for k,v in data.items()}
    payload=json.dumps(display_data,ensure_ascii=False,allow_nan=False).replace('</','<\\/')
    options=''.join(f'<option value="{escape(k)}">{escape(k+" ｜ "+v["summary"]["label"])}</option>' for k,v in data.items())
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>策略1｜20万元、最多5只股票：共享账户模拟成交</title>
<style>body{margin:0;background:#f3f5f8;color:#1e293b;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1260px;margin:auto;padding:32px 24px 80px}h1{font-size:32px;line-height:1.4}h2{margin-top:38px;font-size:23px}p{max-width:1100px}section,.card{background:white;border:1px solid #dbe2ea;border-radius:12px;padding:22px;margin:18px 0}.lead{border-left:5px solid #166b5a}.note{background:#fff7e5;border-left:5px solid #c48a13}.cards{display:flex;flex-wrap:wrap;gap:14px}.cards>div{flex:1;min-width:180px}.big{font-size:27px;font-weight:700;color:#176b58}.muted{color:#64748b;font-size:14px}table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:14px}th,td{padding:9px 12px;text-align:right;border-bottom:1px solid #e2e8f0}th{background:#edf2f7;position:sticky;top:0}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:620px}select,input{padding:10px;border:1px solid #aab8c8;border-radius:6px;max-width:100%}a{color:#12679c}img{width:100%;height:auto}code{background:#edf2f7;padding:2px 5px}li{margin:6px 0}.tag{font-size:13px;color:#475569}button{padding:7px 15px;cursor:pointer}</style>
<main><div class="tag">2026-07-01 — 2026-11-06 · 87个交易日 · 历史行情驱动的模拟订单 · 无券商实盘委托</div>
<h1>把策略1放进一个20万元账户，<br>最多同时持有5只股票</h1>
<section class="lead"><p><b>本次更有价值的变化，是先决定“哪些股票有资格占用名额”。</b>只在同一时刻对已触发的买点排序，容易让早到的信号占满账户。盘前从原策略观察池选出5只低波动候选，再等待原买点，结果较好；其余时间允许持有现金。</p>
<p>原MACD20/100/30、ADX14买入门控和E2卖出规则保留；不因新股票分数更高而强制换仓。不从已结束交易中挑选赢家，也不把独立股票收益相加。</p>
__CARDS__</section>
<section class="note"><b>结果的证据范围</b><p>下面是OHLCV柱内撮合，不是券商交割单：价格采用下一根3分钟线开盘加不利滑点，成交量限制为该柱总量的5%；无法还原盘口队列。2026-08-20之后使用实际采集1分钟线合成；更早使用经校验的历史3分钟线。完整区间存在价格基准校验未通过的持仓日，未伪造成交，详见逐项核查。分红、送转及红利税尚未独立记账，完整区间金额属于这一价格口径下的模拟估计。所有方案均在已研究过的历史区间中比较，不是独立样本外检验。</p></section>
<h2>怎样选出这5只候选</h2><section><ol>
<li>每天开盘前，只使用截至前一完整交易日的数据：原策略日线MACD允许买入观察，且ADX14≥20并高于三个市场交易日前。</li>
<li>剔除已持有、已有待成交订单的股票。低波动方案按过去20个市场交易日涨跌幅的样本标准差由小到大，保留前5只；缺少指标不填0，分数相同按代码排序。</li>
<li>这5只只是当天候选。仍须出现原策略连续3根3分钟柱的买入确认，才能提交下一柱模拟订单。没有触发就保留现金。</li>
<li>每个新仓预算≤当时净资产×20%，预算含买入费用，同时不能超过可用现金；持仓与待买订单合计≤5只。盈利复投，不补仓，不强制凑满。</li>
<li>卖出仍由原MACD/E2决定，T+1可卖；跌停或流动性不足时保留卖出意图并重试。买入订单只尝试下一柱，成交不足部分撤销。</li>
</ol><p>排位发生在开盘前，不读取“今天最终哪几只会触发买点”。单纯同刻排序与盘前候选池分别完整回放；后者是在发现早到信号占满账户之后追加的机制对照，选择历史已明确披露。</p></section>
<h2>完整区间：同一账户规则下的12个主要对照</h2>__COMPARISON__
<p class="muted">“期末资产”包含现金与未卖出持仓市值；“最大回撤”包含初始20万元。平均股票仓位帮助区分股票选择与现金比例的影响。行情问题日按股票×交易日统计，已核实停牌单独归类。</p>
<section><h3>同刻信号排序</h3><img src="simultaneous_nav.svg" alt="同刻信号排序的账户资产变化"><h3>盘前选5只候选</h3><img src="preopen_nav.svg" alt="盘前候选池的账户资产变化"><p class="muted">图例：fifo 原信号/代码顺序；low_vol 低波动；momentum 相对强势；adx ADX增量；flow 主力强度；balanced 三项等权。灰色虚线仅展示同期上证价格指数对应的20万元价值，不能把简单差额称为已证实的风险调整α。</p></section>
<h2>低波动方案：换开始日期、加大成交阻力、剔除风险名称</h2>__SENSITIVITY__
<p>压力撮合把佣金率从万一改为万二（最低5元不变）、滑点从万五改为千一，成交量占比从5%降到1%；印花税和过户费不变。它会改变成交数量和后续路径，收益不保证逐项下降。8月20日和9月1日测试分别从20万元现金重新开始，未继承7月的盈利或持仓。</p>
<section class="lead"><p>盘前低波动方案完整区间净盈亏<b>__BEST__元</b>；压力撮合后为<b>__STRESS__元</b>。8月20日从现金重新开始为<b>__RECENT__元</b>。这些结果支持将“盘前候选＋低波动排序”列入后续连续模拟候选，但收益对成交容量、开始日期和现金占比敏感。</p><p>__NOST__</p><p>集中持仓没有消除原卖出规则的风险：盈利交易结束后，亏损股票仍可能长期占用名额。因此不能仅用闭合交易胜率选择方案。正式策略参数尚未切换。</p></section>
<h2>逐笔模拟成交与期末持仓</h2><section><label>选择账户 <select id="run">__OPTIONS__</select></label><p id="stats"></p><div id="holdings"></div>
<p><label>筛选股票代码/名称 <input id="filter" placeholder="例如 000404"></label> <span id="links"></span></p><div id="fills"></div><p id="fillcount" class="muted"></p></section>
<h2>费用、撮合与核查</h2><section><ul>
<li>双边佣金万一，单笔最低5元；双边过户费十万分之一；卖出印花税万五。佣金按研究假设，非个人券商账单。</li>
<li>价格按买入向上、卖出向下取整到0.01元；滑点后超出该柱实际高低价则不成交，禁止把滑点价格裁剪成有利成交价。开盘已达对应涨跌停方向时不成交。</li>
<li>普通沪深股票按100股整手；科创板买入至少200股、递增1股；北交所至少100股、递增1股。研究股票池假设账户已有对应板块权限。</li>
<li>部分卖出共享同一日订单的最低佣金；跨交易日重报重新计算。卖出结算后才能把现金用于后续新订单，待买预算预留，禁止透支。</li>
<li>__AUDIT__。所有成交均回查原始缓存价格区间和成交量，另用成交账本重新计算现金及剩余股份；没有为了漂亮曲线强制平掉期末持仓。</li>
</ul><p>已核实*ST萃华2026-09-01起停牌；零成交量柱不能当作可交易报价。这类持仓停牌与数据价格基准异常分开记录；原策略可把资金长期锁在退市风险股票中。</p>
<p><a href="https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml">上交所交易规则</a> · <a href="https://edu.sse.com.cn/tib/ysptj/c/4768085.shtml">科创板申报数量</a> · <a href="https://www.mof.gov.cn/jrttts/202308/t20230828_3904235.htm">财政部印花税公告</a> · <a href="https://m.chinaclear.cn/zdjs/dsj/about_dsj.shtml">中国结算费率调整记录</a> · <a href="https://static.cninfo.com.cn/finalpage/2026-09-01/1225539050.PDF">萃华停牌公告</a></p>
</section><h2>可复核文件</h2><section><p><a href="20万五只持仓模拟成交.xlsx">Excel：账户比较、逐笔成交、持仓、每日现金和盈亏</a> · <a href="账户方案对比.csv">全部方案CSV</a> · <a href="行情问题与停牌核查.csv">行情核查CSV</a> · <a href="validation.json">账本审计结果</a></p><p><a href="protocol.json">第一轮固定规则</a> · <a href="preopen_protocol.json">盘前候选规则与选择历史</a> · <a href="candidate_strategy.json">待验证候选配置</a></p><p class="muted">每个 runs/ 目录保留订单、尝试、成交、未入选原因、盘前候选、日持仓和退出决定。信号依赖原数据文件SHA256，补充成交量独立保存；未写入正式交易结果或券商接口。</p></section>
</main><script>const D=__PAYLOAD__;
const el=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function grid(rows,cols){return '<div class="scroll"><table><thead><tr>'+cols.map(c=>'<th>'+c[1]+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(r[c[0]])+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
function render(){const key=el('run').value,d=D[key],q=el('filter').value.trim(),s=d.summary;
el('stats').textContent=`期末资产 ${s.final_assets.toLocaleString()} 元；现金 ${d.cash.toLocaleString()} 元；已实现盈亏 ${s.realized_pnl.toLocaleString()} 元；浮动盈亏 ${s.unrealized_pnl.toLocaleString()} 元。`;
el('holdings').innerHTML=grid(d.ending_holdings,[['code','期末股票'],['name','名称'],['entry_at','入场区间结束'],['shares','股数'],['entry_price','买入价'],['mark','末次价'],['market_value','市值'],['unrealized_pnl','浮盈亏']]);
const f=d.fills.filter(x=>!q||x.code.includes(q)||x.name.includes(q)).map(x=>({...x,action:x.action==='buy'?'买入':'卖出',participation:(x.participation*100).toFixed(3)+'%',source:x.source==='actual_1m'?'实际1分钟聚合':'历史3分钟恢复',partial:x.partial?'是':'否'}));
el('fills').innerHTML=grid(f,[['code','代码'],['name','名称'],['action','方向'],['signal_at','信号确认时间'],['execution_start','撮合窗口开始'],['execution_end','撮合窗口结束'],['price','成交价'],['shares','股数'],['notional','金额'],['fees','费用'],['cash_after','现金余额'],['participation','占该柱成交量'],['partial','部分成交'],['source','行情来源']]);
el('fillcount').textContent=`显示 ${f.length} 笔模拟成交；买单 ${s.selected_orders} 笔，未成交买单 ${s.rejected_buys} 笔。`;
el('links').innerHTML=`<a href="runs/${key}/fills.csv">成交CSV</a> · <a href="runs/${key}/decisions.csv">选股/拒绝明细</a> · <a href="runs/${key}/orders.csv">订单明细</a> · <a href="runs/${key}/quality.json">本账户行情问题</a>`;}
el('run').value='preopen_normal__low_vol';el('run').addEventListener('change',render);el('filter').addEventListener('input',render);render();</script></html>'''
    cards='<div class="cards">'+''.join(f'<div class="card"><span>{label}</span><div class="big">{value}</div></div>' for label,value in [
        ('初始本金','200,000元'),('持仓＋待买上限','5只'),('每只新仓预算','净资产20%'),('已验证账户路径',str(len(summaries)))])+'</div>'
    replacements={'CARDS':cards,'COMPARISON':comparison,'SENSITIVITY':sensitivity,'OPTIONS':options,'PAYLOAD':payload,
        'BEST':f"{best['pnl']:+,.2f}",'STRESS':f"{stress['pnl']:+,.2f}",'RECENT':f"{recent['pnl']:+,.2f}",
        'NOST':no_st_text,'AUDIT':f"共{audit['runs']}个账户、{audit['account_days_checked']}个账户交易日、{audit['fills_checked']}笔模拟成交通过账本核查"}
    for k,v in replacements.items():page=page.replace('__'+k+'__',v)
    (ROOT/'策略1_20万五只持仓模拟成交报告.html').write_text(page,encoding='utf-8')
    candidate=dict(status='research_candidate_not_production',initial_cash=200000,max_positions=5,
        new_position_budget_fraction=.2,rotation=False,selection_time='preopen',watchlist_size=5,
        eligible='original daily MACD buy watch AND ADX14 >= 20 AND ADX14 > ADX14[t-3]',
        rank='ascending previous completed 20-session pct-change standard deviation',
        ranking_tiebreak='code_ascending',risk_name_variants=[False,True],
        risk_name_caution='The variant excluding ST/delisting labels loses money under the strict full-window fill stress; not approved for production by this study',
        entry='original 3 consecutive 3m confirmations',exit='original MACD/E2 + T+1',
        evidence='retrospective shared cash OHLCV simulation; prospective validation outstanding')
    (ROOT/'candidate_strategy.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2))
    manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for pattern in ['runs/*/*.json','signals/*.json.gz','volumes/*.json.gz','ranking_inputs.parquet'] for p in ROOT.glob(pattern)}
    (ROOT/'artifact_hashes.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in audit.items() if k!='runs_detail'},ensure_ascii=False))
    print(ROOT/'策略1_20万五只持仓模拟成交报告.html')


if __name__=='__main__':
    main()
