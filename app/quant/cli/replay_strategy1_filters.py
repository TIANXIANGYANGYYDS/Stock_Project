"""Continuous strategy-1 filter experiment; Mongo reads only, local artifacts.

Freeze candidates before replay. Reuse the production daily replay engine;
never reconstruct a candidate by deleting the original strategy's trades.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
from time import monotonic

import pandas as pd

from app.manually_execute_script.repair_realtime_minutes_ths import connect
from app.quant.data.historical_reference import recover_previous_close, validate_history_day
from app.quant.research.strategy1_filters import (
    FACTOR_KEYS, FILTERS, could_buy, observation_inputs, rank_inputs, replay_paths,
)
from app.quant.research.historical_price_basis import restore_historical_day
from app.quant.runtime.live import LiveThreeMinuteBar, aggregate_complete_three_minute_bars, three_minute_bar_ends
from app.services.trading_calendar_service import get_a_share_calendar


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def save_gzip(path, document):
    tmp = path.with_suffix('.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=3) as f:
        json.dump(document, f, ensure_ascii=False, allow_nan=False)
    tmp.replace(path)


def init_worker(root, calendar, dates, study_hash):
    global ROOT, CALENDAR, DATES, STUDY_HASH, CLIENT, DB, RANKS
    ROOT, CALENDAR, DATES, STUDY_HASH = Path(root), calendar, dates, study_hash
    CLIENT, DB = connect()
    ranks = pd.read_parquet(ROOT/'rank_inputs.parquet')
    RANKS = {code: x.set_index('trade_date').drop(columns='code').to_dict('index')
             for code, x in ranks.groupby('code', sort=False)}


def load_inputs(code):
    cached = ROOT/'inputs'/f'{code}.json.gz'
    if cached.exists():
        with gzip.open(cached, 'rt') as f:
            document=json.load(f)
        return upgrade_historical_basis(document,cached)
    projection = {'_id':0, 'code':1, 'name':1, 'trade_date':1, 'open':1, 'high':1, 'low':1,
                  'close':1, 'pct_chg':1, 'change_amount':1}
    daily = list(DB.stock_daily_detail.find({'code':code, 'adjust':'qfq', 'trade_date':{'$lte':DATES[-1]}}, projection).sort('trade_date',1))
    inputs = observation_inputs(daily, CALENDAR, DATES, code)
    eligible = [day for day, item in inputs.items() if could_buy(item)]
    if not eligible:
        document = dict(code=code, daily=daily, bars={}, audit=[], no_possible_adx_buy=True)
        save_gzip(cached, document)
        return document
    start = min(eligible)
    historical, actual = defaultdict(list), defaultdict(list)
    if start <= '2026-08-31':
        cursor = DB.stock_history_3m_bars_ths_forward_stage.find(
            {'code':code, 'adjust':'qfq', 'interval':'3m',
             'timestamp':{'$gte':start+'T09:33:00+08:00', '$lte':min(DATES[-1],'2026-08-31')+'T15:00:00+08:00'}},
            {'_id':0,'trade_date':1,'timestamp':1,'open':1,'high':1,'low':1,'close':1,'adjust':1,'interval':1})
        for row in cursor:
            historical[row['trade_date']].append(row)
    start_actual = max(start, '2026-08-20')
    if start_actual <= DATES[-1]:
        cursor = DB.stock_realtime_minute_bars.find(
            {'code':code, 'interval':'1m', 'timestamp':{'$gte':start_actual+'T09:30:00+08:00', '$lte':DATES[-1]+'T14:59:00+08:00'}},
            {'_id':0,'trade_date':1,'timestamp':1,'open':1,'high':1,'low':1,'close':1,'previous_close':1})
        cursor = cursor.hint('uniq_realtime_code_interval_timestamp').batch_size(10000)
        for row in cursor:
            actual[row['trade_date']].append(row)
    historical_daily = {r['trade_date']:r for r in DB.stock_history_daily_bars_ths_forward_stage.find(
        {'code':code, 'adjust':'qfq', 'trade_date':{'$gte':'2026-06-01','$lte':'2026-08-31'}},
        {'_id':0,'trade_date':1,'close':1})} if historical else {}
    by_day = {r['trade_date']:r for r in daily}
    positions = {d:i for i,d in enumerate(CALENDAR)}
    output, audit = {}, []
    last_source, last_close, last_day = None, None, None
    for day in DATES:
        if day < start:
            continue
        previous = CALENDAR[positions[day]-1]
        observed = aggregate_complete_three_minute_bars(actual.get(day, ()), trade_date=day)
        saved = sorted(historical.get(day, ()), key=lambda r:r['timestamp'])
        source, method = 'actual_1m', 'captured_reference'
        bars = observed
        historical_valid = (len(saved)==80 and [r['timestamp'] for r in saved]==list(three_minute_bar_ends(day))
            and all(all(math.isfinite(r[k]) and r[k]>0 for k in ['open','high','low','close'])
                    and r['high']>=max(r['open'],r['close']) and r['low']<=min(r['open'],r['close']) for r in saved))
        if day < '2026-08-20' and historical_valid:
            reference = historical_daily.get(previous, {}).get('close')
            # Old data are a consistently adjusted historical series. Verify
            # return scale against a separate daily source; no interpolation.
            current = by_day.get(day, {})
            pct = current.get('pct_chg')
            if reference and pct is not None and abs((saved[-1]['close']/reference-1)*100-pct) <= .05:
                source, method = 'historical_qfq_3m', 'historical_previous_close_return_crosscheck'
                bars = tuple(LiveThreeMinuteBar(
                    start_at=(datetime.fromisoformat(r['timestamp'])-timedelta(minutes=3)).isoformat(),
                    end_at=r['timestamp'], open=r['open'], high=r['high'], low=r['low'], close=r['close'],
                    previous_close=reference) for r in saved)
            else:
                method = 'historical_reference_not_verified'
        elif 0 < len(observed) < 80 and historical_valid:
            accepted, validation = validate_history_day(rows=saved, observed_bars=observed, trade_date=day)
            if accepted:
                captured = {b.previous_close for b in observed if b.previous_close is not None}
                reference = next(iter(captured)) if len(captured)==1 else None
                source, method = 'validated_historical_fallback', validation
                bars = tuple(LiveThreeMinuteBar(
                    start_at=(datetime.fromisoformat(r['timestamp'])-timedelta(minutes=3)).isoformat(),
                    end_at=r['timestamp'], open=r['open'], high=r['high'], low=r['low'], close=r['close'],
                    previous_close=reference) for r in saved)
        if bars and any(b.previous_close is None for b in bars):
            captured = {b.previous_close for b in bars if b.previous_close is not None}
            if len(captured)==1:
                reference, method = next(iter(captured)), 'same_day_captured_reference'
            elif len(captured)>1:
                reference, method = None, 'conflicting_captured_reference'
            else:
                previous_rows = actual.get(previous, ())
                previous_observed = next((r['close'] for r in previous_rows if r['timestamp']==previous+'T14:59:00+08:00'), None)
                observed_close = bars[-1].close if bars[-1].end_at.endswith('15:00:00+08:00') else next(
                    (r['close'] for r in actual.get(day,()) if r['timestamp']==day+'T14:59:00+08:00'), None)
                reference, method = recover_previous_close(daily=by_day.get(day), previous_daily=by_day.get(previous),
                    observed_close=observed_close, previous_observed_close=previous_observed)
            if reference is not None:
                bars = tuple(replace(b, previous_close=reference) if b.previous_close is None else b for b in bars)
        switch_gap = None
        if bars and last_source and source != last_source and last_day == previous and bars[0].previous_close:
            switch_gap = bars[0].previous_close/last_close-1
        audit.append(dict(trade_date=day, source=source, reference_method=method, raw_minutes=len(actual.get(day,())),
                          historical_bars=len(saved), bars=len(bars), complete=len(bars)==80 and all(b.previous_close for b in bars),
                          source_switch_gap=switch_gap, has_daily=day in by_day))
        if bars:
            output[day] = [asdict(b) for b in bars]
            last_source, last_close, last_day = source, bars[-1].close, day
    document = dict(code=code, daily=daily, bars=output, audit=audit, no_possible_adx_buy=False)
    save_gzip(cached, document)
    return upgrade_historical_basis(document,cached)


def upgrade_historical_basis(document, cached):
    """Repair cached historical basis; actual one-minute observations are kept."""
    if document.get('input_price_basis_version')==2:
        return document
    code=document['code']
    early=[r for r in document['audit'] if r['trade_date']<'2026-08-20']
    if early:
        history={r['trade_date']:r for r in DB.stock_history_daily_bars_ths_forward_stage.find(
            {'code':code,'adjust':'qfq','trade_date':{'$gte':'2026-06-01','$lt':'2026-08-20'}},
            {'_id':0,'trade_date':1,'open':1,'high':1,'low':1,'close':1})}
        missing=[r['trade_date'] for r in early if not document['bars'].get(r['trade_date'])]
        recovered=defaultdict(list)
        if missing:
            for r in DB.stock_history_3m_bars_ths_forward_stage.find(
                {'code':code,'adjust':'qfq','interval':'3m','trade_date':{'$in':missing}},
                {'_id':0,'trade_date':1,'timestamp':1,'open':1,'high':1,'low':1,'close':1}):
                recovered[r['trade_date']].append(r)
        daily={r['trade_date']:r for r in document['daily']}
        positions={d:i for i,d in enumerate(CALENDAR)}
        for audit in early:
            day=audit['trade_date'];previous=CALENDAR[positions[day]-1]
            native=[dict(timestamp=r['end_at'],**{k:r[k] for k in ['open','high','low','close']})
                    for r in document['bars'].get(day,())] or recovered.get(day,())
            bars,validation=restore_historical_day(rows=native,previous_native=history.get(previous),
                previous_target=daily.get(previous),current_target=daily.get(day),trade_date=day)
            audit.update(source='historical_3m_rebased_to_daily',reference_method=validation['method'],
                         bars=len(bars),complete=len(bars)==80,price_basis_validation=validation)
            if bars:
                document['bars'][day]=[asdict(b) for b in bars]
            else:
                document['bars'].pop(day,None)
        previous_audit=None
        for audit in document['audit']:
            day=audit['trade_date'];bars=document['bars'].get(day,())
            audit['source_switch_gap']=None
            if bars and previous_audit and previous_audit['source']!=audit['source']:
                previous=CALENDAR[positions[day]-1];old=document['bars'].get(previous,())
                if old and bars[0]['previous_close'] is not None:
                    audit['source_switch_gap']=bars[0]['previous_close']/old[-1]['close']-1
            if bars:
                previous_audit=audit
    document['input_price_basis_version']=2
    save_gzip(cached,document)
    return document


def process_stock(code):
    target = ROOT/'stocks'/f'{code}.json.gz'
    if target.exists():
        with gzip.open(target,'rt') as f:
            existing = json.load(f)
        if existing['study_hash'] != STUDY_HASH:
            raise ValueError('Resume implementation/protocol changed')
        return {'code':code, 'resumed':True}
    data = load_inputs(code)
    inputs = observation_inputs(data['daily'], CALENDAR, DATES, code)
    bars = {day:tuple(LiveThreeMinuteBar(**r) for r in rows) for day,rows in data['bars'].items()}
    results = replay_paths(code=code, dates=DATES, inputs=inputs, bars=bars, ranks=RANKS.get(code,{}))
    document = dict(code=code, study_hash=STUDY_HASH, input_sha256=digest(ROOT/'inputs'/f'{code}.json.gz'),
                    coverage=data['audit'], results=results)
    save_gzip(target, document)
    return {'code':code, 'resumed':False, 'buy_count':sum(e['action']=='buy' for e in results['normal::baseline']['events']),
            'incomplete_days':sum(not a['complete'] and a['has_daily'] for a in data['audit'])}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start-date', default='2026-07-01')
    p.add_argument('--end-date', default='2026-11-06')
    p.add_argument('--factor-root', type=Path, default=Path('.local/analysis/factor_alpha_20260701_20261106'))
    p.add_argument('--output-root', type=Path, required=True)
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--codes', help='Comma-separated smoke-test codes; ranks still use full frozen universe')
    p.add_argument('--aggregate-only', action='store_true')
    args = p.parse_args(argv); root = args.output_root.resolve(); root.mkdir(parents=True, exist_ok=True)
    calendar = [d.strftime('%Y-%m-%d') for d in get_a_share_calendar().sessions_in_range('2024-01-01',args.end_date)]
    dates = [d for d in calendar if args.start_date<=d<=args.end_date]
    if not dates or dates[0]!=args.start_date or dates[-1]!=args.end_date:
        raise ValueError('Use completed trading-session endpoints')
    universe = json.loads((args.factor_root/'universe.json').read_text())['codes']
    codes = sorted(args.codes.split(',')) if args.codes else universe
    if not set(codes)<=set(universe):
        raise ValueError('Codes must belong to the frozen pre-start universe')
    if args.aggregate_only:
        from app.quant.research.strategy1_filter_report import aggregate_and_report
        aggregate_and_report(root, args.factor_root)
        return
    for sub in ['inputs','stocks']:
        (root/sub).mkdir(exist_ok=True)
    if not (root/'rank_inputs.parquet').exists():
        panel = pd.read_parquet(args.factor_root/'factor_panel.parquet',
            columns=['code','trade_date','normalized_close','volume',*FACTOR_KEYS])
        rank_inputs(panel, universe).to_parquet(root/'rank_inputs.parquet',index=False)
    implementation = [Path(__file__).resolve(), Path(__file__).resolve().parents[1]/'research'/'strategy1_filters.py',
                      Path(__file__).resolve().parents[1]/'research'/'historical_price_basis.py',
                      Path(__file__).resolve().parents[1]/'runtime'/'live.py']
    protocol = dict(start=args.start_date, end=args.end_date, dates=dates, codes=codes,
        universe_size=len(universe), candidates=[dict(key=k,label=l,rules=r) for k,l,r in FILTERS],
        capital='100000 per pre-start stock, no transfers; inactive cash retained in denominator',
        engine='replay_live_day; original MACD20/100/30, ADX14 and E2; only extra signal-date buy veto',
        modes=['normal','double_cost'], sample_kind='retrospective; whole window already used; not independent OOS',
        missing='no invented bars/reference; preserve held stale valuation and disclose',
        source_basis='pre-Aug20 qfq restored with prior-day affine calibration and same-day OHLC verification; later actual 1m with matched fallback',
        future_plan='candidate rules frozen for this study only; no production configuration change',
        rank_sha256=digest(root/'rank_inputs.parquet'), implementation={str(q):digest(q) for q in implementation})
    study_hash = hashlib.sha256(json.dumps(protocol,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if (root/'protocol.json').exists() and json.loads((root/'protocol.json').read_text()) != json.loads(json.dumps(protocol)):
        raise ValueError('Existing output protocol differs; choose a new output directory')
    (root/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    t = monotonic(); completed=0; errors=[]
    with ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker,
                             initargs=(str(root),calendar,dates,study_hash)) as pool:
        futures={pool.submit(process_stock,c):c for c in codes}
        for future in as_completed(futures):
            code=futures[future]
            try:
                result=future.result()
            except Exception as exc:
                errors.append({'code':code,'type':type(exc).__name__,'error':str(exc)})
            completed+=1
            if completed%25==0 or completed==len(codes):
                print(json.dumps(dict(completed=completed,total=len(codes),errors=len(errors),elapsed_seconds=round(monotonic()-t,1))),flush=True)
                (root/'progress.json').write_text(json.dumps(dict(completed=completed,total=len(codes),errors=errors),ensure_ascii=False,indent=2))
    if errors:
        raise RuntimeError(f'{len(errors)} stocks failed; inspect progress.json, then resume')
    from app.quant.research.strategy1_filter_report import aggregate_and_report
    aggregate_and_report(root,args.factor_root)


if __name__=='__main__':
    main()
