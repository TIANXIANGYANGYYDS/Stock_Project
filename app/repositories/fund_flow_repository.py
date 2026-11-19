"""Five-session compressed observations and independent permanent daily rows."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import uuid

from pymongo import UpdateOne

from app.core.config import PROJECT_ROOT
from app.crawlers.realtime_market_crawler import CN_TZ
from app.repositories.market_observation_repository import FUND_FLOW_COLLECTION, RUN_COLLECTION
from app.repositories.realtime_snapshot_repository import (
    RealtimeSnapshotRepository, SOURCE_CURSOR_INDEX, _safe, decode_batch, encode_batch,
    symbol_projection,
)

DAILY_COLLECTION = 'stock_fund_flow_daily'
DAILY_RUN_KIND = 'fund_flow_daily'
SPOOL_ROOT = PROJECT_ROOT / '.local' / 'fund_flow_spool'
logger = logging.getLogger(__name__)


def as_datetime(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def fund_symbol_projection(code):
    return {**symbol_projection(code), 'records': {'$elemMatch': {'code': code}},
            'trade_date': 1, 'fetched_at': 1, 'source': 1, 'currency': 1,
            'collection_status': 1, 'complete': 1, 'run_mode': 1, 'scheduled_slot': 1}


def decode_fund_snapshot(document, code=None):
    """Support earlier uncompressed observations without rewriting their times."""
    if 'codec' not in document:
        result = dict(document)
    else:
        envelope = decode_batch(document)
        result = dict(envelope['metadata'], observed_at=envelope['observed_at'],
                      records=envelope['quotes'])
    if code is not None:
        result['records'] = [r for r in result.get('records', []) if r['code'] == code]
    return result


def daily_rows(data, day):
    """Retain the close's cumulative value; summing intraday snapshots is wrong."""
    close = datetime.fromisoformat(day+'T15:00:00+08:00')
    observed = as_datetime(data['observed_at'])
    if observed < close or observed.astimezone(CN_TZ).date().isoformat() != day:
        raise ValueError('fund_flow_daily_requires_same_day_post_close_observation')
    rows = []
    for row in data['records']:
        stamp = row.get('quote_time')
        if not row.get('usable') or not stamp:
            continue
        quote_time = as_datetime(stamp)
        row_observed = as_datetime(row.get('observed_at') or observed)
        if (quote_time < close or quote_time > row_observed
                or quote_time.astimezone(CN_TZ).date().isoformat() != day or row_observed < close):
            continue
        rows.append({**row, 'trade_date': day, 'source': data['source'],
                     'currency': data.get('currency', 'CNY'), 'metric': data['metric'],
                     'quote_time': quote_time, 'observed_at': row_observed,
                     'fetched_at': as_datetime(data['fetched_at']), 'fund_flow_time': None,
                     'finality': 'post_close_observation', 'is_exchange_final': False})
    return rows


