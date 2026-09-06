"""按当前策略从统一起点连续补录；先分集合重放、核验、备份，再原子替换结果集合。"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.quant.cli.migrate_live_adx14 import validate_document
from app.quant.data.historical_reference import recover_previous_close, validate_history_day
from app.quant.runtime.daily_flow import daily_flow_document
from app.quant.runtime.live import LiveThreeMinuteBar, opening_flow_from_document, observation_spec_from_document, replay_live_day
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID
from app.quant.strategies.provisional_daily_macd_3m.adx import LIVE_RECORDING_START
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from app.services.quant_live_service import CN_TZ, QuantLiveService


RESULT_COLLECTION = 'quant_daily_results'


def digest(document):
    clean = {key: value for key, value in document.items() if key != '_id'}
    return hashlib.sha256(json_util.dumps(clean, sort_keys=True).encode()).hexdigest()


def save_gzip(path, value):
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        stream.write(json_util.dumps(value, ensure_ascii=False))


class HistoricalReplayService(QuantLiveService):
    """只在离线补录中补齐可核验的前收盘价；线上缺前收盘价仍禁止撮合。"""

    async def _load_three_minute_bars(self, *, trade_date, codes):
        bars = await super()._load_three_minute_bars(trade_date=trade_date, codes=codes)
        audit = {code: {'method': 'captured_quote_reference' if bars[code] else 'no_contiguous_bars',
                        'bar_source': 'captured_minute_quotes'} for code in codes}
        incomplete = [code for code in codes if 0 < len(bars[code]) < 80]
        historical_closes = {}
        if incomplete:
            history = defaultdict(list)
            async for row in self.database['stock_history_3m_bars_ths_forward_stage'].find(
                {'trade_date': trade_date, 'code': {'$in': incomplete}, 'adjust': 'qfq', 'interval': '3m'},
                {'_id': 0, 'code': 1, 'timestamp': 1, 'open': 1, 'high': 1, 'low': 1, 'close': 1,
                 'adjust': 1, 'interval': 1}):
                history[row['code']].append(row)
            for code in incomplete:
                accepted, validation = validate_history_day(rows=history[code], observed_bars=bars[code], trade_date=trade_date)
                audit[code]['history_validation'] = validation
                if accepted:
                    selected = sorted(history[code], key=lambda row: row['timestamp'])
                    captured = {bar.previous_close for bar in bars[code] if bar.previous_close is not None}
                    reference = next(iter(captured)) if len(captured) == 1 else None
                    bars[code] = tuple(LiveThreeMinuteBar(
                        start_at=(datetime.fromisoformat(row['timestamp'])-timedelta(minutes=3)).isoformat(),
                        end_at=row['timestamp'], open=row['open'], high=row['high'], low=row['low'], close=row['close'],
                        previous_close=reference) for row in selected)
                    historical_closes[code] = float(selected[-1]['close'])
                    audit[code]['bar_source'] = 'validated_historical_3m_from_1m'
        missing = [code for code in codes if bars[code] and any(b.previous_close is None for b in bars[code])]
        if missing:
            previous_row = await self.daily_collection.find_one(
                {'adjust': 'qfq', 'trade_date': {'$lt': trade_date}}, {'_id': 0, 'trade_date': 1}, sort=[('trade_date', -1)])
            previous_date = previous_row['trade_date']
            daily_by_date, close_by_date = {}, {}
            for day in (previous_date, trade_date):
                daily_by_date[day] = {r['code']: r async for r in self.daily_collection.find(
                    {'adjust': 'qfq', 'trade_date': day, 'code': {'$in': missing}},
                    {'_id': 0, 'code': 1, 'close': 1, 'change_amount': 1, 'pct_chg': 1})}
                close_by_date[day] = {r['code']: r['close'] async for r in self.minute_collection.find(
                    {'trade_date': day, 'interval': '1m', 'timestamp': day+'T14:59:00+08:00', 'code': {'$in': missing}},
                    {'_id': 0, 'code': 1, 'close': 1})}
            for code in missing:
                captured = {b.previous_close for b in bars[code] if b.previous_close is not None}
                if len(captured) == 1:
                    reference, method = captured.pop(), 'same_day_captured_quote_reference'
                elif captured:
                    reference, method = None, 'conflicting_captured_references'
                else:
                    reference, method = recover_previous_close(
                        daily=daily_by_date[trade_date].get(code), previous_daily=daily_by_date[previous_date].get(code),
                        observed_close=historical_closes.get(code, close_by_date[trade_date].get(code)),
                        previous_observed_close=close_by_date[previous_date].get(code))
                audit[code].update({'method': method, 'previous_close': reference,
                    'current_daily': daily_by_date[trade_date].get(code),
                    'previous_daily': daily_by_date[previous_date].get(code),
                    'observed_last_minute_close': close_by_date[trade_date].get(code),
                    'previous_observed_last_minute_close': close_by_date[previous_date].get(code),
                    'selected_history_close': historical_closes.get(code)})
                if reference is not None:
                    bars[code] = tuple(replace(bar, previous_close=reference) if bar.previous_close is None else bar for bar in bars[code])
        self.reference_audit = audit
        return bars


def validate_chain(previous, current):
    if previous is None:
        assert current['trade_date'] == LIVE_RECORDING_START
        opening = current['_runtime_state']['opening_flow']
        assert not opening['holdings']
        assert all(a['cash'] == a['initial_cash'] == 100000 for a in opening['accounts'])
    else:
        internal = current['_runtime_state']
        assert internal['opening_flow']['accounts'] == previous['_runtime_state']['accounts']
        assert internal['opening_exit_states'] == previous['_runtime_state']['exit_states']
        assert internal['opening_pending_signals'] == previous['_runtime_state']['pending_signals']
        assert {a['code'] for a in internal['accounts']} == {a['code'] for a in previous['_runtime_state']['accounts']}
    assert current['recording']['start_date'] == LIVE_RECORDING_START
    assert current['strategy']['recording_start_date'] == LIVE_RECORDING_START
    summary = current['summary']
    active = [a for a in current['_runtime_state']['accounts'] if a.get('first_buy_at')]
    assert len(active) == summary['account_count']
    assert round(sum(a['initial_cash'] for a in active), 2) == summary['initial_capital']
    assert round(sum(a['cash'] for a in active), 2) == summary['cash_balance']
    assert round(sum(h['market_value'] for h in current['holding_pool']['items']), 2) == summary['market_value']
    assert round(summary['cash_balance'] + summary['market_value'], 2) == summary['total_assets']


async def run(args):
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=10000)
    db = client[settings.mongo_db_name]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage_name = 'quant_daily_results_extend_' + args.run_id
    backup_name = 'quant_daily_results_before_' + args.run_id
    try:
        if args.end_date >= datetime.now(CN_TZ).date():
            raise ValueError('仅补录已结束的历史日期')
        dates = sorted(await db['stock_daily_detail'].distinct('trade_date', {
            'adjust': 'qfq', 'trade_date': {'$gte': LIVE_RECORDING_START, '$lte': args.end_date.isoformat()}}))
        assert dates and dates[0] == LIVE_RECORDING_START
        baseline_path = output/'source_hashes_before.json'
        production = await db[RESULT_COLLECTION].find({}).to_list(length=None)
        hashes = sorted(digest(d) for d in production)
        if baseline_path.exists():
            assert json.loads(baseline_path.read_text()) == hashes, '正式结果已变化，不能沿用旧补录基线'
        else:
            baseline_path.write_text(json.dumps(hashes, indent=2))
            for doc in production:
                save_gzip(output/f'{doc["trade_date"]}_before.json.gz', doc)
        service = HistoricalReplayService(db)
        service.results = QuantDailyResultRepository(db, collection_name=stage_name)
        await service.ensure_indexes()
        validations, previous = [], None
        for day in dates:
            prepared = await service.prepare(date.fromisoformat(day))
            print(f'prepared {day}: tracked={prepared["runtime"]["tracked_code_count"]}', flush=True)
            result = await service.process(now=datetime.fromisoformat(day+'T15:10:00+08:00'))
            internal = result['_runtime_state']
            opening = opening_flow_from_document(internal['opening_flow'])
            specs = [observation_spec_from_document(item) for item in internal['observation_specs']]
            codes = sorted({*(item.code for item in specs), *(h.code for h in opening.holdings),
                            *(s['code'] for s in internal['opening_pending_signals'])})
            bars = await service._load_three_minute_bars(trade_date=day, codes=codes)
            replayed = replay_live_day(opening_flow=opening, observation_specs=specs,
                opening_pending_signals=internal['opening_pending_signals'], bars_by_code=bars,
                expected_bar_count=80, close_market=True, opening_exit_states=internal.get('opening_exit_states', {}))
            expected_signals = sorted((dict(s) for s in replayed['signals']), key=lambda s: (s.get('signal_at') or '', s['code']))
            assert expected_signals == result['signals']['items'], '重复信号不一致'
            assert [asdict(a) for a in replayed['flow'].accounts] == internal['accounts']
            assert replayed['exit_states'] == internal['exit_states']
            rebuilt = daily_flow_document(replayed['flow'])
            for field in ('holding_pool', 'intraday_trading', 'closed_trades'):
                assert rebuilt[field] == result[field], field
            for field in ('total_assets', 'total_pnl', 'realized_pnl', 'unrealized_pnl', 'account_day_pnl'):
                assert rebuilt['summary'][field] == result['summary'][field], field
            counts = dict(Counter(row['method'] for row in service.reference_audit.values()))
            bar_sources = dict(Counter(row['bar_source'] for row in service.reference_audit.values()))
            result['recording'].update(
                reference_price_method='captured_or_validated_daily_change_reference',
                historical_bar_policy='complete_observed_day_else_verified_historical_day',
                history_rebased_at=datetime.now(CN_TZ).isoformat())
            result['runtime']['preparation_quality']['historical_reference_counts'] = counts
            result['runtime']['preparation_quality']['historical_bar_source_counts'] = bar_sources
            result['runtime']['source']['historical_intraday'] = 'stock_history_3m_bars_ths_forward_stage'
            result['runtime']['source']['historical_reference_method'] = 'daily_close_minus_change_with_price_basis_checks'
            await service.results.save_document(result)
            validate_chain(previous, result)
            validation = validate_document(result)
            validation.update(repeated_replay_matches=True, reference_counts=counts, bar_sources=bar_sources,
                              daily_pnl=result['summary']['account_day_pnl'])
            observed = {code: [asdict(b) for b in values] for code, values in bars.items()}
            validation['input_sha256'] = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
            save_gzip(output/f'{day}_observed_3m.json.gz', observed)
            save_gzip(output/f'{day}_reference_audit.json.gz', service.reference_audit)
            save_gzip(output/f'{day}_result.json.gz', result)
            validations.append(validation)
            previous = result
            (output/'progress.json').write_text(json.dumps(validations, ensure_ascii=False, indent=2))
            print(json.dumps({**{k:v for k,v in validation.items() if k != 'incomplete_codes'}, 'incomplete_code_count':len(validation['incomplete_codes'])}, ensure_ascii=False), flush=True)
        report = {'start_date': LIVE_RECORDING_START, 'end_date': dates[-1], 'trading_days': len(dates),
                  'stage_collection': stage_name, 'backup_collection': backup_name, 'validation': validations,
                  'strategy_id': STRATEGY_ID, 'applied': False,
                  'source_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [*Path('app/quant').rglob('*.py'), Path('app/services/quant_live_service.py')]}}
        # 先将可审核结果落盘；发布失败也不丢验证证据。
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if args.apply:
            latest = await db[RESULT_COLLECTION].find({}).to_list(length=None)
            assert sorted(digest(d) for d in latest) == hashes, '正式结果已变化，中止发布'
            if backup_name in await db.list_collection_names():
                raise RuntimeError('备份集合已存在，请使用新的run-id')
            if production:
                await db[backup_name].insert_many(production)
                archived = await db[backup_name].find({}).to_list(length=None)
                assert sorted(digest(d) for d in archived) == hashes
            for doc in production:
                if doc.get('strategy_id') != STRATEGY_ID:
                    await db[stage_name].insert_one(doc)
            staged_dates = sorted(await db[stage_name].distinct('trade_date', {'strategy_id': STRATEGY_ID}))
            assert staged_dates == dates, '暂存集合包含额外或缺少的交易日'
            # 原子rename使读接口看到旧完整集或新完整集，不逐日拼接不同账户起点。
            await db[stage_name].rename(RESULT_COLLECTION, dropTarget=True)
            report['applied'] = True
            report['applied_at'] = datetime.now(CN_TZ).isoformat()
            published = await db[RESULT_COLLECTION].find({'strategy_id': STRATEGY_ID}).sort('trade_date', 1).to_list(length=None)
            assert [d['trade_date'] for d in published] == dates
            for before, after in zip([None, *published[:-1]], published):
                validate_document(after)
                validate_chain(before, after)
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'days': len(dates), 'applied': report['applied'], 'output': str(output)}, ensure_ascii=False), flush=True)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end-date', type=date.fromisoformat, default=date(2026, 9, 4))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.run_id.replace('_', '').isalnum():
        parser.error('run-id只能包含字母、数字和下划线')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
