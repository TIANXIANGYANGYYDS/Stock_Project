"""Benchmark-relative performance, with explicit risk-free and sampling units.

All inputs are decimal daily returns on identical, ordered dates. The CAPM
intercept is estimated from excess returns; cumulative outperformance is a
different quantity. HAC intervals are exploratory, not proof of future alpha.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd


def _returns(values: pd.Series, name: str) -> pd.Series:
    if not isinstance(values, pd.Series) or values.empty:
        raise ValueError(f"{name} must be a nonempty dated Series")
    if values.index.has_duplicates or not values.index.is_monotonic_increasing:
        raise ValueError(f"{name} dates must be unique and increasing")
    numeric = values.astype(float)
    if not np.isfinite(numeric).all() or (numeric <= -1).any():
        raise ValueError(f"{name} contains missing or invalid returns")
    return numeric


def benchmark_metrics(
    portfolio: pd.Series,
    benchmark: pd.Series,
    *,
    annual_risk_free: float = 0.0,
    risk_free_daily: pd.Series | None = None,
    sessions_per_year: int = 252,
    hac_lags: int = 5,
    minimum_regression_days: int = 30,
) -> dict:
    """Do not silently intersect dates, forward-fill returns, or sum returns."""
    p, b = _returns(portfolio, "portfolio"), _returns(benchmark, "benchmark")
    if not p.index.equals(b.index):
        raise ValueError("Portfolio and benchmark dates must match exactly")
    if sessions_per_year <= 0 or hac_lags < 0 or not math.isfinite(annual_risk_free) or annual_risk_free <= -1:
        raise ValueError("Invalid annualization, HAC or risk-free assumption")
    if minimum_regression_days < 3:
        raise ValueError("Regression requires at least three observations")
    if risk_free_daily is None:
        rf = np.full(len(p), math.expm1(math.log1p(annual_risk_free) / sessions_per_year))
        rf_basis = f"assumed_constant_annual_{annual_risk_free:g}"
    else:
        risk_free_daily = _returns(risk_free_daily, "risk_free_daily")
        if not p.index.equals(risk_free_daily.index):
            raise ValueError("Risk-free dates must match exactly")
        rf = risk_free_daily.to_numpy()
        rf_basis = "provided_daily_series"
    pv = np.cumprod(1 + p.to_numpy())
    bv = np.cumprod(1 + b.to_numpy())
    active = p.to_numpy() - b.to_numpy()
    wealth = np.r_[1., pv]
    peak = np.maximum.accumulate(wealth)
    tracking = float(np.std(active, ddof=1)) if len(active) > 1 else None
    result = {
        "start_date": str(p.index[0]), "end_date": str(p.index[-1]), "sessions": len(p),
        "total_return": float(pv[-1] - 1), "benchmark_return": float(bv[-1] - 1),
        "cumulative_excess_return": float(pv[-1] - bv[-1]),
        "relative_wealth_return": float(pv[-1] / bv[-1] - 1),
        "maximum_drawdown": float(np.max(1 - wealth / peak)),
        "annual_tracking_error": tracking * math.sqrt(sessions_per_year) if tracking is not None else None,
        "information_ratio": float(np.mean(active) / tracking * math.sqrt(sessions_per_year)) if tracking and tracking > 1e-15 else None,
        "risk_free_basis": rf_basis, "annualization_sessions": sessions_per_year,
        "alpha_daily": None, "alpha_annual_linear": None, "beta": None, "r_squared": None,
        "alpha_hac_se": None, "alpha_ci95_low": None, "alpha_ci95_high": None,
        "alpha_pvalue": None, "hac_lags": min(hac_lags, len(p) - 1),
        "regression_status": "insufficient_dates",
    }
    if len(p) < minimum_regression_days:
        return result
    x, y = b.to_numpy() - rf, p.to_numpy() - rf
    if float(np.var(x)) <= 1e-20:
        result["regression_status"] = "constant_benchmark"
        return result
    design = np.column_stack([np.ones(len(x)), x])
    coef = np.linalg.lstsq(design, y, rcond=None)[0]
    residual = y - design @ coef
    bread = np.linalg.inv(design.T @ design)
    scores = design * residual[:, None]
    meat = scores.T @ scores
    lag_count = result["hac_lags"]
    for lag in range(1, lag_count + 1):
        gamma = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / (lag_count + 1)) * (gamma + gamma.T)
    # Same finite-sample correction as a two-parameter HAC regression.
    covariance = bread @ meat @ bread * len(x) / (len(x) - 2)
    se = math.sqrt(max(0., float(covariance[0, 0])))
    alpha = float(coef[0])
    pvalue = math.erfc(abs(alpha / se) / math.sqrt(2)) if se > 1e-15 else (0. if abs(alpha) > 1e-15 else 1.)
    total_ss = float(np.square(y - y.mean()).sum())
    result.update(
        alpha_daily=alpha, alpha_annual_linear=alpha * sessions_per_year, beta=float(coef[1]),
        r_squared=1 - float(residual @ residual) / total_ss if total_ss > 1e-20 else None,
        alpha_hac_se=se, alpha_ci95_low=alpha - 1.959963984540054 * se,
        alpha_ci95_high=alpha + 1.959963984540054 * se, alpha_pvalue=pvalue,
        regression_status="ok_exploratory",
    )
    return result


def benjamini_hochberg(pvalues: Sequence[float | None]) -> list[float | None]:
    """Keep absent tests absent; adjust all finite tests, including both tails."""
    out = [None] * len(pvalues)
    valid = [(i, float(p)) for i, p in enumerate(pvalues) if p is not None and math.isfinite(p)]
    if any(not 0 <= p <= 1 for _, p in valid):
        raise ValueError("p-values must be in [0,1]")
    ordered = sorted(valid, key=lambda x: x[1])
    minimum = 1.
    for rank in range(len(ordered), 0, -1):
        i, value = ordered[rank - 1]
        minimum = min(minimum, value * len(ordered) / rank)
        out[i] = minimum
    return out
