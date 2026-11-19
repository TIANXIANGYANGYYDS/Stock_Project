"""Rebuild closed partial days after minute repair, validating the entire affected tail."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import fcntl
import json
from pathlib import Path
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import PROJECT_ROOT, get_settings
from app.quant.cli.extend_live_history import HistoricalReplayService, digest, save_gzip, validate_chain
from app.quant.cli.migrate_live_adx14 import validate_document
from app.quant.runtime.daily_flow import daily_flow_document
from app.quant.runtime.live import opening_flow_from_document, observation_spec_from_document, replay_live_day
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from app.services.quant_live_service import CN_TZ, QuantLiveService
from app.services.trading_calendar_service import get_a_share_calendar

COLLECTION = 'quant_daily_results'


@contextmanager
def recovery_lock(path=PROJECT_ROOT / '.local/run/quant_recovery.lock'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def document_key(document):
    return str(document['_id'])


async def manifest(collection):
    return {document_key(row): digest(row) async for row in collection.find({})}


async def find_recoverable_day(service, *, start, end):
    """Do not repeatedly replay suspensions or still-missing source data."""
    cursor = service.results.collection.find({
        'strategy_id': STRATEGY_ID, 'trade_date': {'$gte': start, '$lte': end},
        'status': 'closed', 'runtime.incomplete_code_count': {'$gt': 0},
    }, {'trade_date': 1, 'runtime.incomplete_codes': 1}).sort('trade_date', 1)
    async for row in cursor:
        codes = row.get('runtime', {}).get('incomplete_codes', [])
        if not codes:
            continue
        bars = await service._load_three_minute_bars(trade_date=row['trade_date'], codes=codes)
        if any(len(values) == 80 and values[0].previous_close is not None for values in bars.values()):
            return row['trade_date']
    return None


async def validate_replay(service, document, previous):
    internal = document['_runtime_state']
    opening = opening_flow_from_document(internal['opening_flow'])
    specs = [observation_spec_from_document(item) for item in internal['observation_specs']]
    codes = sorted({*(item.code for item in specs), *(h.code for h in opening.holdings),
                    *(item['code'] for item in internal['opening_pending_signals'])})
    bars = await service._load_three_minute_bars(trade_date=document['trade_date'], codes=codes)
    replayed = replay_live_day(opening_flow=opening, observation_specs=specs,
        opening_pending_signals=internal['opening_pending_signals'], bars_by_code=bars,
        expected_bar_count=80, close_market=True, opening_exit_states=internal['opening_exit_states'])
    assert [asdict(account) for account in replayed['flow'].accounts] == internal['accounts']
    assert replayed['exit_states'] == internal['exit_states']
    assert list(replayed['pending_signals']) == internal['pending_signals']
    assert sorted((dict(item) for item in replayed['signals']),
                  key=lambda item: (item.get('signal_at') or '', item['code'])) == document['signals']['items']
    rebuilt = daily_flow_document(replayed['flow'])
    for field in ('holding_pool', 'intraday_trading', 'closed_trades'):
        assert rebuilt[field] == document[field], field
    validate_chain(previous, document)
    previous_holdings = {item['code']: item for item in previous['holding_pool']['items']} if previous else {}
    assert {holding.code for holding in opening.holdings} == set(previous_holdings)
    for holding in opening.holdings:
        carried = previous_holdings[holding.code]
        for field, value in asdict(holding).items():
            expected = carried['mark_price'] if field == 'previous_close' else carried[field]
            assert value == expected, (holding.code, field)
    return {**validate_document(document), 'repeated_replay_matches': True}


async def recover(database, *, end: str, start: str | None = None, output: Path, apply=False,
                  validate_historical_reference=False):
    # The stricter offline reference-price recovery is explicit; scheduled
    # recovery continues to require a reference captured with the actual quote.
    service = (HistoricalReplayService if validate_historical_reference else QuantLiveService)(database)
    source = service.results.collection
    latest = await service.results.latest({'trade_date': 1, 'status': 1})
    if latest and (latest['trade_date'] > end or latest['status'] != 'closed'):
        return {'status': 'deferred', 'reason': 'newer_or_open_ledger_exists'}
    if start is None:
        sessions = get_a_share_calendar().sessions_in_range(
            date.fromisoformat(end) - timedelta(days=30), end)
        start = sessions[-5].strftime('%Y-%m-%d')
    first = await find_recoverable_day(service, start=start, end=end)
    if first is None:
        return {'status': 'unchanged', 'reason': 'no_repaired_partial_day'}
    # Every subsequent opening balance depends on this day's result, including
    # subsequent days that were previously complete.
    days = sorted(await source.distinct('trade_date', {
        'strategy_id': STRATEGY_ID, 'trade_date': {'$gte': first, '$lte': end}}))
    for day in days:
        try:
            await service._require_daily_sync_ready(day)
        except RuntimeError:
            return {'status': 'deferred', 'reason': 'daily_sync_not_ready', 'trade_date': day}
    previous = await service.results.latest_before(first)
    if previous is None:
        # Only the documented strategy inception can have no preceding ledger.
        # Its frozen opening must contain empty holdings and initial balances.
        validate_chain(None, await service.results.get(first))
    sessions = [d.strftime('%Y-%m-%d') for d in get_a_share_calendar().sessions_in_range(first, days[-1])]
    if days != sessions:
        raise RuntimeError('Recovery tail has missing trading days; recover missing ledgers first')
    output.mkdir(parents=True, exist_ok=False)
    stage_name = COLLECTION + '_recovery_' + uuid.uuid4().hex
    staged = database[stage_name]
    before = {}
    original_versions = {}
    original_first = None
    async for row in source.find({}):
        before[document_key(row)] = digest(row)
        await staged.insert_one(row)
        if row.get('strategy_id') == STRATEGY_ID and row.get('trade_date') in days:
            original_versions[row['trade_date']] = row.get('runtime', {}).get('version', 0)
            save_gzip(output / (row['trade_date'] + '_before.json.gz'), row)
            if row['trade_date'] == first:
                original_first = row
    (output / 'source_manifest.json').write_text(json.dumps(before, indent=2))
    save_gzip(output / 'preceding_ledger.json.gz', previous)
    # Preserve the first day's frozen inputs. Later observation pools must be
    # rebuilt against the corrected preceding holdings and pending signals.
    await staged.delete_many({'strategy_id': STRATEGY_ID, 'trade_date': {'$gt': first, '$lte': end}})
    original_first['status'] = 'monitoring'
    original_first['_runtime_state']['snapshot_key'] = None
    original_first['recording'].update(mode='historical_replay', recovery_reason='minute_backfill')
    service.results = QuantDailyResultRepository(database, collection_name=stage_name)
    await service.ensure_indexes()
    await service.results.save_document(original_first)
    validations = []
    for day in days:
        result = await service.process(now=datetime.fromisoformat(day + 'T15:10:00+08:00'))
        result['recording'].update(mode='historical_replay', recovery_reason='minute_backfill',
            computed_at=datetime.now(CN_TZ).isoformat(), recovery_output=str(output),
            reference_price_method=('captured_or_validated_daily_change_reference'
                                    if validate_historical_reference else 'observed_quote_reference'))
        result['runtime']['version'] = max(result['runtime']['version'], original_versions[day] + 1)
        validation = await validate_replay(service, result, previous)
        if validate_historical_reference:
            save_gzip(output / (day + '_reference_audit.json.gz'), service.reference_audit)
            validation['reference_counts'] = dict(Counter(
                row['method'] for row in service.reference_audit.values()))
        validations.append(validation)
        await service.results.save_document(result)
        save_gzip(output / (day + '_after.json.gz'), result)
        (output / 'progress.json').write_text(json.dumps(validations, ensure_ascii=False, indent=2))
        previous = result
        print(json.dumps({'trade_date': day, 'incomplete_codes': validation['incomplete_codes'],
                          'validated': True}), flush=True)
    report = {'status': 'validated', 'days': days, 'stage_collection': stage_name,
              'validation': validations, 'applied': False}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if apply:
        if await manifest(source) != before:
            raise RuntimeError('Production ledger changed during recovery; publication cancelled')
        # Atomic namespace replacement: readers see the old complete chain or
        # the new complete chain. Backups above preserve the replaced originals.
        await staged.rename(COLLECTION, dropTarget=True)
        report.update(status='applied', applied=True, applied_at=datetime.now(CN_TZ).isoformat())
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


async def run(args):
    now = datetime.now(CN_TZ)
    if args.end_date > now.date() or args.end_date == now.date() and now.time() < time(15, 20):
        raise ValueError('Recovery only accepts completed sessions')
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        with recovery_lock():
            result = await recover(client[settings.mongo_db_name], end=args.end_date.isoformat(),
                start=args.start_date.isoformat() if args.start_date else None,
                output=args.output, apply=args.apply,
                validate_historical_reference=args.validate_historical_reference)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--end-date', type=date.fromisoformat, required=True)
    parser.add_argument('--start-date', type=date.fromisoformat)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / '.local/quant_recovery' /
                        (datetime.now(CN_TZ).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--validate-historical-reference', action='store_true',
                        help='Offline only: recover static references with daily/minute price-basis checks')
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
