"""Fixed candidate gates and continuous, independent strategy-1 account paths."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, replace
import math

import pandas as pd

from app.quant.core.indicators import calculate_macd
from app.quant.core.models import Bar
from app.quant.research.factors import FactorBar, _adx, _true_ranges
from app.quant.runtime.daily_flow import (
    IndependentAccount, PreselectionItem, SellCandidateItem, create_daily_flow,
)
from app.quant.runtime.daily_macd import calculate_daily_macd_states
from app.quant.runtime.live import LiveObservationSpec, LiveThreeMinuteBar, replay_live_day
from app.quant.strategies.provisional_daily_macd_3m import determine_observation_action, official_backtest_config
from app.quant.strategies.provisional_daily_macd_3m.adx import ADX_HISTORY_START


FILTERS = (
    ('baseline', '原策略1', ()),
    ('vol20_q20', '20日波动率最低20%', (('volatility_20', .2),)),
    ('vol20_q40', '20日波动率最低40%', (('volatility_20', .4),)),
    ('vol60_q20', '60日波动率最低20%', (('volatility_60', .2),)),
    ('vol60_q40', '60日波动率最低40%', (('volatility_60', .4),)),
    ('turn20_q20', '20日平均换手最低20%', (('turnover_20', .2),)),
    ('turn20_q40', '20日平均换手最低40%', (('turnover_20', .4),)),
    ('vol60_turn20_q40', '60日波动和20日换手均最低40%', (('volatility_60', .4), ('turnover_20', .4))),
)
FACTOR_KEYS = ('volatility_20', 'volatility_60', 'turnover_20')


def rank_inputs(panel: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    """Rank on the previous completed date, preserving ties and missing data."""
    x = panel[panel.code.isin(codes)].copy()
    if x[['code', 'trade_date']].duplicated().any():
        raise ValueError('Duplicate factor keys')
    eligible = x.normalized_close.notna() & x.volume.gt(0)
    for key in FACTOR_KEYS:
        valid = eligible & x[key].notna()
        count = valid.groupby(x.trade_date).transform('sum')
        denominator = eligible.groupby(x.trade_date).transform('sum')
        x[key + '_rank'] = x[key].where(valid).groupby(x.trade_date).rank(method='average', pct=True)
        x.loc[count.lt(.8 * denominator) | denominator.lt(20), key + '_rank'] = float('nan')
    return x[['code', 'trade_date', *FACTOR_KEYS, *[k+'_rank' for k in FACTOR_KEYS]]]


def gate_result(rules: tuple, values: dict) -> tuple[bool, str]:
    for key, threshold in rules:
        rank = values.get(key + '_rank')
        if rank is None or not math.isfinite(rank):
            return False, 'missing_factor:' + key
        if rank > threshold:
            return False, 'threshold:' + key
    return True, 'accepted'


def observation_inputs(daily: list[dict], calendar: list[str], dates: list[str], code: str) -> dict[str, dict]:
    """Match the live service's full-history MACD and fixed-start ADX seeds."""
    rows = sorted(daily, key=lambda r: r['trade_date'])
    rows = [r for r in rows if all(math.isfinite(float(r[k])) and float(r[k]) > 0 for k in ('open','high','low','close'))
            and r['high'] >= max(r['open'], r['close']) and r['low'] <= min(r['open'], r['close'])]
    if not rows:
        return {}
    bars = [Bar(r['trade_date'], r['open'], r['high'], r['low'], r['close']) for r in rows]
    config = official_backtest_config(code=code)
    indicators = calculate_macd(bars, config)
    states = calculate_daily_macd_states(bars, config)
    history = [FactorBar(b.trade_date, b.high, b.low, b.close, 0.) for b in bars if b.trade_date >= ADX_HISTORY_START]
    adx = dict(zip([b.trade_date for b in history], _adx(history, 14, _true_ranges(history))))
    position = {day: i for i, day in enumerate(calendar)}
    days = [b.trade_date for b in bars]
    result = {}
    for day in dates:
        i = position[day]
        if i < 4:
            raise ValueError('Insufficient calendar warmup')
        previous, comparison = calendar[i-1], calendar[i-4]
        j = bisect_right(days, previous)-1
        if j < 0:
            continue
        stale = days[j] != previous
        enough = j+1 >= config.warmup_bars
        action = determine_observation_action(indicators[j-1], indicators[j]) if enough and not stale else None
        result[day] = dict(
            code=code, name=rows[j].get('name') or code, action=action,
            observation_before_date=days[j-1] if j else previous,
            observation_date=previous, previous_close=bars[j].close,
            reference_histogram=indicators[j].histogram if enough else 0.,
            previous_state=states[j] if enough and not stale else None,
            adx=adx.get(previous), adx_3_days_ago=adx.get(comparison),
            factor_completed_date=previous, factor_comparison_date=comparison,
        )
    return result


def could_buy(item: dict) -> bool:
    a, b = item.get('adx'), item.get('adx_3_days_ago')
    return item.get('action') == 'buy' and a is not None and b is not None and a >= 20 and a > b


