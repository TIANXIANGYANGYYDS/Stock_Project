"""Audit all A shares and fill absent 1m observations from the THS webpage.

Original rows are insert-only protected. Website minutes are actual/unadjusted,
end-labelled; our collection uses start-labelled minutes. The extra opening
auction point is archived but never shifted into the continuous-session grid.
Backfills carry their real retrieval time, never a simulated historical arrival.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import threading
import time

import requests
from pymongo import MongoClient, UpdateOne

from app.core.config import get_settings
from app.manually_execute_script.stock_history_common import CN_TZ, StockTarget, market_for_code
from app.manually_execute_script.sync_ths_2026_shadow import discover_ths_market_id
from app.manually_execute_script.validate_stock_history_against_ths import (
    discover_ths_direct_headers, fetch_ths_direct_bars, THS_DIRECT_KLINE_URL,
)

COLLECTION = 'stock_realtime_minute_bars'
PROVIDER = 'THS_WEB_ACTUAL_BACKFILL'
VERSION = 'ths-minute-repair-20261007.3'
INDEX = 'idx_realtime_trade_date_interval_timestamp_code'


def connect():
    """基础补数直接复用项目数据库配置，不依赖情绪策略研究模块。"""
    settings = get_settings()
    client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=10000,
                         connectTimeoutMS=10000, socketTimeoutMS=120000,
                         maxPoolSize=2, tz_aware=True)
    return client, client[settings.mongo_db_name]


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temp.replace(path)


def minute_starts(day):
    return {f'{day}T{m//60:02}:{m%60:02}:00+08:00'
            for lo, hi in ((570, 690), (780, 900)) for m in range(lo, hi)}


def normalize(row, *, day, name, retrieved_at, run_id, response_sha256):
    end = datetime.fromisoformat(row['key']).astimezone(CN_TZ)
    start = (end - timedelta(minutes=1)).isoformat(timespec='seconds')
    if row.get('code') is None or start not in minute_starts(day):
        return None
    values = {key: float(row[key]) for key in ('open', 'high', 'low', 'close', 'volume', 'amount')}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('nonfinite_ohlcv')
    if (min(values[k] for k in ('open', 'high', 'low', 'close')) <= 0
            or min(values['volume'], values['amount']) < 0
            or values['low'] > min(values['open'], values['close'])
            or values['high'] < max(values['open'], values['close'])):
        raise ValueError('invalid_ohlcv')
    return dict(code=row['code'], name=name, market=market_for_code(row['code']),
                trade_date=day, interval='1m', timestamp=start, **values,
                previous_close=None, provider=PROVIDER, revision_count=0,
                first_seen_at=retrieved_at, last_seen_at=retrieved_at,
                created_at=retrieved_at, updated_at=retrieved_at,
                source_kind='historical_backfill', adjust='none', volume_unit='share',
                source_timestamp=end.isoformat(), source_endpoint=THS_DIRECT_KLINE_URL,
                source_response_sha256=response_sha256, repair_run_id=run_id,
                repair_version=VERSION, availability_basis='retrieved_at_not_historical_receipt')


def insert_missing(collection, docs):
    operations = [UpdateOne({k: row[k] for k in ('code', 'interval', 'timestamp')},
                            {'$setOnInsert': row}, upsert=True) for row in docs]
    return collection.bulk_write(operations, ordered=False).upserted_count if operations else 0


def insert_missing_aggregates(collection, code, day):
    """Build absent larger bars only from a complete 1m constituent grid."""
    from app.services.realtime_minute_service import RealtimeMinuteService, AGGREGATE_INTERVAL_MINUTES
    rows = list(collection.find({'code': code, 'trade_date': day, 'interval': '1m'}, {'_id': 0}))
    by_time = {row['timestamp']: row for row in rows if row['timestamp'] in minute_starts(day)}
    now = datetime.now(CN_TZ)
    documents = []
    for minutes in AGGREGATE_INTERVAL_MINUTES:
        buckets = {}
        for timestamp, row in by_time.items():
            bucket = RealtimeMinuteService._period_bucket_start(datetime.fromisoformat(timestamp), minutes)
            buckets.setdefault(bucket, []).append(row)
        for timestamp, parts in buckets.items():
            if len(parts) != minutes:
                continue
            parts.sort(key=lambda row: row['timestamp'])
            doc = dict(parts[0])
            doc.update(interval=f'{minutes}m', timestamp=timestamp,
                       high=max(row['high'] for row in parts), low=min(row['low'] for row in parts),
                       close=parts[-1]['close'], volume=sum(row['volume'] for row in parts),
                       amount=sum(row['amount'] for row in parts), provider='ONE_MINUTE_ROLLUP',
                       source_kind='reconstructed_from_complete_1m',
                       constituent_providers=sorted({row['provider'] for row in parts}),
                       first_seen_at=now, last_seen_at=now, created_at=now, updated_at=now,
                       constituent_count=len(parts), revision_count=0)
            documents.append(doc)
    return insert_missing(collection, documents)


def prior_universe(collection, start):
    """A suspension must not remove a previously observed stock from audit.

    Code enumeration only locates keys; names/existence are read strictly
    before start. Stocks first observed later enter on their own day.
    """
    names = {}
    for code in collection.distinct('code'):
        row = collection.find_one(
            {'code': code, 'trade_date': {'$lt': start}},
            {'_id': 0, 'code': 1, 'name': 1}, sort=[('trade_date', -1)],
            hint='uniq_code_trade_date_adjust',
        )
        if row:
            names[code] = row.get('name')
    return names


def inventory(db, root, start, end):
    date_filter = {'trade_date': {'$gte': start, '$lte': end}}
    days = sorted(set(db.stock_daily_detail.distinct('trade_date', date_filter)) |
                  set(db[COLLECTION].distinct('trade_date', dict(date_filter, interval='1m'))))
    names = prior_universe(db.stock_daily_detail, start)
    tasks, summaries = [], []
    for day in days:
        daily_names = {x['code']: x.get('name') for x in db.stock_daily_detail.find(
            {'trade_date': day}, {'code': 1, 'name': 1, '_id': 0}).hint('idx_trade_date_code')}
        names.update(daily_names)
        path = root / f'inventory_{day}.json'
        # Always re-read Mongo: cached inventories omit late-listed stocks.
        observed = {x['_id']: x['timestamps'] for x in db[COLLECTION].aggregate([
            {'$match': {'trade_date': day, 'interval': '1m'}},
            {'$group': {'_id': '$code', 'timestamps': {'$push': '$timestamp'}}},
        ], hint=INDEX, allowDiskUse=True)}
        expected = minute_starts(day)
        gaps = []
        for code in sorted(set(names) | set(observed)):
            missing = sorted(expected - set(observed.get(code, [])))
            if missing:
                gaps.append(dict(code=code, name=names.get(code), day=day, missing=missing))
        summary = dict(day=day, symbols=len(set(names) | set(observed)),
                       daily_symbols=len(daily_names),
                       daily_symbols_with_gaps=sum(g['code'] in daily_names for g in gaps),
                       daily_missing_minutes=sum(len(g['missing']) for g in gaps if g['code'] in daily_names),
                       stock_days_with_gaps=len(gaps), missing=sum(len(g['missing']) for g in gaps),
                       whole_day=sum(len(g['missing']) == 240 for g in gaps),
                       observed=sum(len(set(ts) & expected) for ts in observed.values()))
        data = dict(summary=summary, gaps=gaps)
        write_json(path, data)
        summaries.append(data['summary']); tasks.extend(data['gaps'])
        print('inventory', data['summary'], flush=True)
    write_json(root / 'inventory.json', summaries)
    return tasks


class Downloader:
    def __init__(self, root, headers, db, apply):
        self.root, self.headers, self.db, self.apply = root, headers, db, apply
        self.local = threading.local()
        self.market_ids = {}
        self.lock = threading.Lock()

    def __call__(self, task):
        code, day = task['code'], task['day']
        result_path = self.root / 'results' / f'{day}_{code}.json'
        result = dict(code=code, day=day, missing_before=len(task['missing']), applied=self.apply)
        try:
            if not hasattr(self.local, 'session'):
                self.local.session = requests.Session()
            session = self.local.session
            with self.lock:
                market_id = self.market_ids.get(code)
            if market_id is None:
                market_id, _ = discover_ths_market_id(session,
                    target=StockTarget(code, task['name'], market_for_code(code)), headers=self.headers)
                with self.lock:
                    self.market_ids[code] = market_id
            cutoff = int(datetime.fromisoformat(day + 'T15:00:00+08:00').timestamp() * 1000)
            # An explicitly bounded request; latest/future candles are never
            # accepted as substitutes for the requested historical day.
            rows, audit = fetch_ths_direct_bars(session, headers=self.headers, code=code,
                market=market_id, time_period='min_1', end_time_ms=cutoff,
                adjust_type='actual', count=241, allow_partial=True, max_attempts=3,
                retry_delay=0.5)
            retrieved = datetime.now(CN_TZ)
            raw = json.dumps(dict(rows=rows, audit=audit, market_id=market_id,
                                 retrieved_at=retrieved.isoformat()), ensure_ascii=False).encode()
            digest = hashlib.sha256(raw).hexdigest()
            with gzip.open(self.root / 'responses' / f'{day}_{code}.json.gz', 'wb') as stream:
                stream.write(raw)
            result.update(response_sha256=digest, audit=audit, retrieved_at=retrieved.isoformat())
            if rows is None:
                result.update(status='source_unresolved', inserted=0, resolved=0)
            else:
                required = set(task['missing']); docs = []
                for row in rows:
                    doc = normalize(row, day=day, name=task['name'], retrieved_at=retrieved,
                                    run_id=self.root.name, response_sha256=digest)
                    if doc and doc['timestamp'] in required:
                        docs.append(doc)
                inserted = insert_missing(self.db[COLLECTION], docs) if self.apply else 0
                aggregate_inserted = insert_missing_aggregates(self.db[COLLECTION], code, day) if self.apply and docs else 0
                result.update(status='checked', source_rows=len(rows), usable_missing=len(docs),
                              inserted=inserted, resolved=len(docs),
                              aggregate_inserted=aggregate_inserted,
                              remaining=sorted(required - {d['timestamp'] for d in docs}))
        except Exception as exc:
            result.update(status='error', error_type=type(exc).__name__, inserted=0, resolved=0)
        write_json(result_path, result)
        time.sleep(0.10)  # Bound load even for fast HTTP responses.
        return result


def run(*, start, end, output, apply=False, workers=6):
    now = datetime.now(CN_TZ)
    if (start > end or end > now.date().isoformat()
            or (end == now.date().isoformat() and (now.hour, now.minute) < (15, 15))):
        raise ValueError('only completed historical days can be repaired')
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    for folder in ('responses', 'results'):
        (root / folder).mkdir(exist_ok=True)
    spec = dict(version=VERSION, start=start, end=end, scope='all A shares SH SZ BJ',
                universe='all previously observed daily codes carried forward plus daily and observed codes',
                writes='missing 1m and complete-rollup keys only; existing rows unchanged',
                adjustment='actual', timestamp='minute end minus 1 minute; auction excluded')
    if (root / 'protocol.json').exists() and json.loads((root / 'protocol.json').read_text()) != spec:
        raise ValueError('output protocol mismatch')
    write_json(root / 'protocol.json', spec)
    client, db = connect()
    try:
        tasks = inventory(db, root, start, end)
        with requests.Session() as session:
            headers, evidence = discover_ths_direct_headers(session, code='601086')
        write_json(root / 'discovery.json', evidence)
        worker = Downloader(root, headers, db, apply)
        results = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
            for result in executor.map(worker, tasks):
                results.append(result)
                if len(results) % 100 == 0:
                    print('progress', len(results), '/', len(tasks), 'inserted', sum(x['inserted'] for x in results), flush=True)
        summary = dict(tasks=len(tasks), original_missing=sum(len(t['missing']) for t in tasks),
                       inserted=sum(x['inserted'] for x in results),
                       aggregate_inserted=sum(x.get('aggregate_inserted', 0) for x in results),
                       source_resolved=sum(x['resolved'] for x in results),
                       source_unresolved=sum(x['status'] != 'checked' or bool(x.get('remaining')) for x in results),
                       remaining_missing=sum(len(x.get('remaining', [])) if x['status'] == 'checked'
                                             else x['missing_before'] for x in results),
                       applied=apply, finished_at=datetime.now(CN_TZ).isoformat())
        if apply:
            # Verify actual stored keys after writes, including late universe
            # changes. HTTP success/attempt counts are not a coverage audit.
            inventory(db, root, start, end)
            quality = db['stock_realtime_minute_quality']
            quality.create_index('trade_date', unique=True)
            for item in json.loads((root / 'inventory.json').read_text()):
                quality.update_one({'trade_date': item['day']}, {'$set': {
                    **item, 'audited_at': datetime.now(CN_TZ), 'report_path': str(root),
                    'coverage_basis': 'observed_1m_keys_not_tick_or_ohlc_accuracy',
                    'unclassified_gaps_are_not_assumed_suspended': True,
                }}, upsert=True)
        write_json(root / 'summary.json', summary)
        print('finished', summary, flush=True)
        return summary
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(start=args.start, end=args.end, output=args.output, apply=args.apply, workers=args.workers)


if __name__ == '__main__':
    main()
