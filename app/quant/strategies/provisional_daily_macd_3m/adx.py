"""正式ADX14买入门控；复用已验证的Wilder实现和E2退出状态机。"""
from __future__ import annotations

from typing import Sequence

from app.quant.core.models import Bar
from app.quant.research.adx_exit import ExitController, ExitVariant, adx_state, liquidation_quote
from app.quant.research.factors import FactorBar, FactorSnapshot, _adx, _true_ranges

# 延续ADX退出研究的固定预热起点，逐日追加，不能每天重新截断种子。
ADX_HISTORY_START = '2025-11-05'
ADX_PERIOD = 14
from .config import RECORDING_START_DATE

LIVE_RECORDING_START = RECORDING_START_DATE


def daily_adx_snapshot(*, bars: Sequence[Bar], trade_date: str,
                       completed_date: str, comparison_date: str) -> FactorSnapshot:
    """只用t-1及以前完整日线；比较端点固定为市场交易日t-4。"""
    history = [FactorBar(b.trade_date, b.high, b.low, b.close, 0.) for b in bars
               if ADX_HISTORY_START <= b.trade_date <= completed_date]
    values = _adx(history, ADX_PERIOD, _true_ranges(history))
    by_date = {b.trade_date: value for b, value in zip(history, values)}
    return FactorSnapshot(trade_date, completed_date, {
        'adx_14': by_date.get(completed_date),
        'adx_14_3_days_ago': by_date.get(comparison_date),
    })


def buy_allowed(snapshot: FactorSnapshot | None) -> bool:
    return adx_state(snapshot, ADX_PERIOD)[0] == 'strong'


def e2_controller(snapshot: FactorSnapshot | None, *, state: str = 'HOLDING',
                  deferred_from: str | None = None) -> ExitController:
    snapshots = {snapshot.signal_date: snapshot} if snapshot else {}
    return ExitController(ExitVariant(ADX_PERIOD, 'E2'), snapshots,
                          state=state, deferred_from=deferred_from)
