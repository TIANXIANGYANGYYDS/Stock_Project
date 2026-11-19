"""Restore one historical price basis using only previous-day calibration.

THS historical qfq may subtract cash dividends, not merely multiply prices.
Reject nonuniform mappings rather than mixing historical and actual units.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math

from app.quant.runtime.live import LiveThreeMinuteBar, three_minute_bar_ends


def restore_historical_day(*, rows, previous_native, previous_target, current_target, trade_date):
    fields=('open','high','low','close')
    if not previous_native or not previous_target or not current_target:
        return (), {'method':'missing_price_basis_reference'}
    try:
        source={k:float(previous_native[k]) for k in fields}
        target={k:float(previous_target[k]) for k in fields}
        today={k:float(current_target[k]) for k in fields}
    except (TypeError,KeyError,ValueError):
        return (), {'method':'missing_price_basis_fields'}
    if any(not math.isfinite(v) or v<=0 for r in (source,target,today) for v in r.values()):
        return (), {'method':'invalid_price_basis'}
    # Candidate transformations and their selection use only t-1 OHLC.
    candidates=[(1.,target['close']-source['close']),
                (target['close']/source['close'],0.)]
    if source['high']-source['low']>.02:
        scale=(target['high']-target['low'])/(source['high']-source['low'])
        candidates.append((scale,target['close']-scale*source['close']))
    calibration=next(((a,b) for a,b in candidates if a>0 and
                      max(abs(a*source[k]+b-target[k]) for k in fields)<=.015),None)
    if calibration is None:
        return (), {'method':'previous_day_basis_not_affine'}
    scale,offset=calibration
    ordered=sorted(rows,key=lambda r:r['timestamp'])
    if [r['timestamp'] for r in ordered]!=list(three_minute_bar_ends(trade_date)):
        return (), {'method':'historical_grid_incomplete'}
    transformed=[{k:scale*float(r[k])+offset for k in fields} for r in ordered]
    if any(any(not math.isfinite(v) or v<=0 for v in r.values()) or
           r['high']<max(r['open'],r['close'])-1e-10 or r['low']>min(r['open'],r['close'])+1e-10 for r in transformed):
        return (), {'method':'invalid_transformed_ohlc'}
    aggregate=dict(open=transformed[0]['open'],close=transformed[-1]['close'],
                   high=max(r['high'] for r in transformed),low=min(r['low'] for r in transformed))
    error=max(abs(aggregate[k]-today[k]) for k in fields)
    if error>.02:
        return (), {'method':'current_day_basis_disagrees','ohlc_error':error,'scale':scale,'offset':offset}
    reference=target['close']
    pct=current_target.get('pct_chg')
    if pct is None or abs((today['close']/reference-1)*100-float(pct))>.015+1.1/reference:
        return (), {'method':'daily_reference_return_disagrees','scale':scale,'offset':offset}
    bars=tuple(LiveThreeMinuteBar(
        start_at=(datetime.fromisoformat(r['timestamp'])-timedelta(minutes=3)).isoformat(),
        end_at=r['timestamp'],previous_close=reference,**values) for r,values in zip(ordered,transformed))
    return bars, {'method':'prior_day_affine_basis_verified','scale':scale,'offset':offset,'ohlc_error':error}
