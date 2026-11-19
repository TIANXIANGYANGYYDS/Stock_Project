"""Offline, searchable factor results with matched-date benchmark curves."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill


def render_report(root: Path) -> None:
    scores=pd.read_parquet(root/'portfolio_metrics.parquet')
    curves=pd.read_parquet(root/'portfolio_curves.parquet')
    status=pd.read_csv(root/'全部因子实现与覆盖.csv')
    monthly=pd.read_csv(root/'逐月表现.csv')
    summary=json.loads((root/'summary.json').read_text())
    protocol=json.loads((root/'protocol.json').read_text())
    benchmark_returns=pd.read_parquet(root/'benchmark.parquet').query("code == 'sh.000001'").set_index('date').daily_return
    display=scores[['scenario','label','group','kind','comparable_full_window','start_date','end_date','sessions',
                    'total_return','benchmark_return','cumulative_excess_return','relative_wealth_return',
                    'alpha_daily','beta','alpha_ci95_low','alpha_ci95_high','alpha_bh_q','maximum_drawdown',
                    'double_cost_excess','same_universe_excess','mean_cash_weight','max_stale_mark_weight']].copy()
    percentages=['total_return','benchmark_return','cumulative_excess_return','relative_wealth_return','maximum_drawdown',
                 'double_cost_excess','same_universe_excess','mean_cash_weight','max_stale_mark_weight']
    for key in percentages:display[key]=display[key]*100
    for key in ['alpha_daily','alpha_ci95_low','alpha_ci95_high']:display[key]*=10000
    display=display.rename(columns={
        'scenario':'组合标识','label':'指标','group':'组别','kind':'组合类型','comparable_full_window':'完整窗口可比较',
        'start_date':'评估起点','end_date':'评估终点','sessions':'交易日数','total_return':'组合净收益%',
        'benchmark_return':'同期上证收益%','cumulative_excess_return':'累计超额_百分点','relative_wealth_return':'相对财富收益%',
        'alpha_daily':'每日Alpha_基点','beta':'市场Beta','alpha_ci95_low':'Alpha95%下界_基点','alpha_ci95_high':'Alpha95%上界_基点',
        'alpha_bh_q':'Alpha多重检验q','maximum_drawdown':'最大回撤%','double_cost_excess':'双倍成本超额_百分点',
        'same_universe_excess':'相对同池等权超额_百分点','mean_cash_weight':'平均现金比例%','max_stale_mark_weight':'最大旧价格估值占比%'})
    selected=['equal_weight_universe','volatility_60','volatility_20','turnover_20','main_strength','dark_strength','industry_relative20','adx']
    highlights=display[scores.factor.isin(selected)&scores.group.isin(['all','Q1','Q5'])]
    deferred=pd.DataFrame([
        {'项目':'dif_pct、shrink_ratio','状态':'信号事件公式已实现','限制':'仅在真实策略信号时点定义；不能捏造87日每日横截面，另存事件指标'},
        {'项目':'因子滞后、执行延迟、成交分钟','状态':'执行诊断已实现','限制':'成交后才完整可知，禁止当作同次交易买入因子'},
        {'项目':'新闻完成率、覆盖率及样本数','状态':'质量控制已实现','限制':'单列质量，不作为有效收益信号宣称'},
        {'项目':'题材特征/状态','状态':'快照数值与状态适配器已实现','限制':'需要当时可得的个股题材归属和有效题材值；没有合格映射时不生成组合'},
        {'项目':'财务、估值、市值、机构及融资融券','状态':'缺少本轮已核验的历史输入','限制':'需要公告/可得时间的历史面板；不能回填今天的值'},
        {'项目':'DDE：DDX/DDY/DDZ','状态':'缺少历史源','限制':'主力资金净流入不等于DDE，未伪造替代值'},
        {'项目':'创作者观点与历史评分','状态':'缺少完整时点版本面板','限制':'未来验真结果不能作为过去的评分；不生成回填收益'},
        {'项目':'逐分钟事件计数与精确封单','状态':'当前日级研究不代替分钟/盘口重放','限制':'仅使用实际留存的上一日快照字段；不能从OHLC恢复订单簿'},
    ])
    deferred.to_csv(root/'未就绪及非选股字段.csv',index=False,encoding='utf-8-sig')
    tables={'组合结果_中文':display,'原始统计_小数':scores,'全部因子状态':status,'逐月表现':monthly,
            '逐日净值':curves,'限制与待补数据':deferred}
    if (root/'策略1现有账本Alpha.csv').exists():tables['策略1账本对照']=pd.read_csv(root/'策略1现有账本Alpha.csv')
    if (root/'信号事件指标.csv').exists():tables['真实信号事件']=pd.read_csv(root/'信号事件指标.csv',dtype={'code':str})
    with pd.ExcelWriter(root/'因子收益与Alpha.xlsx',engine='openpyxl') as writer:
        for name,frame in tables.items():
            frame.to_excel(writer,sheet_name=name,index=False)
            ws=writer.sheets[name];ws.freeze_panes='D2';ws.auto_filter.ref=ws.dimensions
            for cell in ws[1]:cell.font=Font(color='FFFFFF',bold=True);cell.fill=PatternFill('solid',fgColor='164C65')
            for column in ws.columns:
                ws.column_dimensions[column[0].column_letter].width=26 if name!='限制与待补数据' else 55
                if ws.max_row<2000:
                    for cell in column[1:]:cell.alignment=Alignment(vertical='top',wrap_text=True)
    chart={}
    labels=scores.set_index('scenario')
    for scenario,frame in curves.groupby('scenario',sort=False):
        record=labels.loc[scenario];factor=record.factor
        target=chart.setdefault(factor,{'label':str(record.label),'groups':{}})
        rows=frame[['trade_date','nav','benchmark_nav','relative_nav']].copy()
        # Do not draw pre-availability cash as a valid factor track record.
        rows=rows[rows.trade_date.ge(record.start_date)]
        if len(rows):
            start_nav=rows.nav.iloc[0]/(1+frame[frame.trade_date.eq(record.start_date)].daily_return.iloc[0])
            benchmark_previous=rows.benchmark_nav.iloc[0]/(1+benchmark_returns.loc[record.start_date])
            rows['nav']=rows.nav/start_nav;rows['benchmark_nav']=rows.benchmark_nav/benchmark_previous
            rows['relative_nav']=rows.nav/rows.benchmark_nav
        target['groups'][record.group]=json.loads(rows.to_json(orient='records'))
    count_positive=int((scores.comparable_full_window&scores.alpha_daily.gt(0)&scores.alpha_bh_q.lt(.05)).sum())
    short_text=f'''<h1>因子收益与 Alpha</h1><p class="lead">{summary['start']} — {summary['end']} · {summary['sessions']}个交易日 · 期初已知股票池{summary['universe_codes']:,}只</p>
<p>本次登记<strong>{summary['registered_fields']}个字段</strong>（原121项及可读取的环境扩展），运行<strong>{summary['portfolios']}个研究组合</strong>，其中{summary['full_window_portfolios']}个满足完整窗口比较条件。各组是同一套协议的对照，不是独立发现的有效策略。</p>
<p class="note">同期上证指数收益 <strong>{summary['primary_benchmark_return']:.2%}</strong>。本轮完整窗口组合中，正Alpha且通过全体组合多重检验q&lt;0.05的数量为<strong>{count_positive}</strong>。历史跑赢不等于已证明存在稳定、可交易的Alpha。</p>
<p><strong>累计超额收益</strong> = 组合累计净收益 − 同期上证累计收益（百分点）。<strong>相对财富收益</strong> = 组合期末净值 ÷ 同期指数净值 − 1。<strong>回归Alpha</strong>来自每日模型 <code>Rp − Rf = α + β × (Rb − Rf) + ε</code> 的截距。</p>
<p>主表Alpha以每日基点展示，1基点=0.01%。无风险利率主口径明确假设年化{protocol['rf_annual_assumption']:.1%}，另有1%和2%敏感性；不是实际无风险利率观测值。95%区间使用5阶HAC；q对全部成功回归做BH校正。年化Alpha仅线性乘252，不能把{summary['sessions']}日年化推算当未来收益承诺。</p>
<p>上证使用<strong>000001.SH价格指数</strong>，不是深市000001股票，也不是000888全收益指数。另与沪深300及同股票池等权组合比较，减轻把板块、规模或市场风格当作因子能力的误判。</p>
<p><a href="因子收益与Alpha.xlsx">Excel：全部结果、净值和因子状态</a> · <a href="组合收益与Alpha.csv">统计CSV</a> · <a href="全部因子实现与覆盖.csv">实现与覆盖清单</a> · <a href="protocol.json">固定研究协议</a></p>'''
    css='''body{font-family:system-ui,"Noto Sans CJK SC",sans-serif;background:#f3f6f8;color:#173042;margin:0}main{max-width:1450px;margin:auto;padding:28px}h1{font-size:30px}h2{font-size:22px;margin-top:32px}p,li{line-height:1.75}.lead{color:#526a7b}.note{padding:15px;background:#fff1d5;border-left:4px solid #ae7d1f}table{border-collapse:collapse;font-size:13px;background:white;width:100%}th,td{padding:9px;border:1px solid #d4dfe6;text-align:left;vertical-align:top;min-width:100px}th{background:#164c65;color:white;position:sticky;top:0}tr:nth-child(even){background:#f6f9fb}.scroll{overflow:auto;max-height:640px}input,select{padding:10px;font-size:15px;max-width:95%;margin:8px}input{width:380px}a{color:#086991}#chart{width:100%;height:auto;background:white;border:1px solid #d4dfe6}#legend{line-height:1.8;padding:8px}.controls{background:#e6eff4;padding:12px}code{background:#e6edf2;padding:3px}'''
    controls='''<h2>净值对照</h2><div class="controls"><label>指标 <select id="factor"></select></label><label>图形 <select id="mode"><option value="nav">组合与同期上证净值</option><option value="relative_nav">相对上证净值</option></select></label></div><p>Q1为低值组，Q5为高值组；相同数值不按股票代码强行拆组。市场变量另做高于/低于历史中位数的择时对照。短历史组合只画有可用决策后的区间，各组从自己的有效起点归一；起点不同时分别画出同期上证虚线，可切换相对净值比较。</p><svg id="chart" viewBox="0 0 1200 450"></svg><div id="legend"></div>'''
    method=f'''<h2>统一的研究规则</h2><ul><li>日线选股因子使用前一完整交易日的值，次日开盘执行；分5组等权，每{protocol['rebalance_sessions']}个交易日调整一次。同类因子不各自寻找最佳阈值；同时展示全部组。</li><li>市场环境变量不做股票横截面分组；与此前20个决策日、至少10个有效值的中位数比较，分别研究高值和低值时持有等权股票池，否则持有现金。</li><li>初始净值固定为1，不随新增信号注入资金。双边佣金万一、双边滑点万五、卖出印花税万五；双倍成本单独重算。未强制清仓期末持仓。</li><li>这是使用单位化、可分割持仓的研究组合，未模拟100股整手、最低手续费、订单簿容量或市场冲击。开盘涨跌停按板块与已存名称近似约束；不构成真实成交审计。</li><li>股票价格按保存的日涨跌幅构建一致收益链；没有单独登记现金分红。日线复权版本与上证价格指数的分红口径可能影响比较，已保留原始行情与来源。</li><li>缺少当日价格的持仓按最后已知价格估值，并披露旧价格估值占比；不能把这些记录解释为成功卖出。组合目标缺少足够数据时保持原持仓，绝不把缺失当0。</li><li>股票池根据样本开始前真实历史记录冻结，不纳入后来上市的股票，不因后来退市而删除代码。每天只用上一日可见报价和因子确定候选。</li><li>这是{summary['sessions']}日回溯性因子组合研究；当前正式策略1的MACD/ADX/E2参数没有改变。信号条件下的增量效果，需要另外完整回放原策略路径。</li></ul>'''
    limitations=f'''<h2>覆盖与解释边界</h2><p>“完整窗口”要求{summary['start']}已有有效决策，且至少80%的计划调仓点可用；其他结果按自身有效区间与同日期指数比较，不放入完整窗口排名。因子表覆盖不等于当时实收：价格/资金/市场宽度属于可追踪来源的历史回溯版本；AI报告、热榜、龙虎榜与情绪快照则要求实际记录时间不晚于决策。</p><p>本段历史已被多轮研究使用，未成为新的独立留出集。相似因子及高低组高度相关，{summary['portfolios']}个组合不能当{summary['portfolios']}份独立证据。结果用于筛选下一轮候选，不能直接推断未来能赚钱。</p>'''
    html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>因子收益与Alpha</title><style>'+css+'</style><main>'+short_text+controls+method
    html+='<h2>部分重点对照</h2><p>主力强度=净流入÷成交额×100；暗盘强度按截图算例使用“主力强度百分数×涨跌幅百分数×净流入亿元×100”。这个原公式含净流入的平方，会消除流入与流出的符号区别；另有dark_preserve_flow保留资金方向的对照版本。截图分档尚未被验证为有效交易规则。</p><div class="scroll">'+highlights.to_html(index=False,na_rep='不足样本',float_format=lambda x:f'{x:.4f}')+'</div>'
    html+='<h2>全部组合</h2><input id="search" placeholder="搜索因子、组别或组合标识"><label><input id="full" type="checkbox" style="width:auto" checked>只显示完整窗口</label><p id="count"></p><div class="scroll">'+display.to_html(index=False,table_id='results',na_rep='不足样本',float_format=lambda x:f'{x:.4f}')+'</div>'
    if '策略1账本对照' in tables:
        html+='<h2>原策略1现有账本对照</h2><p>以下仅覆盖正式账本8月20日起的51日，不拼接7月旧版策略。固定全池资本换算与活跃账户时间加权收益分开列出，不能与累计损益/活跃账户初始本金混用。</p><div class="scroll">'+tables['策略1账本对照'].to_html(index=False,na_rep='不足样本',float_format=lambda x:f'{x:.6f}')+'</div>'
    html+=limitations+'<div class="scroll">'+deferred.to_html(index=False)+'</div>'
    html+='<p>方法与基准来源：<a href="https://rpc.cfainstitute.org/-/media/documents/code/gips/measures-risk-adjusted-return.pdf">CFA Institute：Jensen Alpha及Beta的限制</a>；<a href="https://www.sse.com.cn/market/sseindex/indexlist/index.shtml?classid=010202">上海证券交易所：上证指数及全收益指数</a>。指数原始日线来自BaoStock，已与先前独立留存重叠数据核验。</p></main>'
    data=json.dumps(chart,ensure_ascii=False,allow_nan=False).replace('<','\\u003c')
    full_flags=json.dumps(scores.comparable_full_window.tolist())
    html+='''<script>const DATA='''+data+''';const FLAGS='''+full_flags+''';const select=document.getElementById('factor');for(const [key,item] of Object.entries(DATA)){const o=document.createElement('option');o.value=key;o.textContent=item.label+' ['+key+']';select.appendChild(o);}const colors=['#176f99','#ce7135','#4a9460','#ad4d83','#6858ac','#292f34'];const ns='http://www.w3.org/2000/svg';function draw(){const svg=document.getElementById('chart');svg.replaceChildren();const item=DATA[select.value],mode=document.getElementById('mode').value;let series=Object.entries(item.groups).map(([k,v])=>({label:k,values:v.map(x=>({date:x.trade_date,value:x[mode]}))}));if(mode==='nav'){const windows=new Map();for(const rows of Object.values(item.groups)){const key=rows[0].trade_date+' / '+rows[rows.length-1].trade_date;if(!windows.has(key))windows.set(key,rows);}for(const [window,rows] of windows){series.push({label:windows.size===1?'同期上证指数':'上证 '+window,benchmark:true,values:rows.map(x=>({date:x.trade_date,value:x.benchmark_nav}))});}}else{const longest=Object.values(item.groups).sort((a,b)=>b.length-a.length)[0];series.push({label:'基准=1',values:longest.map(x=>({date:x.trade_date,value:1}))});}const dates=[...new Set(series.flatMap(s=>s.values.map(p=>p.date)))].sort(), vals=series.flatMap(s=>s.values.map(p=>p.value));let lo=Math.min(1,...vals),hi=Math.max(1,...vals),pad=Math.max(.01,(hi-lo)*.08);lo-=pad;hi+=pad;const X=d=>80+dates.indexOf(d)/Math.max(1,dates.length-1)*1080,Y=v=>390-(v-lo)/(hi-lo)*340;function node(tag,attrs,text){const el=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;svg.appendChild(el);return el;}for(let i=0;i<6;i++){let v=lo+(hi-lo)*i/5,y=Y(v);node('line',{x1:80,x2:1160,y1:y,y2:y,stroke:'#dce5eb'});node('text',{x:72,y:y+5,'text-anchor':'end','font-size':14},v.toFixed(3));}for(let i=0;i<5;i++){const d=dates[Math.round(i*(dates.length-1)/4)];node('text',{x:X(d),y:420,'text-anchor':'middle','font-size':14},d.slice(5));}series.forEach((s,i)=>node('polyline',{points:s.values.map(p=>X(p.date)+','+Y(p.value)).join(' '),fill:'none',stroke:colors[i%colors.length],'stroke-width':2.3,'stroke-dasharray':s.benchmark?'7 4':'none'}));const legend=document.getElementById('legend');legend.replaceChildren();series.forEach((s,i)=>{const span=document.createElement('span');span.style.color=colors[i%colors.length];span.style.marginRight='25px';span.textContent='━ '+s.label;legend.appendChild(span);});}select.addEventListener('change',draw);document.getElementById('mode').addEventListener('change',draw);draw();function filter(){let n=0,q=document.getElementById('search').value.toLowerCase(),full=document.getElementById('full').checked;document.querySelectorAll('#results tbody tr').forEach((r,i)=>{const okay=(!full||FLAGS[i])&&r.textContent.toLowerCase().includes(q);r.hidden=!okay;if(okay)n++;});document.getElementById('count').textContent=n+' / '+FLAGS.length+' 个组合';}document.getElementById('search').addEventListener('input',filter);document.getElementById('full').addEventListener('change',filter);filter();</script></html>'''
    (root/'因子收益与Alpha报告.html').write_text(html,encoding='utf-8')
