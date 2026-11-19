"""Causal daily factors for the complete catalog, independent of trade outcomes.

Rows are labelled by the completed session. Portfolio code must shift them to
the next session. Rolling daily factors require consecutive calendar rows;
missing sessions are not silently shortened. Source vintage remains recorded.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable

import numpy as np
import pandas as pd

from app.quant.research.factors import FactorBar, _adx, _atr, _rsi, _true_ranges


CATALOG = json.loads(Path(__file__).with_name('factor_catalog.json').read_text())


def board(code: str) -> str:
    if code.startswith(('300', '301')): return 'chinext'
    if code.startswith(('688', '689')): return 'star'
    if code.startswith(('4', '8', '920')): return 'bse'
    return 'sh_main' if code.startswith('6') else 'sz_main'


def build_daily_factors(
    daily: pd.DataFrame,
    *,
    sessions: list[str],
    output_start: str,
    flow: pd.DataFrame | None = None,
    industry: pd.DataFrame | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Daily OHLCV inputs include pre-start warmup, not forward outcome labels."""
    required = {'code','trade_date','open','high','low','close','pct_chg','volume','amount','turnover_pct'}
    if not required <= set(daily): raise ValueError('Missing daily input columns')
    if daily[['code','trade_date']].duplicated().any(): raise ValueError('Duplicate stock dates')
    if sessions != sorted(set(sessions)) or output_start not in sessions: raise ValueError('Invalid factor calendar')
    daily = daily[daily.trade_date.isin(sessions)].sort_values(['code','trade_date']).copy()
    flow_by_code = {}
    if flow is not None:
        if flow[['code','trade_date']].duplicated().any(): raise ValueError('Duplicate flow dates')
        flow_by_code = {c:x.set_index('trade_date') for c,x in flow.groupby('code',sort=False)}
    panels=[]; groups=list(daily.groupby('code',sort=False))
    for number,(code,raw) in enumerate(groups,1):
        raw=raw.set_index('trade_date'); x=raw.reindex(sessions).copy(); x['code']=code
        source_good=(x[['open','high','low','close']].gt(0).all(axis=1) & x.pct_chg.gt(-100)
                     & x.high.ge(x[['open','close']].max(axis=1)) & x.low.le(x[['open','close']].min(axis=1)))
        returns=(x.pct_chg/100).where(source_good)
        logs=np.log1p(returns)
        price=np.exp(logs.cumsum())
        x['normalized_close']=price
        x['normalized_open']=price*x.open/x.close
        x['normalized_high']=price*x.high/x.close
        x['normalized_low']=price*x.low/x.close
        x['daily_valid']=source_good
        for n in (1,3,5,10,20,60):x[f'ret_{n}']=np.expm1(logs.rolling(n).sum())*100
        for n in (5,10,20,60):
            name=f'ma_ma{n}'
            x[f'ma_distance_{n}']=(x.close/x[name]-1)*100 if name in x else np.nan
        x['ma_distance_120']=(price/price.rolling(120).mean()-1)*100
        for n in (5,20,60):x[f'volatility_{n}']=x.pct_chg.where(source_good).rolling(n).std()
        for n in (1,5,20):x[f'turnover_{n}']=x.turnover_pct.where(source_good).rolling(n).mean()
        x['amount_log']=np.log1p(x.amount.where(x.amount.ge(0)))
        x['amount20_log']=np.log1p(x.amount.where(x.amount.ge(0)).rolling(20).mean())
        x['volume_ratio']=x.volume.rolling(5).mean()/x.volume.rolling(20).mean()
        x['turnover_ratio']=x.turnover_pct/x.turnover_pct.rolling(20).mean()
        x['positive_days5']=x.pct_chg.gt(0).astype(float).where(source_good).rolling(5).sum()
        x['drawdown20']=(price/price.rolling(20).max()-1)*100
        reference=x.close/(1+returns)
        x['gap_pct']=(x.open/reference-1)*100
        x['candle_return']=(x.close/x.open-1)*100
        x['close_position']=(x.close-x.low)/(x.high-x.low).replace(0,np.nan)
        x['upper_shadow']=(x.high-x[['open','close']].max(axis=1))/reference*100
        x['lower_shadow']=(x[['open','close']].min(axis=1)-x.low)/reference*100
        x['amplitude']=x.get('amplitude_pct',(x.high-x.low)/reference*100)
        for column,source in [('macd_dif_pct','macd_dif'),('macd_hist_pct','macd_hist')]:
            x[column]=x[source]/x.close*100 if source in x else np.nan
        x['macd_hist_delta']=x.macd_hist.diff()/x.close*100 if 'macd_hist' in x else np.nan
        if {'boll_upper','boll_lower','boll_mid'} <= set(x):
            x['boll_position']=(x.close-x.boll_lower)/(x.boll_upper-x.boll_lower).replace(0,np.nan)
            x['boll_width']=(x.boll_upper-x.boll_lower)/x.boll_mid*100
        else:x['boll_position']=x['boll_width']=np.nan
        for name,source in [('chip_profit','chip_profit_ratio'),('chip_concentration90','chip_cost_90_concentration'),('chip_concentration70','chip_cost_70_concentration')]:
            x[name]=x[source]*100 if source in x else np.nan
        x['chip_cost_distance']=(x.close/x.chip_avg_cost-1)*100 if 'chip_avg_cost' in x else np.nan
        # Stateful Wilder indicators use the ordered valid price observations;
        # missing output dates remain missing, and comparison endpoints use the
        # market calendar. Normalized OHLC avoids mixing adjustment vintages.
        valid=x[source_good & price.notna()]
        bars=[FactorBar(str(d),float(r.normalized_high),float(r.normalized_low),float(r.normalized_close),float(r.volume or 0)) for d,r in valid.iterrows()]
        ranges=_true_ranges(bars)
        for n in (7,14,21):
            x[f'adx_{n}']=pd.Series(_adx(bars,n,ranges),index=valid.index,dtype=float)
            x[f'adx_{n}_3_days_ago']=x[f'adx_{n}'].shift(3)
        for n in (10,14,20):
            atr=pd.Series(_atr(bars,n,ranges),index=valid.index,dtype=float)
            x[f'natr_{n}']=atr/price*100
        for n in (9,14,21):x[f'rsi_{n}']=pd.Series(_rsi(bars,n),index=valid.index,dtype=float)
        # Preserve the prior research ATR initialization as a named variant.
        research_atr=np.full(len(ranges),np.nan)
        if len(ranges)>=14:
            research_atr[13]=np.mean(ranges[:14])
            for i in range(14,len(ranges)):research_atr[i]=(research_atr[i-1]*13+ranges[i])/14
        x['atr_pct']=pd.Series(research_atr,index=valid.index)/price*100
        for n in (20,60,120):
            for skip in (0,5):x[f'rs_momentum_l{n}_s{skip}']=price.shift(max(skip,1)-1)/price.shift(n-1)-1
        for n in (10,20,40):x[f'rtov_{n}']=x.turnover_pct/x.turnover_pct.shift(1).rolling(n).median()
        x['adx']=x.adx_14;x['adx_delta']=x.adx_14-x.adx_14_3_days_ago
        f=flow_by_code.get(code)
        if f is not None:
            f=f.reindex(sessions)
            ratio=f.main_net_yuan/x.amount*100
            okay=(x.amount.gt(0) & (ratio-f.main_ratio_pct).abs().le(.020000001)
                  & (f.main_net_yuan-f.large_net_yuan-f.super_large_net_yuan).abs().le(np.maximum(10,f.main_net_yuan.abs()*.00001)))
            net=f.main_net_yuan.where(okay);amount=x.amount.where(okay)
            x['flow_valid']=okay;x['main_strength']=ratio.where(okay)
            for n in (5,20):x[f'main_strength{n}']=net.rolling(n).sum()/amount.rolling(n).sum()*100
            x['main_net_yi']=net/1e8;x['main_net5_yi']=net.rolling(5).sum()/1e8
            x['main_positive_days5']=net.gt(0).astype(float).where(net.notna()).rolling(5).sum()
            x['main_strength_delta5']=x.main_strength-x.main_strength.rolling(5).mean()
            for size in ('super_large','large','small'):x[f'{size}_strength']=(f[f'{size}_net_yuan']/x.amount*100).where(okay)
            x['flow_up_price_down']=(net.gt(0)&x.pct_chg.lt(0)).astype(float).where(net.notna()&x.pct_chg.notna())
            x['dark_strength']=x.main_strength*x.pct_chg*x.main_net_yi*100
            x['dark_source_change']=x.main_strength*f.flow_change_pct*x.main_net_yi*100
            x['dark_signed_log']=np.sign(x.dark_strength)*np.log1p(x.dark_strength.abs())
            x['dark_abs_log']=np.log1p(x.dark_strength.abs())
            x['dark_preserve_flow']=x.main_strength*x.pct_chg*x.main_net_yi.abs()*100
        x=x.loc[output_start:].copy();x['trade_date']=x.index;x['board']=board(code)
        for spec in CATALOG:
            if spec['key'] not in x:x[spec['key']]=np.nan
        keep=['code','trade_date','name','board','open','high','low','close','volume','amount','pct_chg',
              'normalized_open','normalized_high','normalized_low','normalized_close','daily_valid',
              *[s['key'] for s in CATALOG]]
        if 'name' not in x:x['name']=code
        panels.append(x[keep].reset_index(drop=True))
        if progress and number%200==0:progress(number,len(groups))
    p=pd.concat(panels,ignore_index=True)
    group=p.groupby(['trade_date','board']).ret_20
    count,total=group.transform('count'),group.transform('sum')
    p['relative_return20']=p.ret_20-(total-p.ret_20)/(count-1)
    for n in (20,60,120):
        for skip in (0,5):p[f'rs_rank_l{n}_s{skip}']=p.groupby('trade_date')[f'rs_momentum_l{n}_s{skip}'].rank(pct=True,method='average')
    for n in (10,14,20):p[f'natr_rank_{n}']=p.groupby('trade_date')[f'natr_{n}'].rank(pct=True,method='average')
    if industry is not None and not industry.empty:
        history=industry.rename(columns={'date':'trade_date'}).copy()
        history=history[history.industry.notna()&history.industry_update_date.le(history.trade_date)]
        history=history.sort_values(['code','trade_date']).drop_duplicates(['code','trade_date'])
        history=history[history.industry.ne(history.groupby('code').industry.shift())]
        history['asof']=pd.to_datetime(history.trade_date)
        p['asof']=pd.to_datetime(p.trade_date)
        p=pd.merge_asof(p.sort_values('asof'),history[['code','asof','industry']].sort_values('asof'),on='asof',by='code',direction='backward')
        group=p.groupby(['trade_date','industry']).ret_20;count,total=group.transform('count'),group.transform('sum')
        p['industry_relative20']=(p.ret_20-(total-p.ret_20)/(count-1)).where(count.ge(5))
        p=p.drop(columns='asof')
    return p.replace([np.inf,-np.inf],np.nan).sort_values(['trade_date','code']).reset_index(drop=True)


def signal_features(signals: pd.DataFrame, session_index: dict[str,int]) -> pd.DataFrame:
    """Implement the five event/diagnostic fields without making daily proxies.

    Requires genuine saved signal and execution records. Actual execution
    timing is diagnostic only; no forward information enters selection ranks.
    """
    required={'provisional_dif','signal_price','shrink_ratio','signal_at','entry_at','factor_date'}
    if not required<=set(signals):raise ValueError('Missing signal/entry event fields')
    x=signals.copy()
    if (x.signal_price<=0).any():raise ValueError('Invalid signal price')
    if not (x.factor_date.lt(x.signal_at.str[:10]) & x.signal_at.le(x.entry_at)).all():raise ValueError('Signal availability violation')
    x['dif_pct']=100*x.provisional_dif/x.signal_price
    x['factor_age_sessions']=x.entry_at.str[:10].map(session_index)-x.factor_date.map(session_index)
    x['execution_delay_sessions']=x.entry_at.str[:10].map(session_index)-x.signal_at.str[:10].map(session_index)
    minute=x.entry_at.str[11:13].astype(int)*60+x.entry_at.str[14:16].astype(int)
    x['entry_session_minute']=np.where(minute<780,minute-570,minute-660)
    return x