class FundFlowRepository(RealtimeSnapshotRepository):
    collection_name = FUND_FLOW_COLLECTION
    raw_collections = (FUND_FLOW_COLLECTION,)

    def __init__(self, database=None, *, spool_root=SPOOL_ROOT, background_upload=False):
        super().__init__(database, spool_root=spool_root)
        self.background_upload = background_upload
        self._upload = None
        self._upload_requested = False

    async def create_indexes(self):
        await self.collection.create_index([('trade_date', 1), ('observed_at', 1), ('_id', 1)],
                                           name=SOURCE_CURSOR_INDEX)
        await self.database[DAILY_COLLECTION].create_index([('code', 1), ('trade_date', -1)])
        await self.database[DAILY_COLLECTION].create_index(
            [('trade_date', 1), ('code', 1), ('source', 1)], unique=True)
        await self.database[RUN_COLLECTION].create_index([('kind', 1), ('finished_at', -1)])

    @staticmethod
    def _load(path):
        import gzip
        import json
        envelope = json.loads(gzip.decompress(path.read_bytes()))
        metadata = dict(envelope['metadata'])
        for key in ('fetch_started_at', 'fetched_at'):
            metadata[key] = as_datetime(metadata[key])
        return {**metadata, '_id': envelope['batch_id'],
                'observed_at': as_datetime(envelope['observed_at']), **encode_batch(envelope)}

    def _target_collection(self, document):
        return FUND_FLOW_COLLECTION

    async def save_observation(self, data, *, clock_verified=True):
        metadata = {k: v for k, v in data.items() if k not in ('records', 'requested_codes', 'observed_at')}
        observed = data['observed_at']
        envelope = {'batch_id': observed.strftime('%Y%m%dT%H%M%S%f-') + uuid.uuid4().hex,
                    'observed_at': observed.isoformat(), 'metadata': _safe(metadata),
                    'quotes': await asyncio.to_thread(_safe, data['records'])}
        if not self.background_upload:
            return await self._save_envelope(envelope, clock_verified=clock_verified)
        path, cutoff = await self._stage_envelope(envelope, clock_verified=clock_verified)
        # 采样只等待本地暂存文件持久化。单个后台任务从磁盘回放，数据库变慢时不积压内存。
        self._upload_requested = True
        if self._upload is None or self._upload.done():
            self._upload = asyncio.create_task(self._upload_pending(cutoff))
        return {'spooled': True, 'persisted': None, 'batch_id': path.name.removesuffix('.json.gz')}

    async def _upload_pending(self, cutoff):
        try:
            while self._upload_requested:
                self._upload_requested = False
                if await self.replay_pending(limit=3, cutoff=cutoff) == 3:
                    self._upload_requested = True
        except Exception:
            logger.exception('fund_flow_upload_deferred')

    async def finish_upload(self):
        if self._upload is not None:
            await self._upload

    async def latest_after_close(self, day):
        start = datetime.fromisoformat(day+'T15:00:00+08:00')
        doc = await self.collection.find_one({'trade_date': day, 'observed_at': {'$gte': start}},
                                             sort=[('observed_at', -1)])
        return await asyncio.to_thread(decode_fund_snapshot, doc) if doc else None

    async def daily_complete(self, day):
        row = await self.database[RUN_COLLECTION].find_one({'_id': f'{DAILY_RUN_KIND}:{day}'})
        return bool(row and row.get('complete'))

    async def save_daily(self, data, day):
        rows = daily_rows(data, day)
        collection = self.database[DAILY_COLLECTION]
        if rows:
            await collection.bulk_write([UpdateOne(
                {'trade_date': day, 'code': row['code'], 'source': row['source']},
                {'$setOnInsert': row}, upsert=True) for row in rows], ordered=False)
        codes = data.get('requested_codes')
        filters = {'trade_date': day, 'source': data['source']}
        if codes:
            filters['code'] = {'$in': codes}
        stored = await collection.count_documents(filters)
        expected = data['requested_count']
        return {'trade_date': day, 'expected_count': expected, 'stored_count': stored,
                'eligible_count': len(rows), 'complete': stored == expected,
                'status': 'success' if stored == expected else 'partial'}

    async def record_run(self, kind, target, result):
        now = datetime.now(CN_TZ)
        updates = {**result, 'kind': kind, 'target': target, 'finished_at': now,
                   'error_type': result.get('error_type'), 'reason': result.get('reason')}
        counters = {'attempts': 1}
        if kind == 'fund_flow' and 'response_complete' in result:
            field = 'full_response_cycles' if result['response_complete'] else 'missing_response_cycles'
            counters[field] = 1
            if not result['count']:
                counters['empty_response_cycles'] = 1
        if result.get('status') in ('missed', 'partial', 'failed'):
            counters[result['status']+'_cycles'] = 1
            updates['last_issue_at'] = now
            updates['last_issue_reason'] = result.get('reason') or result.get('error_type') or result['status']
        await self.database[RUN_COLLECTION].update_one({'_id': f'{kind}:{target}'},
            {'$set': updates, '$inc': counters}, upsert=True)