def replay_paths(*, code: str, dates: list[str], inputs: dict[str, dict],
                 bars: dict[str, tuple[LiveThreeMinuteBar, ...]], ranks: dict[str, dict]) -> dict:
    """Each candidate replays its own cash, holdings, pending buys and E2 exits.

    Returns compact curves (assets, cash, market value, realized PNL, stale
    market value), plus actual signals/trades. No future-return trade deletion.
    """
    output = {}
    for mode, multiplier in [('normal', 1), ('double_cost', 2)]:
        for key, label, rules in FILTERS:
            account = IndependentAccount(code, code)
            holdings, pending, exits = (), (), {}
            curve, signals, trades, events = [], [], [], []
            incomplete_held_days = 0
            for day in dates:
                item = inputs.get(day)
                action = item['action'] if item else None
                if holdings:
                    action = 'sell' if action == 'sell' and not pending else 'hold'
                elif action != 'buy' or pending:
                    action = None
                # Avoid replaying a data-independent no-trade account day.
                # ADX-rejected buy watches are retained for signal diagnostics.
                if action is None and not pending:
                    curve.append([account.cash, account.cash, 0., account.realized_pnl, 0.])
                    continue
                if item:
                    fields = dict(item, action=action or 'hold')
                else:
                    previous = day  # held without a daily state: E2 sees missing ADX
                    fields = dict(code=code, name=account.name, action='hold', observation_before_date=previous,
                                  observation_date=previous, previous_close=holdings[0].mark_price,
                                  reference_histogram=0., previous_state=None)
                spec = LiveObservationSpec(**fields)
                candidates = [PreselectionItem(code, spec.name, '原MACD买入观察', spec.previous_close)] if action == 'buy' else []
                sells = [SellCandidateItem(code, spec.name, '原MACD卖出观察', spec.previous_close)] if action == 'sell' else []
                # The live service carries submitted orders into the candidate
                # lists even when today's MACD observation no longer qualifies.
                if pending and pending[0]['action'] == 'buy' and not candidates:
                    candidates = [PreselectionItem(code, spec.name, '跨日待成交买入', spec.previous_close)]
                if pending and pending[0]['action'] == 'sell' and not sells:
                    sells = [SellCandidateItem(code, spec.name, '跨日待成交卖出', spec.previous_close)]
                flow = create_daily_flow(trade_date=day, selection_date=spec.observation_date,
                    generated_at=day+'T09:20:00+08:00', candidates=candidates, sell_candidates=sells,
                    holdings=holdings, accounts=[account])
                cfg = flow.config
                if multiplier != 1:
                    flow = replace(flow, config=replace(cfg, commission_rate=cfg.commission_rate*multiplier,
                        stamp_duty_rate=cfg.stamp_duty_rate*multiplier, slippage_rate=cfg.slippage_rate*multiplier))
                prior_values = ranks.get(spec.factor_completed_date, {})
                callback = (lambda c, d, values=prior_values: gate_result(rules, values)) if rules else None
                result = replay_live_day(opening_flow=flow, observation_specs=[spec], opening_pending_signals=pending,
                    opening_exit_states=exits, bars_by_code={code: bars.get(day, ())},
                    expected_bar_count=80, close_market=True, research_buy_gate=callback)
                flow = result['flow']; account = flow.accounts[0]
                holdings, pending, exits = flow.holdings, result['pending_signals'], result['exit_states']
                market = sum(round(h.shares*h.mark_price, 2) for h in holdings)
                stale = sum(round(h.shares*h.mark_price, 2) for h in holdings if h.marked_at < day+'T15:00:00+08:00')
                incomplete_held_days += int(bool(holdings) and code in result['incomplete_codes'])
                curve.append([round(account.cash+market, 2), account.cash, market, account.realized_pnl, stale])
                signals.extend(dict(s) for s in result['signals'])
                trades.extend(asdict(t) for t in flow.closed_trades)
                events.extend(asdict(e) for e in flow.executions)
            # A pending signal may occur on several dates; preserve its final
            # state and one unique identity, not a new signal for each retry.
            signals = list({s['signal_id']: s for s in signals}.values())
            closed_entries = {t['entry_event_id'] for t in trades}
            for t in trades:
                t.update(outcome='closed', asof_return=t['net_return'])
            for h in holdings:
                cost = h.entry_notional+h.buy_commission
                trades.append(dict(code=code, name=h.name, entry_event_id=h.entry_event_id,
                    entry_signal_at=h.entry_signal_at, entry_execution_at=h.entry_execution_at,
                    entry_execution_price=h.entry_execution_price, entry_notional=h.entry_notional,
                    buy_commission=h.buy_commission, shares=h.shares, exit_execution_at=None,
                    outcome='open', net_pnl=None, net_return=None,
                    asof_return=(h.shares*h.mark_price-cost)/cost,
                    unrealized_pnl=h.shares*h.mark_price-cost, mark_price=h.mark_price, marked_at=h.marked_at))
            assert len(trades) == sum(e['action']=='buy' for e in events)
            assert len(closed_entries) == sum(e['action']=='sell' for e in events)
            output[f'{mode}::{key}'] = dict(curve=curve, signals=signals, trades=trades,
                events=events, incomplete_held_days=incomplete_held_days)
    return output
