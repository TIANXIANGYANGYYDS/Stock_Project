import numpy as np
import pandas as pd
import pytest

from app.quant.research import factor_alpha_study


def test_partial_history_keeps_valid_cash_days_and_matches_monthly_window(tmp_path, monkeypatch):
    """The evaluation starts at an available decision, not the first purchase.

    Before availability, zero-return cash is just an implementation placeholder
    and must not be advertised as a factor's monthly track record.
    """
    calendar = pd.bdate_range('2026-01-01', periods=100).strftime('%Y-%m-%d').tolist()
    codes = [f'{i:06d}' for i in range(1, 26)]
    panel = pd.MultiIndex.from_product([calendar, codes], names=['trade_date', 'code']).to_frame(index=False)
    panel['normalized_open'] = panel['normalized_close'] = 1.
    panel['close'] = 10.
    panel['volume'] = 1000.
    context = pd.DataFrame({'trade_date': calendar, 'environment': np.nan})
    context.loc[51:63, 'environment'] = 0.
    context.loc[64:, 'environment'] = 1.
    benchmark = pd.concat([
        pd.DataFrame({'date': calendar, 'code': symbol,
                      'daily_return': np.sin(np.arange(100)) * .01})
        for symbol in ['sh.000001', 'sh.000300']
    ], ignore_index=True)
    monkeypatch.setattr(factor_alpha_study, 'CATALOG', [])
    summary = factor_alpha_study.run_study(
        panel=panel, stock_context=pd.DataFrame(columns=['trade_date', 'code']),
        market_context=context,
        context_specs=[{'key': 'environment', 'label': '环境', 'family': '环境', 'role': 'market_context'}],
        benchmark=benchmark, calendar=calendar, codes=codes,
        start=calendar[20], end=calendar[-1], output=tmp_path,
    )
    metrics = pd.read_parquet(tmp_path / 'portfolio_metrics.parquet').set_index('scenario')
    row = metrics.loc['environment::high']
    assert row.start_date == calendar[61]  # 10 prior observations; initially cash.
    assert row.sessions == 39
    assert not row.comparable_full_window
    curves = pd.read_parquet(tmp_path / 'portfolio_curves.parquet')
    curve = curves[curves.scenario.eq('environment::high')]
    assert curve.loc[curve.holding_count.gt(0), 'trade_date'].iloc[0] == calendar[64]
    evaluated = curve[curve.trade_date.ge(row.start_date)]
    assert row.mean_cash_weight == pytest.approx(evaluated.cash_weight.mean())
    monthly = pd.read_csv(tmp_path / '逐月表现.csv')
    monthly = monthly[monthly.scenario.eq('environment::high')]
    assert monthly.sessions.sum() == 39
    assert (1 + monthly.return_value).prod() - 1 == pytest.approx(row.total_return)
    matched = benchmark[benchmark.code.eq('sh.000001') & benchmark.date.ge(row.start_date)]
    assert row.benchmark_return == pytest.approx((1 + matched.daily_return).prod() - 1)
    assert summary['full_window_portfolios'] == 1  # Only the constant-universe baseline.

