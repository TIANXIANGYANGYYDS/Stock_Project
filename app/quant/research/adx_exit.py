"""Frozen ADX exit state machine, reused by the official ADX14/E2 adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from app.quant.core.execution import money
from app.quant.core.models import BacktestConfig, Bar
from app.quant.research.factors import FactorSnapshot


@dataclass(frozen=True)
class ExitVariant:
    period: int | None
    version: str

    @property
    def key(self) -> str:
        return 'baseline' if self.period is None else f'adx{self.period}_{self.version}'

    @property
    def early(self) -> bool:
        return self.version in ('E1', 'E3')

    @property
    def delay(self) -> bool:
        return self.version in ('E2', 'E3')


EXIT_GRID = (ExitVariant(None, 'E0'),) + tuple(
    ExitVariant(n, e) for n in (14, 21) for e in ('E0', 'E1', 'E2', 'E3')
)


def adx_state(snapshot: FactorSnapshot | None, period: int) -> tuple[str, float | None, float | None]:
    a = snapshot.value(f'adx_{period}') if snapshot else None
    b = snapshot.value(f'adx_{period}_3_days_ago') if snapshot else None
    if a is None or b is None or not isfinite(a) or not isfinite(b):
        return 'missing', a, b
    if a >= 20 and a > b:
        return 'strong', a, b
    if a < 20 or a < b:
        return 'weak', a, b
    return 'neutral', a, b


def liquidation_quote(position: dict[str, Any], bar: Bar, config: BacktestConfig) -> dict[str, float]:
    """Quote at this completed bar's close, using only its already-known range.

    The same deterministic slippage/clamping and per-leg rounding as execution;
    this quote never fills an order and never touches a random stream.
    """
    price = min(bar.high, max(bar.low, bar.close * (1 - config.slippage_rate)))
    notional = money(price * position['shares'])
    commission = money(notional * config.commission_rate)
    stamp = money(notional * config.stamp_duty_rate)
    proceeds = money(notional - commission - stamp)
    cost = money(position['entry_notional'] + position['buy_commission'])
    return {'quote_price': price, 'quote_net_proceeds': proceeds,
            'entry_cost': cost, 'estimated_net_return': (proceeds - cost) / cost}


@dataclass
class ExitController:
    variant: ExitVariant
    snapshots: dict[str, FactorSnapshot]
    state: str = 'FLAT'
    deferred_from: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def on_fill(self, action: str) -> None:
        self.state = 'HOLDING' if action == 'buy' else 'FLAT'
        self.deferred_from = None

    def decide(self, *, at: str, quote: dict[str, float], dif: float | None,
               histogram: float | None, original_sell: bool,
               entry_at: str) -> dict[str, Any]:
        """Priority: pending > deferred > original signal > early protection."""
        snapshot = self.snapshots.get(at[:10])
        strength, a, b = adx_state(snapshot, int(self.variant.period))
        valid_macd = all(x is not None and isfinite(x) for x in (dif, histogram))
        profitable = quote['estimated_net_return'] > 0
        allow = strength == 'strong' and profitable and valid_macd and dif > 0 and histogram > 0
        before = self.state
        action, reason = 'hold', 'holding'
        if before == 'EXIT_PENDING':
            return {'action': 'pending', 'reason': 'pending_intent_immutable'}
        if before == 'DEFERRED_EXIT':
            if not allow:
                action, reason = 'submit', 'deferred_invalid'
        elif original_sell:
            if self.variant.delay and allow:
                action, reason = 'defer', 'original_sell_deferred'
                self.state = 'DEFERRED_EXIT'
                self.deferred_from = at
            else:
                action, reason = 'submit', 'original_macd'
        elif self.variant.early and strength == 'weak' and profitable:
            action, reason = 'submit', 'early_protection'
        if action == 'submit':
            self.state = 'EXIT_PENDING'
        row = {'at': at, 'entry_at': entry_at, 'state_before': before,
               'state_after': self.state, 'action': action, 'reason': reason,
               'original_sell': original_sell, 'deferred_from': self.deferred_from,
               'adx_state': strength, 'adx': a, 'adx_3_days_ago': b,
               'factor_completed_date': snapshot.completed_date if snapshot else None,
               'provisional_dif': dif, 'provisional_histogram': histogram,
               'allow_delay': bool(allow), 'data_anomaly': strength == 'missing' or not valid_macd,
               **quote}
        # Audit every held bar, including hold/deferred continuation and missing data.
        self.rows.append(row)
        return row
