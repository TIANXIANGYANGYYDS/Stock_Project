"""Timestamp-qualified observations and separately labelled historical context.

Daily numeric market breadth can be reconstructed after the fact. AI reports,
popularity, billboard and stored theme/stock snapshots require receipt or
generation before the decision. An unavailable source is never a zero event.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd


def flatten_numeric(value: dict, prefix: str = '') -> dict[str, float]:
    """Numeric leaves only; timestamps, evidence lists and prose are not scores."""
    result={}
    for key,item in value.items():
        name=f'{prefix}_{key}' if prefix else str(key)
        if isinstance(item,dict):result.update(flatten_numeric(item,name))
        elif isinstance(item,(int,float,bool)) and math.isfinite(float(item)):
            result[name]=float(item)
    return result


def _before(value, cutoff) -> bool:
    if not value:return False
    stamp=pd.Timestamp(value)
    if stamp.tzinfo is None:return False
    return stamp<=cutoff


def build_observation_factors(
    *, dates: list[str], calendar: list[str], codes: list[str], sources: dict[str,list[dict]],
) -> tuple[pd.DataFrame,pd.DataFrame,list[dict],dict]:
    """Return decisions-at-open rows, never completed-date rows to shift twice."""
    indices={d:i for i,d in enumerate(calendar)}
    code_set=set(codes)
    sentiment={r['trade_date']:r for r in sources.get('market_sentiment_daily',[]) if r.get('scope')=='hs_mainboard'}
    morning={r['trade_date']:r for r in sources.get('daily_market_analysis',[])}
    hot=sorted([r for r in sources.get('stock_ths_hot_rank_snapshots',[]) if r.get('observed_at') and len(r.get('stocks',[]))>=100],key=lambda r:pd.Timestamp(r['observed_at']))
    billboards=sources.get('stock_billboard_daily',[])
    snapshots={r['meta']['trade_date']:r for r in sources.get('short_sentiment_snapshots',[]) if 'meta' in r}
    original_market={'limit_up','limit_down','seal_rate','height','amount_ratio','advancing','declining','up_over_5','down_over_5','promotion','yesterday_up_return'}
    stock_fields=['change_ratio','turnover_ratio','amount_yuan','open_change_ratio','drawdown_from_high_ratio',
                  'is_limit_up','is_limit_down','is_sealed','touched_up','seal_amount_yuan','seal_volume_shares',
                  'observed_open_count','confirmed_streak_before_today','intraday_streak','target_streak']
    quality_keys={'coverage','eligible','expected','missing','excluded','unknown_streak','N','expected_N','quality','delta15_N'}
    specs={};stock_rows=[];market_rows=[]
    audit={'morning_dates':0,'hot_dates':0,'billboard_complete_dates':0,'snapshot_dates':0,'historically_reconstructed_market_dates':0,'late_snapshots_rejected':0}
    def register(key,label,role,source,basis):
        specs[key]={'key':key,'label':label,'family':source,'role':role,'availability_basis':basis}
    for day in dates:
        j=indices[day]
        if j==0:raise ValueError('Decision calendar needs a prior session')
        previous=calendar[j-1];cutoff=pd.Timestamp(day+'T09:30:00+08:00')
        market={'trade_date':day};by_stock={}
        def stock(code):return by_stock.setdefault(code,{'trade_date':day,'code':code})
        m=sentiment.get(previous)
        if m:
            if not _before(m.get('recorded_at'),cutoff):audit['historically_reconstructed_market_dates']+=1
            for key,value in flatten_numeric(m.get('values',{})).items():
                name='market_'+key if key in original_market else 'market_extra_'+key
                market[name]=value
                register(name,'市场日统计 '+key,'data_quality' if key in quality_keys else 'market_context','日级市场情绪','prior_close_historical_reconstruction')
        m=morning.get(day)
        if m and m.get('status')=='completed' and _before(m.get('updated_at'),cutoff):
            audit['morning_dates']+=1
            market.update(morning_risk_score={'low':0,'medium':1,'high':2}.get(m.get('analysis',{}).get('risk_level'),np.nan),
                          morning_bias_score={'bearish':-1,'neutral':0,'bullish':1}.get(m.get('analysis',{}).get('market_bias'),np.nan),
                          news_completion=m.get('news_window',{}).get('completion_ratio',np.nan))
            for field in ['morning_risk_score','morning_bias_score','news_completion']:
                register(field,field,'data_quality' if field=='news_completion' else 'market_context','盘前分析','updated_at_before_open')
            # Aggregate report breadth is known; arbitrary sector->stock text
            # matching is not. Preserve news sector scores as a separate table.
            for kind in ['heat_ranking','investment_ranking']:
                items=m.get(kind) or []
                if items:
                    for field in ['final_score','news_count','positive_news_count','negative_news_count','neutral_news_count','recent_news_count','source_count']:
                        values=[float(x[field]) for x in items if isinstance(x.get(field),(int,float))]
                        if values:
                            name=f'news_{kind}_{field}_mean';market[name]=float(np.mean(values))
                            register(name,f'{kind}板块均值 {field}','market_context','新闻板块评分','report_updated_before_open')
        eligible_hot=[r for r in hot if r['trade_date'] in (day,previous) and _before(r['observed_at'],cutoff)]
        if eligible_hot:
            audit['hot_dates']+=1;chosen=eligible_hot[-1];lookup={r['code']:r for r in chosen['stocks']}
            for code in codes:
                item=lookup.get(code);row=stock(code)
                row.update(hotrank_top100=float(item is not None),hotrank_rank=float(item['rank']) if item else 101.)
                if item:
                    for source,target in [('heat','hotrank_heat'),('rank_change','hotrank_change')]:
                        value=item.get(source)
                        if isinstance(value,(int,float)) and math.isfinite(value):row[target]=float(value)
            for field in ['hotrank_top100','hotrank_rank','hotrank_heat','hotrank_change']:
                register(field,field,'cross_section','热榜','observed_before_open; outside_top100_is_not_missing_snapshot')
        previous_five=calendar[max(0,j-5):j]
        selected=[r for r in billboards if r['trade_date'] in previous_five and r.get('publication_status')=='observed' and _before(r.get('observed_at'),cutoff)]
        if len(previous_five)==5 and len({r['trade_date'] for r in selected})==5:
            audit['billboard_complete_dates']+=1;counts={};net={}
            for r in selected:
                seen=set()
                for item in r.get('records',[]):
                    code=item.get('code');identity=(code,item.get('buy_amount_yuan'),item.get('sell_amount_yuan'),item.get('net_amount_yuan'))
                    if identity in seen:continue
                    seen.add(identity);counts[code]=counts.get(code,0)+1
                    value=item.get('net_amount_yuan')
                    if isinstance(value,(int,float)):net[code]=net.get(code,0.)+value
                    else:net[code]=np.nan
            for code in codes:stock(code).update(billboard_count5=float(counts.get(code,0)),billboard_net5_yi=net.get(code,0.)/1e8)
            for field in ['billboard_count5','billboard_net5_yi']:
                register(field,field,'cross_section','龙虎榜','five_complete_disclosed_sessions; exact_duplicate_amounts_deduplicated')
        snapshot=snapshots.get(previous)
        if snapshot and _before(snapshot['meta'].get('generated_at'),cutoff) and _before(snapshot['meta'].get('as_of'),cutoff) and snapshot['meta'].get('is_final'):
            audit['snapshot_dates']+=1
            sm=snapshot.get('market',{})
            for block in ['values','feedback']:
                for key,value in flatten_numeric(sm.get(block,{})).items():
                    name=f'short_market_{block}_{key}';market[name]=value
                    role='data_quality' if any(k in key.split('_') for k in ['coverage','observed','sample','missing','eligible','expected']) else 'market_context'
                    register(name,name,role,'盘中情绪收盘快照','generated_before_next_open')
            emotion=sm.get('emotion',{})
            for key,value in flatten_numeric(emotion.get('facts',{})).items():
                name='short_market_fact_'+key;market[name]=value
                register(name,name,'market_context','大盘环境','generated_before_next_open')
            state=emotion.get('state')
            for state_name in ['冰点','过冷','微冷','微热','过热','沸点']:
                if state:
                    name='short_market_state_'+state_name;market[name]=float(state==state_name)
                    register(name,name,'market_context','大盘状态','generated_before_next_open; categorical_one_hot')
            themes={t['theme_id']:t for t in snapshot.get('themes',[])}
            for code,item in snapshot.get('stocks',{}).items():
                if code not in code_set or not item.get('valid'):continue
                row=stock(code)
                for field in stock_fields:
                    value=item.get(field)
                    if isinstance(value,(bool,int,float)) and math.isfinite(float(value)):
                        name='observed_stock_'+field;row[name]=float(value)
                        register(name,name,'cross_section','个股盘中路径','prior_day_final_observed_snapshot')
                for path in ['board_run','popularity']:
                    for field,value in flatten_numeric(item.get(path) or {}).items():
                        if field in ['window_days','limit_up_days','rank','heat','rank_change']:
                            name=f'observed_{path}_{field}';row[name]=value
                            register(name,name,'cross_section','连板与热度','prior_day_final_observed_snapshot')
                theme=themes.get(item.get('attributed_theme_id'))
                if theme:
                    for block in ['values','feedback']:
                        for key,value in flatten_numeric(theme.get(block,{})).items():
                            name=f'theme_{block}_{key}';row[name]=value
                            role='data_quality' if any(k in key.split('_') for k in ['coverage','observed','sample','missing','eligible','expected']) else 'cross_section'
                            register(name,name,role,'题材环境','historically_stored_attribution_before_open')
                    emotion=theme.get('emotion',{})
                    for key,value in flatten_numeric(emotion.get('facts',{})).items():
                        name='theme_fact_'+key;row[name]=value
                        register(name,name,'cross_section','题材结构','historically_stored_attribution_before_open')
                    for state_name in ['整理','启动','主升','分歧','高潮','退潮']:
                        if emotion.get('state'):
                            name='theme_state_'+state_name;row[name]=float(emotion['state']==state_name)
                            register(name,name,'cross_section','题材状态','prior_day_state_one_hot')
        elif snapshot:audit['late_snapshots_rejected']+=1
        stock_rows.extend(by_stock.values());market_rows.append(market)
    return pd.DataFrame(stock_rows),pd.DataFrame(market_rows),list(specs.values()),audit
