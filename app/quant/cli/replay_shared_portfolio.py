"""Replay the frozen July-November strategy-1 signals in a 200k shared account."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from functools import lru_cache
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path

import pandas as pd

from app.quant.research.shared_portfolio import AccountRules, FACTOR_COLUMNS, POLICIES, SharedAccount, first_buy, provisional, score
from app.quant.research.strategy1_filters import observation_inputs, could_buy
from app.quant.runtime.live import three_minute_bar_ends
from app.services.trading_calendar_service import get_a_share_calendar


INPUT_ROOT = Path('.local/analysis/strategy1_filters_20260701_20261106_v2')
FACTOR_ROOT = Path('.local/analysis/factor_alpha_20260701_20261106')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_gzip(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return json.load(f)


def save_gzip(path, value):
    temp = path.with_suffix(f'.{os.getpid()}.tmp')
    with gzip.open(temp, 'wt', encoding='utf-8', compresslevel=3) as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
    temp.replace(path)


def prepare_stock(args):
    code, root, dates, calendar, version = args
    source = INPUT_ROOT/'inputs'/f'{code}.json.gz'
    target = Path(root)/'signals'/f'{code}.json.gz'
    source_hash = digest(source)
    if target.exists():
        old = read_gzip(target)
        if old['source_hash']==source_hash and old['version']==version:
            return old['signals']
    data = read_gzip(source)
    inputs = observation_inputs(data['daily'], calendar, dates, code)
    items = {day:dict(item, previous_state=asdict(item['previous_state']) if item['previous_state'] else None)
             for day,item in inputs.items()}
    signals = []
    for day, item in items.items():
        signal = first_buy(item, data['bars'].get(day, []))
        if signal:
            signals.append(signal)
    save_gzip(target, dict(code=code, source_hash=source_hash, version=version, inputs=items, signals=signals))
    return signals


class Market:
    """Read only Mongo; all added volume data are saved for reproducibility."""
    def __init__(self, root):
        self.root, self.client, self.db = root, None, None

    @lru_cache(maxsize=96)
    def data(self, code):
        return read_gzip(INPUT_ROOT/'inputs'/f'{code}.json.gz')

    @lru_cache(maxsize=128)
    def inputs(self, code):
        return read_gzip(self.root/'signals'/f'{code}.json.gz')['inputs']

    @lru_cache(maxsize=128)
    def volumes(self, code):
        path = self.root/'volumes'/f'{code}.json.gz'
        if path.exists():
            return read_gzip(path)
        if self.client is None:
            from app.manually_execute_script.repair_realtime_minutes_ths import connect
            self.client, self.db = connect()
        data = self.data(code)
        sources = {a['trade_date']:a['source'] for a in data['audit']}
        result = {}
        actual = defaultdict(dict)
        projection = {'_id':0, 'trade_date':1, 'timestamp':1, 'volume':1, 'volume_unit':1}
        if any(s=='actual_1m' for s in sources.values()):
            query = {'code':code, 'interval':'1m', 'timestamp':{'$gte':'2026-08-20T09:30:00+08:00', '$lte':'2026-11-06T14:59:00+08:00'}}
            for r in self.db.stock_realtime_minute_bars.find(query,projection).hint('uniq_realtime_code_interval_timestamp').batch_size(10000):
                if sources.get(r['trade_date'])=='actual_1m':
                    assert r.get('volume_unit','share')=='share'
                    actual[r['trade_date']][r['timestamp']] = r.get('volume')
            for day, rows in data['bars'].items():
                if sources.get(day)!='actual_1m':
                    continue
                for bar in rows:
                    start = datetime.fromisoformat(bar['start_at'])
                    values = [actual[day].get((start+timedelta(minutes=i)).isoformat()) for i in range(3)]
                    result[bar['end_at']] = sum(values) if all(v is not None and v>=0 for v in values) else None
        if any(s and s.startswith('historical') for s in sources.values()):
            query = {'code':code, 'interval':'3m', 'adjust':'qfq', 'timestamp':{'$gte':'2026-07-01T09:33:00+08:00', '$lte':'2026-08-31T15:00:00+08:00'}}
            for r in self.db.stock_history_3m_bars_ths_forward_stage.find(query, projection):
                if sources.get(r['trade_date'], '').startswith('historical'):
                    assert r.get('volume_unit')=='share'
                    result[r['timestamp']] = r.get('volume')
        save_gzip(path, result)
        print(json.dumps({'volume_cached':code, 'bars':len(result)}), flush=True)
        return result

    @lru_cache(maxsize=1024)
    def bars(self, code, day):
        data = self.data(code)
        audit = next((a for a in data['audit'] if a['trade_date']==day), {})
        # Get volume only for stocks actually selected or held, never all 5,530.
        volumes = self.volumes(code)
        return {b['end_at']:dict(b, volume=volumes.get(b['end_at']), source=audit.get('source'))
                for b in data['bars'].get(day, [])}


def factor_inputs(root, signals=None):
    target = root/'ranking_inputs.parquet'
    if not target.exists():
        p = pd.read_parquet(FACTOR_ROOT/'factor_panel.parquet', columns=[
            'code','trade_date','volume','normalized_close',*FACTOR_COLUMNS,'adx_14','adx_14_3_days_ago'])
        p['adx_delta'] = p.adx_14-p.adx_14_3_days_ago
        eligible = p.volume.gt(0) & p.normalized_close.notna()
        for k in (*FACTOR_COLUMNS, 'adx_delta'):
            valid = eligible & p[k].notna()
            coverage = valid.groupby(p.trade_date).transform('sum')/eligible.groupby(p.trade_date).transform('sum')
            p[k+'_rank'] = p[k].where(valid & coverage.ge(.8)).groupby(p.trade_date).rank(pct=True)
        p.to_parquet(target,index=False)
    p = pd.read_parquet(target)
    if signals is not None:
        keys=pd.DataFrame(sorted({(s['code'],s['factor_date']) for s in signals}),columns=['code','trade_date'])
        p=p.merge(keys,on=['code','trade_date'],validate='one_to_one')
    p=p.astype(object).where(p.notna(),None)
    return {(r.pop('code'), r.pop('trade_date')):r for r in p.to_dict('records')}


def preopen_candidates(root, codes):
    """Unconditional prior-day watches, including stocks with no future signal."""
    watches=defaultdict(list)
    for code in codes:
        for day, item in read_gzip(root/'signals'/f'{code}.json.gz')['inputs'].items():
            if could_buy(item):
                watches[day].append(dict(code=code,name=item['name'],at=day+'T09:30:00+08:00',
                    factor_date=item['factor_completed_date'],adx=item['adx'],
                    adx_delta=item['adx']-item['adx_3_days_ago']))
    return watches


def select_watchlist(account, watches, factors, exclude_risk=False):
    ranked=[]
    for s in watches:
        if exclude_risk and ('ST' in s['name'].upper() or '退' in s['name']):
            continue
        if s['code'] in account.positions or s['code'] in account.pending:
            continue
        value=score(account.policy,s,factors.get((s['code'],s['factor_date']),{}))
        if value is not None:
            ranked.append(dict(s,score=value))
    ranked.sort(key=lambda r:(-r['score'],r['code']))
    selected=ranked[:5]
    account.watchlists.extend(dict(r,rank=i,candidate_count=len(ranked)) for i,r in enumerate(selected,1))
    return {r['code'] for r in selected}


def simulate(*, market, dates, signals, factors, policy, rules, watches=None, exclude_risk=False):
    account = SharedAccount(policy, rules)
    by_time = defaultdict(list)
    for s in signals:
        by_time[s['at']].append(s)
    quality = []
    for day_index, day in enumerate(dates):
        consecutive = defaultdict(int)
        allowed=select_watchlist(account,watches.get(day,[]),factors,exclude_risk) if watches is not None else None
        for end in three_minute_bar_ends(day):
            # First finish orders carried into this interval. New orders created
            # at its close cannot consume this interval's prices or liquidity.
            for code in list(account.pending):
                bars = market.bars(code, day)
                bar = bars.get(end)
                order = account.pending[code]
                if bar is None:
                    if order['action']=='buy':
                        order['status'] = 'missing_next_bar_cancelled'
                        account.attempts.append(dict(order_id=order['order_id'], code=code, action='buy',at=end,status=order['status']))
                        account.pending.pop(code)
                    continue
                account.execute(code, bar)
            for code, position in list(account.positions.items()):
                bar = market.bars(code, day).get(end)
                if bar is None or not bar.get('volume') or bar['volume']<=0:
                    continue
                position['mark'], position['mark_at'] = bar['close'], end
                consecutive[code] = account.evaluate_exit(code, market.inputs(code).get(day, {}), bar, consecutive[code])
            account.select(by_time.get(end, []), factors, end, allowed_codes=allowed)
        # Daily close is valuation only; never invent intraday fills for a gap.
        for code, position in account.positions.items():
            bars = market.bars(code, day)
            if len(bars)<80:
                quality.append(dict(day=day,code=code,available_bars=len(bars),kind='incomplete_held_day'))
            # Keep the same recorded price basis; do not mix an unverified daily
            # adjusted close into an actual intraday quote series.
        account.close_day(day)
        if (day_index+1)%20==0:
            print(json.dumps(dict(policy=policy, day=day, assets=account.assets(),
                                  held=len(account.positions),fills=len(account.fills))),flush=True)
    return account, quality


def write_result(root, name, account, quality):
    dest = root/'runs'/name
    dest.mkdir(parents=True,exist_ok=True)
    for key in ('orders','attempts','fills','decisions','trades','curve','holdings','exit_events','watchlists'):
        values = getattr(account,key)
        save_gzip(dest/(key+'.json.gz'), values)
        pd.DataFrame(values).to_csv(dest/(key+'.csv'),index=False,encoding='utf-8-sig')
    (dest/'quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
    (dest/'ending_state.json').write_text(json.dumps(dict(cash=account.cash,positions=account.positions,
        pending=account.pending, max_slots=account.max_slots, rules=asdict(account.rules)),ensure_ascii=False,indent=2))
    assets = pd.Series([account.rules.initial_cash]+[r['assets'] for r in account.curve])
    summary = dict(run=name, policy=account.policy, final_assets=account.assets(),
        pnl=round(account.assets()-account.rules.initial_cash,2),return_pct=(account.assets()/account.rules.initial_cash-1)*100,
        max_drawdown_pct=(assets/assets.cummax()-1).min()*100,
        fees=round(sum(r['fees'] for r in account.fills),2),
        buys=sum(r['action']=='buy' for r in account.fills), sells=sum(r['action']=='sell' for r in account.fills),
        closed_trades=len(account.trades), win_rate=sum(r['pnl']>0 for r in account.trades)/len(account.trades) if account.trades else None,
        max_slots=account.max_slots, end_holdings=len(account.positions), end_cash=account.cash,
        realized_pnl=account.realized, unrealized_pnl=round(account.assets()-account.rules.initial_cash-account.realized,2),
        mean_exposure=sum(r['market_value']/r['assets'] for r in account.curve)/len(account.curve),
        incomplete_held_days=len(quality), stale_close_days=sum(r['stale_market_value']>0 for r in account.curve),
        partial_fills=sum(r['partial'] for r in account.fills))
    (dest/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-root',type=Path,default=Path('.local/analysis/strategy1_shared_200k_5stocks_20260701_20261106'))
    p.add_argument('--workers',type=int,default=6)
    p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--policies',default=','.join(k for k,_ in POLICIES))
    p.add_argument('--mode',choices=['normal','stress'],default='normal')
    p.add_argument('--start-date',default='2026-07-01',help='Fresh cash initialization sensitivity; same fixed policies')
    p.add_argument('--preopen-top5',action='store_true',help='Rank all unconditional ADX buy watches before open; wait for original signals')
    p.add_argument('--exclude-risk',action='store_true',help='Pre-open name as-of t-1: exclude ST and delisting labels')
    args=p.parse_args();root=args.output_root.resolve()
    if args.exclude_risk and not args.preopen_top5:
        raise ValueError('Risk-name gate is a pre-open pool comparison')
    for sub in ('signals','volumes','runs'):
        (root/sub).mkdir(parents=True,exist_ok=True)
    original=json.loads((INPUT_ROOT/'protocol.json').read_text())
    dates,codes=original['dates'],original['codes']
    calendar=[d.strftime('%Y-%m-%d') for d in get_a_share_calendar().sessions_in_range('2024-01-01', dates[-1])]
    from app.quant.research import shared_portfolio, strategy1_filters
    version=hashlib.sha256((inspect.getsource(first_buy)+inspect.getsource(provisional)+digest(strategy1_filters.__file__)).encode()).hexdigest()
    protocol=dict(start=dates[0],end=dates[-1],dates=dates,universe_size=len(codes),
        rules=asdict(AccountRules()), policies=POLICIES, ranking='Only simultaneous original buy signals; t-1 factors; higher score first; code breaks ties',
        replacements='None: keep original MACD/ADX/E2 exits; reinvest 20% of decision-time NAV per new position including fees',
        matching='Next completed 3m bar, adverse tick-rounded open slippage, no high/low clipping; 5% volume cap. Buy remainder IOC cancelled; sells persist. T+1.',
        capital='One shared 200000 CNY account, maximum five held/pending-buy codes, no borrowing or top-up',
        permissions='Research assumes existing trading permissions for all frozen-universe boards; no real broker order',
        limitations=['OHLCV shadow simulation, no bid/ask depth or queue reconstruction',
            'Pre-Aug20 restored historical 3m price basis; Aug20+ actual 1m aggregated; daily source consistency validation is retrospective',
            'Stored daily factors are historical versions, not an archived publication-time feed',
            'Corporate-action cash distributions and dividend withholding not separately booked; reference gaps audited',
            'Same July-Nov sample already explored; no independent out-of-sample claim'],
        stress='Commission/slippage doubled, participation reduced from 5% to 1%; exchange taxes unchanged',
        source_protocol_sha256=digest(INPUT_ROOT/'protocol.json'), factor_panel_sha256=digest(FACTOR_ROOT/'factor_panel.parquet'),
        implementation_version=version)
    if not (root/'protocol.json').exists():
        (root/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    (root/'preopen_protocol.json').write_text(json.dumps(dict(
        reason='After simultaneous-rank replay revealed early arrivals consume the five slots',
        rule='Daily pre-open top 5 among all prior-day MACD/ADX buy watches, excluding currently held/pending codes. No future intraday signal/price used in eligibility. Original signal still required.',
        count=5,policies=POLICIES,selection_history='Second exploratory mechanism comparison after first six simulations; not independent OOS',
        inputs_version=version),ensure_ascii=False,indent=2))
    all_signals=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        work=((c,str(root),dates,calendar,version) for c in codes)
        for i, rows in enumerate(pool.map(prepare_stock,work,chunksize=10),1):
            all_signals.extend(rows)
            if i%250==0 or i==len(codes):
                print(json.dumps(dict(prepared=i,total=len(codes),signals=len(all_signals))),flush=True)
    all_signals.sort(key=lambda s:(s['at'],s['code']))
    save_gzip(root/'all_signals.json.gz',all_signals)
    if args.prepare_only:
        return
    if args.start_date not in dates:
        raise ValueError('Start must be a recorded trading date')
    dates=[d for d in dates if d>=args.start_date]
    all_signals=[s for s in all_signals if s['at'][:10]>=args.start_date]
    watches=preopen_candidates(root,codes) if args.preopen_top5 else None
    keys=all_signals+[s for rows in watches.values() for s in rows] if watches is not None else all_signals
    factors=factor_inputs(root,keys); market=Market(root)
    rules=AccountRules() if args.mode=='normal' else AccountRules(commission=.0002,slippage=.001,participation=.01)
    summaries=[]
    for policy in args.policies.split(','):
        if policy not in dict(POLICIES):
            raise ValueError(policy)
        account,quality=simulate(market=market,dates=dates,signals=all_signals,factors=factors,policy=policy,rules=rules,watches=watches,exclude_risk=args.exclude_risk)
        tag=args.mode+('' if args.start_date=='2026-07-01' else '_'+args.start_date.replace('-',''))
        if args.preopen_top5:
            tag=('preopen_no_st_' if args.exclude_risk else 'preopen_')+tag
        summaries.append(write_result(root,tag+'__'+policy,account,quality))
        (root/'runs'/(tag+'__'+policy)/'implementation.json').write_text(json.dumps({
            'engine':digest(shared_portfolio.__file__),'runner':digest(__file__),
            'signal_inputs_version':version,'start_date':dates[0],'preopen_top5':args.preopen_top5,
            'exclude_risk':args.exclude_risk},indent=2))
    if market.client:
        market.client.close()
    print(json.dumps({'complete':len(summaries)}),flush=True)


if __name__=='__main__':
    main()
