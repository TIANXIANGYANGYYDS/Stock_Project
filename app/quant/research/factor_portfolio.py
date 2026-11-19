"""Fixed-capital, long-only research portfolios with next-open execution.

Positions use fractional research units on return-normalized prices, not broker
lots. Costs, cash, delayed execution and stale marks are explicit. Missing data
never supplies a zero factor, a fabricated quote, or a successful execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PortfolioCosts:
    commission: float = .0001
    sell_stamp_duty: float = .0005
    slippage: float = .0005

    @property
    def buy(self) -> float:
        return self.commission + self.slippage

    @property
    def sell(self) -> float:
        return self.commission + self.sell_stamp_duty + self.slippage

    def __post_init__(self):
        if any(not np.isfinite(v) or v < 0 or v >= .1 for v in (self.commission, self.sell_stamp_duty, self.slippage)):
            raise ValueError("Invalid transaction cost")


def factor_groups(values: np.ndarray, eligible: np.ndarray, groups: int = 5) -> np.ndarray:
    """Keep equal values together; binary factors use the low/high groups.

    A cross-sectional constant is not a stock-selection signal and remains
    unassigned. Ranking by stock code to break ties would create fake signals.
    """
    if groups < 2 or values.shape != eligible.shape or values.ndim != 1:
        raise ValueError("Invalid factor grouping input")
    result = np.zeros(len(values), dtype=np.int8)
    good = eligible & np.isfinite(values)
    observed = values[good]
    unique = np.unique(observed)
    if len(unique) < 2:
        return result
    if len(unique) < groups:
        result[good] = 1 + np.rint(np.searchsorted(unique, observed) * (groups - 1) / (len(unique) - 1)).astype(np.int8)
    else:
        ranks = pd.Series(observed).rank(method="average", pct=True).to_numpy()
        result[good] = np.minimum(groups, np.ceil(ranks * groups)).astype(np.int8)
    return result


def target_schedule(
    factor_values: np.ndarray,
    eligible: np.ndarray,
    *,
    group: int,
    groups: int = 5,
    rebalance_sessions: int = 5,
    minimum_stocks: int = 20,
    minimum_coverage: float = .8,
) -> tuple[dict[int, np.ndarray], list[dict]]:
    """Row i is the factor at the previous session's close for execution on i."""
    if factor_values.shape != eligible.shape or factor_values.ndim != 2:
        raise ValueError("Factor and eligible matrices must match")
    if not 1 <= group <= groups or rebalance_sessions < 1 or minimum_stocks < 1 or not 0 <= minimum_coverage <= 1:
        raise ValueError("Invalid portfolio schedule")
    decisions, audit = {}, []
    for i in range(0, len(factor_values), rebalance_sessions):
        valid = eligible[i] & np.isfinite(factor_values[i])
        denominator = int(eligible[i].sum())
        coverage = valid.sum() / denominator if denominator else 0.
        buckets = factor_groups(factor_values[i], eligible[i], groups)
        selected = buckets == group
        usable = coverage >= minimum_coverage and selected.sum() >= minimum_stocks
        audit.append(dict(day_index=i, eligible_stocks=denominator, factor_stocks=int(valid.sum()),
                          coverage=float(coverage), selected_stocks=int(selected.sum()), usable=bool(usable)))
        if usable:
            decisions[i] = selected.astype(float) / selected.sum()
    return decisions, audit


def simulate_portfolio(
    *,
    dates: list[str],
    open_prices: np.ndarray,
    close_prices: np.ndarray,
    initial_marks: np.ndarray,
    can_buy: np.ndarray,
    can_sell: np.ndarray,
    targets: dict[int, np.ndarray],
    costs: PortfolioCosts = PortfolioCosts(),
) -> pd.DataFrame:
    """Mark every session, including cash-only days, on one fixed capital base."""
    shape = open_prices.shape
    if len(shape) != 2 or len(dates) != shape[0] or any(a.shape != shape for a in (close_prices, can_buy, can_sell)):
        raise ValueError("Price, execution and calendar dimensions disagree")
    if len(set(dates)) != len(dates) or dates != sorted(dates) or initial_marks.shape != (shape[1],):
        raise ValueError("Invalid dates or initial marks")
    for prices in (open_prices, close_prices, initial_marks):
        if ((prices <= 0) & np.isfinite(prices)).any() or np.isinf(prices).any():
            raise ValueError("Invalid research prices")
    if ((can_buy | can_sell) & ~np.isfinite(open_prices)).any():
        raise ValueError("Executable quote must have an open price")
    for i, weights in targets.items():
        if not 0 <= i < shape[0] or weights.shape != (shape[1],) or not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() > 1 + 1e-10:
            raise ValueError("Invalid long-only target")
    units = np.zeros(shape[1]); cash = 1.; previous_nav = 1.
    marks = initial_marks.copy()
    rows = []
    # Only positions bought on an earlier date are eligible for sale. The
    # schedule makes one target decision per day and never sells today's buy.
    for i, day in enumerate(dates):
        opening = np.where(np.isfinite(open_prices[i]), open_prices[i], marks)
        if ((units > 0) & ~np.isfinite(opening)).any():
            raise ValueError("Held asset has no prior mark")
        opening = np.nan_to_num(opening, nan=0.)
        opening_nav = cash + float(units @ opening)
        buy_value = sell_value = charge = 0.
        blocked_buy = blocked_sell = 0
        if i in targets:
            desired_value = targets[i] * opening_nav
            desired_units = np.divide(desired_value, opening, out=np.zeros_like(units), where=opening > 0)
            sells = np.maximum(units - desired_units, 0.)
            blocked_sell = int(((sells > 1e-12) & ~can_sell[i]).sum())
            sells[~can_sell[i]] = 0.
            sell_value = float(sells @ opening)
            units -= sells
            cash += sell_value * (1 - costs.sell)
            buys = np.maximum(desired_units - units, 0.)
            blocked_buy = int(((buys > 1e-12) & ~can_buy[i]).sum())
            buys[~can_buy[i]] = 0.
            requested = float(buys @ opening) * (1 + costs.buy)
            if requested > cash and requested:
                buys *= max(0., cash) / requested
            buy_value = float(buys @ opening)
            units += buys
            cash -= buy_value * (1 + costs.buy)
            charge = buy_value * costs.buy + sell_value * costs.sell
        if cash < -1e-10:
            raise AssertionError("Cash cannot be borrowed")
        cash = max(0., cash)
        seen = np.isfinite(close_prices[i])
        marks = np.where(seen, close_prices[i], marks)
        marked_values = units * np.nan_to_num(marks, nan=0.)
        nav = cash + float(marked_values.sum())
        if nav <= 0 or not np.isfinite(nav):
            raise ValueError("Invalid NAV")
        rows.append(dict(trade_date=day, nav=nav, daily_return=nav / previous_nav - 1,
                         cash_weight=cash / nav, holding_count=int((units > 1e-12).sum()),
                         cost=charge, traded_value=buy_value + sell_value,
                         turnover=(buy_value + sell_value) / opening_nav,
                         rebalance=i in targets, blocked_buys=blocked_buy, blocked_sells=blocked_sell,
                         stale_mark_weight=float(marked_values[~seen].sum() / nav)))
        previous_nav = nav
    return pd.DataFrame(rows)
