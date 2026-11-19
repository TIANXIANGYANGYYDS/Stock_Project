import warnings
import numpy as np
import pandas as pd
import pytest

from app.quant.research.factor_features import build_daily_factors, signal_features, CATALOG
from app.quant.research.factors import FactorBar, calculate_factor_snapshots
from app.quant.research.factor_context import build_observation_factors


def fixture_daily():
    dates=pd.bdate_range('2025-01-01',periods=180).strftime('%Y-%m-%d').tolist()
    returns=np.sin(np.arange(180)*.4)*.01
    close=10*np.cumprod(1+returns)
    frame=pd.DataFrame(dict(code='000001',trade_date=dates,open=close*.998,high=close*1.01,
                            low=close*.99,close=close,pct_chg=returns*100,volume=1000.,amount=10000.,turnover_pct=1.))
    return dates,frame


def build(frame,dates,start):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',pd.errors.PerformanceWarning)
        return build_daily_factors(frame,sessions=dates,output_start=start)


def test_all_catalog_fields_exist_and_future_edits_do_not_change_past():
    dates,d=fixture_daily();p=build(d,dates,dates[140])
    assert {s['key'] for s in CATALOG} <= set(p)
    changed=d.copy();changed.loc[170:,'pct_chg']=15
    other=build(changed,dates,dates[140])
    pd.testing.assert_frame_equal(p[p.trade_date<dates[170]],other[other.trade_date<dates[170]])
    # Event fields must not be invented as daily substitutes.
    assert p[['dif_pct','shrink_ratio','entry_session_minute']].isna().all().all()


def test_legacy_adx_rsi_natr_rs_formulas_match_at_signal_time():
    dates,d=fixture_daily();p=build(d,dates,dates[150]).set_index('trade_date')
    bars=[FactorBar(r.trade_date,r.high,r.low,r.close,r.volume,r.turnover_pct) for r in d.itertuples()]
    snapshots=calculate_factor_snapshots(bars,market_dates=dates,signal_dates=dates[160:])
    for signal,snapshot in snapshots.items():
        row=p.loc[snapshot.completed_date]
        for key,value in snapshot.values.items():
            assert row[key] == pytest.approx(value,abs=1e-9),key


def test_missing_session_is_not_silently_removed_from_rolling_window():
    dates,d=fixture_daily();d=d[d.trade_date.ne(dates[155])]
    p=build(d,dates,dates[150]).set_index('trade_date')
    assert pd.isna(p.loc[dates[160],'ret_20'])
    assert pd.isna(p.loc[dates[155],'adx'])


def test_late_auxiliary_data_is_not_available_at_open():
    dates=['2026-07-01','2026-07-02']
    sources={'daily_market_analysis':[dict(trade_date=dates[1],status='completed',updated_at='2026-07-02T10:00:00+08:00',analysis={'risk_level':'high'})],
             'stock_ths_hot_rank_snapshots':[dict(trade_date=dates[1],observed_at='2026-07-02T10:00:00+08:00',stocks=[{'code':f'{i:06d}','rank':i+1} for i in range(100)])]}
    stock,market,specs,audit=build_observation_factors(dates=dates[1:],calendar=dates,codes=['000001'],sources=sources)
    assert stock.empty
    assert 'morning_risk_score' not in market
    assert audit['morning_dates']==audit['hot_dates']==0


def test_signal_diagnostics_validate_timing_and_are_separate():
    frame=pd.DataFrame([dict(provisional_dif=.2,signal_price=10.,shrink_ratio=.03,
                            signal_at='2026-07-01T14:00:00+08:00',entry_at='2026-07-02T09:33:00+08:00',factor_date='2026-06-30')])
    row=signal_features(frame,{'2026-06-30':0,'2026-07-01':1,'2026-07-02':2}).iloc[0]
    assert row.dif_pct==2
    assert row.execution_delay_sessions==1
    assert row.factor_age_sessions==2
    assert row.entry_session_minute==3


def test_dark_strength_units_and_loss_of_flow_direction():
    dates, daily = fixture_daily()
    daily['amount'] = 22.75e8
    flow = daily[['code', 'trade_date']].copy()
    flow['main_net_yuan'] = 3626.47e4
    flow['large_net_yuan'] = 1626.47e4
    flow['super_large_net_yuan'] = 2000e4
    flow['small_net_yuan'] = -3626.47e4
    flow['main_ratio_pct'] = flow.main_net_yuan / daily.amount * 100
    flow['flow_change_pct'] = daily.pct_chg
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', pd.errors.PerformanceWarning)
        positive = build_daily_factors(daily, sessions=dates, output_start=dates[170], flow=flow)
        flow[['main_net_yuan', 'large_net_yuan', 'super_large_net_yuan', 'small_net_yuan', 'main_ratio_pct']] *= -1
        negative = build_daily_factors(daily, sessions=dates, output_start=dates[170], flow=flow)
    assert positive.main_strength.iloc[0] == pytest.approx(3626.47e4 / 22.75e8 * 100)
    expected = positive.main_strength * positive.pct_chg * .362647 * 100
    np.testing.assert_allclose(positive.dark_strength, expected)
    np.testing.assert_allclose(positive.dark_strength, negative.dark_strength)
    np.testing.assert_allclose(positive.dark_preserve_flow, -negative.dark_preserve_flow)
