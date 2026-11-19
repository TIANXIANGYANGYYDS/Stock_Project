"""Strategy-1 shadow account: shared cash, causal ranking, bar-volume fills.

Research only. Signal generation and E2 reuse the strategy's frozen formulas;
no connection to a broker or mutation of the production independent accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from app.quant.core.execution import money
from app.quant.research.adx_exit import ExitController, ExitVariant
from app.quant.research.factors import FactorSnapshot
from app.quant.runtime.daily_flow import daily_price_limit
from app.quant.strategies.provisional_daily_macd_3m.strategy import confirm_provisional_histogram


POLICIES = (
    ('fifo', '原信号先到先买（同刻代码升序）'),
    ('low_vol', '20日低波动优先'),
    ('momentum', '20日相对强势优先'),
    ('adx', 'ADX14增量优先'),
    ('flow', '5日主力强度优先'),
    ('balanced', '低波动＋相对强势＋ADX等权'),
)
FACTOR_COLUMNS = ('volatility_20', 'relative_return20', 'main_strength5')


def provisional(item: dict, bar: dict) -> tuple[float, float, float] | None:
    state = item.get('previous_state')
    if not state or not bar.get('previous_close') or not item.get('previous_close'):
        return None
    scale = bar['previous_close'] / item['previous_close']
    fast, slow, dea = (state[k] * scale for k in ('fast_ema', 'slow_ema', 'dea'))
    dif = fast + 2 / 21 * (bar['close'] - fast) - (slow + 2 / 101 * (bar['close'] - slow))
    hist = 2 * (dif - (dea + 2 / 31 * (dif - dea)))
    return dif, hist, item['reference_histogram'] * scale


def first_buy(item: dict, bars: list[dict]) -> dict | None:
    a, b = item.get('adx'), item.get('adx_3_days_ago')
    if item.get('action') != 'buy' or a is None or b is None or not (a >= 20 and a > b):
        return None
    count = 0
    for bar in bars:
        values = provisional(item, bar)
        if values is None:
            return None
        dif, hist, ref = values
        qualifies, shrink = confirm_provisional_histogram(
            action='buy', reference_histogram=ref, provisional_histogram=hist)
        count = count + 1 if qualifies else 0
        if count >= 3:
            return dict(code=item['code'], name=item['name'], at=bar['end_at'],
                        price=bar['close'], factor_date=item['factor_completed_date'],
                        adx=a, adx_delta=a-b, shrink=shrink, dif=dif)
    return None


def score(policy: str, signal: dict, factors: dict) -> float | None:
    if policy == 'fifo':
        return 0.
    if policy == 'adx':
        return signal['adx_delta']
    keys = {'low_vol': ('volatility_20',), 'momentum': ('relative_return20',),
            'flow': ('main_strength5',),
            'balanced': ('volatility_20', 'relative_return20', 'adx_delta')}[policy]
    values = [factors.get(k + '_rank') for k in keys]
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    return sum(1-v if k == 'volatility_20' else v for k, v in zip(keys, values)) / len(keys)


@dataclass(frozen=True)
class AccountRules:
    initial_cash: float = 200_000.
    max_positions: int = 5
    fraction: float = .2
    commission: float = .0001
    minimum_commission: float = 5.
    transfer: float = .00001
    stamp: float = .0005
    slippage: float = .0005
    participation: float = .05


def lot_rule(code: str) -> tuple[int, int]:
    if code.startswith(('688', '689')):
        return 200, 1
    if code.startswith(('4', '8', '92')):
        return 100, 1
    return 100, 100


def fees(notional: float, action: str, rules: AccountRules) -> float:
    if notional <= 0:
        return 0.
    return money(max(rules.minimum_commission, money(notional * rules.commission))
                 + money(notional * rules.transfer)
                 + (money(notional * rules.stamp) if action == 'sell' else 0.))


def match(order: dict, bar: dict, *, rules: AccountRules, shares: int = 0,
          entry_date: str = '') -> dict:
    """A budget IOC buy; a sell intent can be retried on later bars/days.

    Volume is used only to cap simulated execution over this *completed* bar.
    Report the whole execution window, never pretend it is a tick/queue fill.
    """
    action, code = order['action'], order['code']
    day = bar['start_at'][:10]
    if action == 'sell' and day <= entry_date:
        return {'status': 't_plus_one'}
    if not bar.get('previous_close'):
        return {'status': 'missing_reference'}
    volume = bar.get('volume')
    if volume is None or not math.isfinite(volume) or volume <= 0:
        return {'status': 'missing_or_zero_volume'}
    upper = daily_price_limit(action='buy', code=code, name=order['name'], trade_date=day,
                              previous_close=bar['previous_close'])
    lower = daily_price_limit(action='sell', code=code, name=order['name'], trade_date=day,
                              previous_close=bar['previous_close'])
    if (action == 'buy' and bar['open'] >= upper-.000001) or (action == 'sell' and bar['open'] <= lower+.000001):
        return {'status': 'price_limit'}
    raw = bar['open'] * (1 + rules.slippage if action == 'buy' else 1 - rules.slippage)
    price = (math.ceil(raw * 100 - 1e-8) if action == 'buy' else math.floor(raw * 100 + 1e-8)) / 100
    if not (max(lower, bar['low'])-.000001 <= price <= min(upper, bar['high'])+.000001):
        return {'status': 'slipped_price_outside_bar'}
    minimum, step = lot_rule(code)
    capacity = math.floor(volume * rules.participation + 1e-9)
    if action == 'buy':
        quantity = int(order['budget'] / price) // step * step
        while quantity >= minimum and money(quantity*price) + fees(money(quantity*price), action, rules) > order['budget']:
            quantity -= step
        if quantity < minimum:
            return {'status': 'unaffordable_lot'}
        requested = quantity
        quantity = min(quantity, capacity // step * step)
        if quantity < minimum:
            return {'status': 'insufficient_volume'}
        cost = fees(money(price*quantity), action, rules)
    else:
        requested = shares
        quantity = shares if capacity >= shares else capacity // step * step
        if quantity <= 0:
            return {'status': 'insufficient_volume'}
        # Partial executions of one daily order share its minimum commission.
        before = order.get('filled_notional_today', 0.)
        cost = money(fees(money(before + price*quantity), action, rules) - fees(before, action, rules))
    return dict(status='filled', shares=quantity, requested=requested, price=price,
                notional=money(price*quantity), fees=cost, volume=volume,
                participation=quantity/volume, reference_open=bar['open'],
                execution_start=bar['start_at'], execution_end=bar['end_at'],
                source=bar.get('source'), partial=quantity < requested)


class SharedAccount:
    def __init__(self, policy: str, rules: AccountRules = AccountRules()):
        self.policy, self.rules = policy, rules
        self.cash = rules.initial_cash
        self.positions, self.pending = {}, {}
        self.orders, self.attempts, self.fills, self.decisions = [], [], [], []
        self.trades, self.curve, self.holdings, self.exit_events, self.watchlists = [], [], [], [], []
        self.controllers = {}
        self.realized = 0.
        self.max_slots = 0

    def assets(self) -> float:
        return money(self.cash + sum(p['shares']*p['mark'] for p in self.positions.values()))

    def submit(self, code, name, action, at, **fields):
        order = dict(order_id=f'{self.policy}-{len(self.orders)+1:05}', code=code, name=name,
                     action=action, signal_at=at, status='pending', **fields)
        self.orders.append(order)
        self.pending[code] = order
        return order

    def select(self, signals: list[dict], factors: dict, at: str, allowed_codes=None):
        scored = [(s, score(self.policy, s, factors.get((s['code'], s['factor_date']), {}))) for s in signals]
        scored.sort(key=lambda x: (x[1] is None, -(x[1] or 0.), x[0]['code']))
        decision_nav = self.assets()
        for rank, (s, value) in enumerate(scored, 1):
            code = s['code']
            reserved = sum(o['budget'] for o in self.pending.values() if o['action']=='buy')
            slots = len(set(self.positions) | set(self.pending))
            budget = money(min(decision_nav*self.rules.fraction, max(0., self.cash-reserved)))
            if code in self.positions or code in self.pending:
                reason = 'already_held_or_pending'
            elif allowed_codes is not None and code not in allowed_codes:
                reason = 'outside_preopen_top5'
            elif slots >= self.rules.max_positions:
                reason = 'five_slots_occupied'
            elif value is None:
                reason = 'missing_ranking_factor'
            elif budget <= self.rules.minimum_commission:
                reason = 'no_available_cash'
            else:
                reason = 'selected'
                self.submit(code, s['name'], 'buy', at, budget=budget,
                            decision_nav=decision_nav, score=value, rank=rank,
                            factor_date=s['factor_date'], reason='original_macd_adx')
            self.decisions.append(dict(**s, policy=self.policy, score=value, rank=rank,
                                       decision=reason, budget=budget, decision_nav=decision_nav,
                                       factor_values=factors.get((code, s['factor_date']), {})))
        self.check()

    def execute(self, code: str, bar: dict):
        order = self.pending[code]
        if bar['start_at'] < order['signal_at']:
            raise AssertionError('Cannot fill a signal before its next bar')
        p = self.positions.get(code, {})
        if order.get('execution_day') != bar['start_at'][:10]:
            order['execution_day'] = bar['start_at'][:10]
            order['filled_notional_today'] = 0.
        result = match(order, bar, rules=self.rules, shares=p.get('shares', 0), entry_date=p.get('entry_at','')[:10])
        self.attempts.append(dict(order_id=order['order_id'], code=code, action=order['action'],
                                  at=bar['end_at'], **result))
        if result['status'] != 'filled':
            if order['action'] == 'buy':
                order['status'] = result['status']
                self.pending.pop(code)
            return
        r = result
        order['filled_notional_today'] = money(order['filled_notional_today'] + r['notional'])
        if order['action'] == 'buy':
            self.cash = money(self.cash - r['notional'] - r['fees'])
            self.positions[code] = dict(code=code, name=order['name'], shares=r['shares'],
                original_shares=r['shares'], entry_at=r['execution_end'], entry_price=r['price'],
                entry_cost=money(r['notional']+r['fees']), remaining_cost=money(r['notional']+r['fees']),
                mark=r['price'], mark_at=r['execution_end'], buy_order_id=order['order_id'], realized=0.)
            self.controllers[code] = ExitController(ExitVariant(14, 'E2'), {}, state='HOLDING')
            order['status'] = 'partial_filled_cancelled' if r['partial'] else 'filled'
            self.pending.pop(code)
        else:
            self.cash = money(self.cash + r['notional'] - r['fees'])
            allocated = p['remaining_cost'] if r['shares']==p['shares'] else money(p['remaining_cost']*r['shares']/p['shares'])
            pnl = money(r['notional']-r['fees']-allocated)
            self.realized = money(self.realized+pnl)
            p['realized'] = money(p['realized']+pnl)
            p['remaining_cost'] = money(p['remaining_cost']-allocated)
            p['shares'] -= r['shares']
            if not p['shares']:
                self.trades.append(dict(code=code, name=p['name'], entry_at=p['entry_at'],
                    exit_at=r['execution_end'], entry_cost=p['entry_cost'], pnl=p['realized'],
                    return_net=p['realized']/p['entry_cost'], buy_order_id=p['buy_order_id'],
                    sell_order_id=order['order_id'], exit_reason=order['reason']))
                self.positions.pop(code)
                self.controllers.pop(code)
                self.pending.pop(code)
                order['status'] = 'filled'
        self.fills.append(dict(order_id=order['order_id'], code=code, name=order['name'],
            action=order['action'], signal_at=order['signal_at'], **r, cash_after=self.cash,
            position_count=len(self.positions)))
        self.check()

    def evaluate_exit(self, code: str, item: dict, bar: dict, consecutive: int) -> int:
        p = self.positions[code]
        if code in self.pending:
            return consecutive
        values = provisional(item, bar)
        dif, hist, ref = values if values else (None, None, None)
        original = False
        if item.get('action') == 'sell' and p['entry_at'][:10] < bar['end_at'][:10] and values:
            qualifies, _ = confirm_provisional_histogram(action='sell', reference_histogram=ref, provisional_histogram=hist)
            consecutive = consecutive+1 if qualifies else 0
            original = consecutive >= 3
        controller = self.controllers[code]
        day = bar['end_at'][:10]
        controller.snapshots = {day: FactorSnapshot(day, item.get('factor_completed_date', ''),
            {'adx_14':item.get('adx'), 'adx_14_3_days_ago':item.get('adx_3_days_ago')})}
        notional = money(math.floor(bar['close']*(1-self.rules.slippage)*100+1e-8)/100*p['shares'])
        net = money(notional-fees(notional, 'sell', self.rules))
        decision = controller.decide(at=bar['end_at'],
            quote={'estimated_net_return':(net-p['remaining_cost'])/p['remaining_cost'],
                   'quote_net_proceeds':net, 'entry_cost':p['remaining_cost']},
            dif=dif, histogram=hist, original_sell=original, entry_at=p['entry_at'])
        controller.rows.clear()
        if decision['action'] in ('submit', 'defer'):
            self.exit_events.append(dict(code=code, **decision))
        if decision['action']=='submit':
            self.submit(code, p['name'], 'sell', bar['end_at'], reason=decision['reason'])
        return consecutive

    def check(self):
        slots = len(set(self.positions) | set(self.pending))
        self.max_slots = max(self.max_slots, slots)
        assert self.cash >= -.001 and slots <= self.rules.max_positions
        assert sum(o['budget'] for o in self.pending.values() if o['action']=='buy') <= self.cash+.01
        pnl = self.realized + sum(p['shares']*p['mark']-p['remaining_cost'] for p in self.positions.values())
        assert abs(self.assets()-self.rules.initial_cash-pnl) <= .03

    def close_day(self, day):
        self.check()
        market = money(sum(p['shares']*p['mark'] for p in self.positions.values()))
        stale = money(sum(p['shares']*p['mark'] for p in self.positions.values() if p['mark_at'][:10]!=day))
        self.curve.append(dict(trade_date=day, assets=self.assets(), cash=self.cash,
            market_value=market, realized_pnl=self.realized, unrealized_pnl=money(self.assets()-self.rules.initial_cash-self.realized),
            holding_count=len(self.positions), stale_market_value=stale))
        for p in self.positions.values():
            self.holdings.append(dict(trade_date=day, **p, market_value=money(p['shares']*p['mark']),
                                      weight=p['shares']*p['mark']/self.assets()))
