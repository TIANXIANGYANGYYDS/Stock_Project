import numpy as np
import pandas as pd
import pytest

from app.quant.research.alpha import benchmark_metrics, benjamini_hochberg


def series(values):
    return pd.Series(values, index=pd.date_range('2026-07-01', periods=len(values)))


def test_alpha_is_intercept_not_cumulative_outperformance():
    market = series(np.sin(np.arange(80)) * .015 + .001)
    rf = (1.02 ** (1 / 252)) - 1
    portfolio = rf + .0003 + 1.7 * (market - rf)
    result = benchmark_metrics(portfolio, market, annual_risk_free=.02)
    assert result['beta'] == pytest.approx(1.7)
    assert result['alpha_daily'] == pytest.approx(.0003)
    assert result['alpha_annual_linear'] == pytest.approx(.0756)
    assert result['cumulative_excess_return'] == pytest.approx((1 + portfolio).prod() - (1 + market).prod())
    assert result['relative_wealth_return'] == pytest.approx((1 + portfolio).prod() / (1 + market).prod() - 1)
    assert result['alpha_daily'] != pytest.approx(result['cumulative_excess_return'])


def test_same_benchmark_has_zero_alpha_and_unit_beta():
    market = series(np.cos(np.arange(70)) * .01)
    result = benchmark_metrics(market, market)
    assert result['alpha_daily'] == pytest.approx(0, abs=1e-14)
    assert result['beta'] == pytest.approx(1)
    assert result['cumulative_excess_return'] == 0
    assert result['information_ratio'] is None
    assert result['alpha_pvalue'] == 1


def test_refuses_dropped_dates_and_missing_returns():
    market = series([.01] * 40)
    with pytest.raises(ValueError, match='match exactly'):
        benchmark_metrics(market.iloc[1:], market)
    bad = market.copy(); bad.iloc[2] = np.nan
    with pytest.raises(ValueError, match='invalid returns'):
        benchmark_metrics(bad, market)
    assert benchmark_metrics(market, market)['regression_status'] == 'constant_benchmark'
    assert benchmark_metrics(market[:10], market[:10])['alpha_daily'] is None


def test_hac_uncertainty_and_risk_free_series():
    rng = np.random.default_rng(125)
    b = series(rng.normal(0, .01, 100))
    rf = series(np.linspace(0, .0002, 100))
    p = rf + .0001 + .8 * (b - rf) + rng.normal(0, .002, 100)
    r = benchmark_metrics(p, b, risk_free_daily=rf)
    assert r['alpha_hac_se'] > 0
    assert r['alpha_ci95_low'] < r['alpha_daily'] < r['alpha_ci95_high']
    assert 0 <= r['alpha_pvalue'] <= 1
    assert r['risk_free_basis'] == 'provided_daily_series'
    assert benjamini_hochberg([.01, None, .04, .03]) == pytest.approx([.03, None, .04, .04])
