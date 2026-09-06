"""从2026-09-03的真实采集行情重放新版影子盘，验证后替换每日结果。"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
from datetime import date, datetime, timezone
from dataclasses import asdict
from pathlib import Path

from bson import BSON, json_util
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID, STRATEGY_VERSION
from app.quant.strategies.provisional_daily_macd_3m.adx import LIVE_RECORDING_START
from app.quant.runtime.live import opening_flow_from_document, observation_spec_from_document, replay_live_day
from app.quant.runtime.daily_flow import daily_flow_document
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from app.services.quant_live_service import CN_TZ, QuantLiveService


STAGE_COLLECTION = 'quant_daily_results_adx14_v2_stage'
ARCHIVE_COLLECTION = 'quant_daily_results_strategy_archive'


def validate_document(document):
    assert document['strategy']['version'] == STRATEGY_VERSION
    assert document['status'] == 'closed'
    assert document['recording']['mode'] == 'historical_replay'
    accounts = document['_runtime_state']['accounts']
    assert accounts and len({a['code'] for a in accounts}) == len(accounts)
    assert all(a['initial_cash'] == 100000 for a in accounts)
    assert all(a['cash'] >= 0 for a in accounts)
    for signal in document['signals']['items']:
        if signal['action'] == 'buy' and signal['status'] in ('filled', 'pending_execution'):
            assert signal['adx_14'] >= 20 and signal['adx_14'] > signal['adx_14_3_days_ago']
            assert signal['factor_completed_date'] < signal['signal_at'][:10]
        if signal['status'] == 'filled':
            assert signal['execution_at'] >= signal['signal_at']
    for trade in document['closed_trades']['items']:
        assert trade['entry_execution_at'][:10] < trade['exit_execution_at'][:10]
    size = len(BSON.encode(document))
    assert size < 16 * 1024 * 1024
    summary = document['summary']
    assert abs(summary['total_assets'] - summary['initial_capital'] - summary['total_pnl']) < .01
    return {'trade_date': document['trade_date'], 'strategy_version': STRATEGY_VERSION,
            'account_count': len(accounts), 'buy_count': summary['buy_count'],
            'sell_count': summary['sell_count'], 'holding_count': summary['holding_count'],
            'total_pnl': summary['total_pnl'], 'total_return': summary['total_return'],
            'data_status': document['runtime']['data_status'],
            'incomplete_codes': document['runtime']['incomplete_codes'],
            'bson_bytes': size, 'passed': True}


async def run(args):
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri, serverSelectionTimeoutMS=10000)
    database = client[settings.mongo_db_name]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        dates = sorted(await database['stock_daily_detail'].distinct('trade_date', {
            'adjust': 'qfq', 'trade_date': {'$gte': LIVE_RECORDING_START, '$lte': args.end_date.isoformat()}}))
        if not dates or dates[0] != LIVE_RECORDING_START:
            raise ValueError('缺少起始日真实日线，不能迁移')
        if args.end_date >= datetime.now(CN_TZ).date():
            raise ValueError('迁移只处理已结束的历史日期')
        service = QuantLiveService(database)
        service.results = QuantDailyResultRepository(database, collection_name=STAGE_COLLECTION)
        await service.ensure_indexes()
        source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [*Path('app/quant').rglob('*.py'), Path('app/services/quant_live_service.py')]}
        documents, validations = [], []
        for day in dates:
            if not await database['stock_realtime_minute_bars'].find_one({'trade_date': day, 'interval':'1m'}, {'_id':1}):
                raise ValueError(f'{day}: 没有真实分钟行情')
            prepared = await service.prepare(date.fromisoformat(day))
            print(f'prepared {day}: tracked={prepared["runtime"]["tracked_code_count"]}', flush=True)
            result = await service.process(now=datetime.fromisoformat(day+'T15:10:00+08:00'))
            validation = validate_document(result)
            internal = result['_runtime_state']
            opening = opening_flow_from_document(internal['opening_flow'])
            specs = [observation_spec_from_document(item) for item in internal['observation_specs']]
            codes = sorted({*(item.code for item in specs), *(h.code for h in opening.holdings),
                            *(item['code'] for item in internal['opening_pending_signals'])})
            market_bars = await service._load_three_minute_bars(trade_date=day, codes=codes)
            replayed = replay_live_day(opening_flow=opening, observation_specs=specs,
                opening_pending_signals=internal['opening_pending_signals'], bars_by_code=market_bars,
                expected_bar_count=80, close_market=True,
                opening_exit_states=internal.get('opening_exit_states', {}))
            assert list(replayed['signals']) == result['signals']['items'], 'Repeated signal path changed'
            assert [asdict(a) for a in replayed['flow'].accounts] == internal['accounts']
            assert replayed['exit_states'] == internal['exit_states']
            rebuilt = daily_flow_document(replayed['flow'])
            for field in ('holding_pool', 'intraday_trading', 'closed_trades'):
                assert rebuilt[field] == result[field], f'Repeated {field} changed'
            for field in ('total_assets', 'total_pnl', 'realized_pnl', 'unrealized_pnl', 'account_day_pnl'):
                assert rebuilt['summary'][field] == result['summary'][field]
            observed = {code:[asdict(b) for b in bars] for code,bars in market_bars.items()}
            validation['repeated_replay_matches'] = True
            validation['three_minute_input_sha256'] = hashlib.sha256(json.dumps(observed,sort_keys=True).encode()).hexdigest()
            with gzip.open(output / f'{day}_observed_3m.json.gz', 'wt', encoding='utf-8') as f:
                json.dump(observed,f,ensure_ascii=False)
            documents.append(result)
            validations.append(validation)
            with gzip.open(output / f'{day}_new.json.gz', 'wt', encoding='utf-8') as f:
                f.write(json_util.dumps(result, ensure_ascii=False))
            print(json.dumps(validation, ensure_ascii=False), flush=True)
        universe = {a['code'] for a in documents[0]['_runtime_state']['accounts']}
        assert all({a['code'] for a in d['_runtime_state']['accounts']} == universe for d in documents)
        for previous, current in zip(documents, documents[1:]):
            assert current['_runtime_state']['opening_flow']['accounts'] == previous['_runtime_state']['accounts']
            assert current['_runtime_state']['opening_exit_states'] == previous['_runtime_state']['exit_states']
        report = {'start_date':LIVE_RECORDING_START,'end_date':dates[-1],
                  'strategy_id':STRATEGY_ID,'strategy_version':STRATEGY_VERSION,
                  'source_hashes':source_hashes,'validation':validations,'applied':False,
                  'computed_at':datetime.now(CN_TZ).isoformat()}
        if args.apply:
            repository = QuantDailyResultRepository(database)
            archive = database[ARCHIVE_COLLECTION]
            await archive.create_index([('strategy_id',1),('trade_date',1),('archived_strategy_version',1)],unique=True)
            for document in documents:
                day = document['trade_date']
                old = await repository.get(day)
                if old and old.get('strategy',{}).get('version') != STRATEGY_VERSION:
                    with gzip.open(output / f'{day}_old.json.gz', 'wt', encoding='utf-8') as f:
                        f.write(json_util.dumps(old, ensure_ascii=False))
                    archived = {k:v for k,v in old.items() if k!='_id'}
                    archived.update(archived_strategy_version=old.get('strategy',{}).get('version','unknown'),
                                    archived_at=datetime.now(timezone.utc),archive_reason='ADX14_E2_v2_migration')
                    await archive.update_one({'strategy_id':STRATEGY_ID,'trade_date':day,
                        'archived_strategy_version':archived['archived_strategy_version']},
                        {'$setOnInsert':archived},upsert=True)
                saved = {k:v for k,v in document.items() if k!='_id'}
                await repository.save_document(saved)
                verify = await repository.get(day)
                assert validate_document(verify)['passed']
            report['applied'] = True
        (output/'migration_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'output':str(output),'applied':report['applied'],'days':len(dates)},ensure_ascii=False),flush=True)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end-date', type=date.fromisoformat, default=date(2026,9,4))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
