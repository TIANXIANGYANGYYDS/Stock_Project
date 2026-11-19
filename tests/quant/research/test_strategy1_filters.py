from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.quant.research.strategy1_filters import gate_result, rank_inputs, replay_paths
from app.quant.runtime.daily_macd import DailyMacdState
from app.quant.runtime.live import LiveThreeMinuteBar
import app.quant.runtime.live as live


def test_ranks_preserve_missing_values_ties_and_coverage():
    codes=[f'{i:06d}' for i in range(25)]
    p=pd.DataFrame(dict(code=codes,trade_date='2026-06-30',normalized_close=1.,volume=1.,
                        volatility_20=range(25),volatility_60=1.,turnover_20=range(25)))
    p.loc[:9,'turnover_20']=np.nan
    out=rank_inputs(p,codes)
    assert out.turnover_20_rank.isna().all()
    assert out.volatility_60_rank.nunique()==1
    assert out.volatility_20_rank.iloc[0]==1/25
    assert gate_result((('volatility_20',.2),),out.iloc[0].to_dict())[0]
    assert not gate_result((('turnover_20',.4),),out.iloc[0].to_dict())[0]


def test_filter_replays_later_entry_instead_of_deleting_baseline_trade(monkeypatch):
    monkeypatch.setattr(live,'provisional_daily_indicator_from_state',lambda *a,**k:
        SimpleNamespace(dif=1.,dea=.5,histogram=1.))
    monkeypatch.setattr(live,'confirm_provisional_histogram',lambda **k:(True,.1))
    dates=['2026-07-01','2026-07-02','2026-07-03']
    previous=['2026-06-30',*dates[:-1]]
    inputs={};bars={}
    for day,prior,price in zip(dates,previous,[10.,11.,11.5]):
        inputs[day]=dict(code='000001',name='测试',action='sell' if day==dates[-1] else 'buy',
            observation_before_date=prior,observation_date=prior,previous_close=price,
            reference_histogram=2. if day==dates[-1] else -2.,previous_state=DailyMacdState(12.,11.,.2),
            adx=19. if day==dates[-1] else 30.,adx_3_days_ago=25.,factor_completed_date=prior,
            factor_comparison_date='2026-06-25')
        start=datetime.fromisoformat(day+'T09:30:00+08:00')
        bars[day]=tuple(LiveThreeMinuteBar(
            (start+timedelta(minutes=3*i)).isoformat(),(start+timedelta(minutes=3*(i+1))).isoformat(),
            price,price+.03,price-.03,price,price) for i in range(4))
    ranks={previous[0]:{'volatility_20_rank':.9},previous[1]:{'volatility_20_rank':.1}}
    results=replay_paths(code='000001',dates=dates,inputs=inputs,bars=bars,ranks=ranks)
    base=results['normal::baseline'];filtered=results['normal::vol20_q20']
    assert len(base['trades'])==len(filtered['trades'])==1
    assert base['trades'][0]['entry_execution_at'].startswith(dates[0])
    assert filtered['trades'][0]['entry_execution_at'].startswith(dates[1])
    assert filtered['trades'][0]['outcome']=='closed'
    assert filtered['curve'][0][0]==100000
    assert base['curve'][-1][0]>filtered['curve'][-1][0]
    assert results['double_cost::vol20_q20']['curve'][-1][0]<filtered['curve'][-1][0]


def test_cross_day_orders_keep_their_candidates_after_macd_changes(monkeypatch):
    monkeypatch.setattr(live,'provisional_daily_indicator_from_state',lambda *a,**k:
        SimpleNamespace(dif=1.,dea=.5,histogram=1.))
    monkeypatch.setattr(live,'confirm_provisional_histogram',lambda **k:(True,.1))
    dates=['2026-07-01','2026-07-02','2026-07-03','2026-07-06']
    inputs={};bars={}
    for i,day in enumerate(dates):
        prior='2026-06-30' if i==0 else dates[i-1]
        action={0:'buy',2:'sell'}.get(i)
        inputs[day]=dict(code='000001',name='测试',action=action,
            observation_before_date=prior,observation_date=prior,previous_close=10.,
            reference_histogram=-2. if action=='buy' else 2.,previous_state=DailyMacdState(12.,11.,.2),
            adx=30. if i<2 else 19.,adx_3_days_ago=25.,factor_completed_date=prior,
            factor_comparison_date='2026-06-25')
        start=datetime.fromisoformat(day+'T14:51:00+08:00') if i%2==0 else datetime.fromisoformat(day+'T09:30:00+08:00')
        bars[day]=tuple(LiveThreeMinuteBar(
            (start+timedelta(minutes=3*j)).isoformat(),(start+timedelta(minutes=3*(j+1))).isoformat(),
            10.,10.03,9.97,10.,10.) for j in range(3 if i%2==0 else 4))
    result=replay_paths(code='000001',dates=dates,inputs=inputs,bars=bars,ranks={})['normal::baseline']
    assert len(result['trades'])==1
    trade=result['trades'][0]
    assert trade['entry_execution_at']==dates[1]+'T09:30:00+08:00'
    assert trade['exit_execution_at']==dates[3]+'T09:30:00+08:00'
    assert trade['outcome']=='closed'

