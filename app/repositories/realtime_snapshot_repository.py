"""Durable compressed quote batches, with local write-ahead spooling.

One document per poll avoids millions of per-symbol index entries per day.
There is no TTL. A pending file is removed only after Mongo acknowledges the
same immutable batch ID; retries cannot duplicate a batch or lose its receipt time.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import uuid

from bson import Binary
from pymongo.errors import PyMongoError

from app.core.config import PROJECT_ROOT
from app.crawlers.realtime_market_crawler import CN_TZ, quote_issue, quote_phase
from app.repositories.base import BaseMongoRepository

logger = logging.getLogger(__name__)
SNAPSHOT_COLLECTION = 'stock_realtime_quote_batches'
SPOOL_ROOT = PROJECT_ROOT / '.local' / 'realtime_quote_spool'


def _safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def snapshot_row(quote):
    row = _safe(asdict(quote))
    row['quality_issue'] = quote_issue(quote)
    row['phase'] = quote_phase(quote.market_data_time) if quote.market_data_time else 'unknown'
    # The public feed's auction encoding has to be verified against an actual
    # auction before treating its book quantities as matched/unmatched orders.
    row['auction_semantics'] = 'raw_book_unverified' if 'auction' in row['phase'] else None
    return row


def decode_batch(document):
    payload = bytes(document['payload'])
    if hashlib.sha256(payload).hexdigest() != document['sha256']:
        raise ValueError('realtime snapshot checksum mismatch')
    return json.loads(gzip.decompress(payload))


class RealtimeSnapshotRepository(BaseMongoRepository):
    collection_name = SNAPSHOT_COLLECTION

    def __init__(self, database=None, *, spool_root=SPOOL_ROOT):
        super().__init__(database)
        self.spool_root = Path(spool_root)

    async def create_indexes(self):
        await self.collection.create_index([('trade_date', 1), ('observed_at', 1)],
                                           name='idx_quote_day_observed')
        await self.collection.create_index([('trade_date', 1), ('phases', 1), ('observed_at', 1)],
                                           name='idx_quote_day_phase_observed')

    def _spool(self, envelope):
        self.spool_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.spool_root).free
        if free < 10 * 1024 ** 3:
            logger.error('realtime_snapshot_disk_low free_bytes=%s action=expand_storage', free)
        payload = gzip.compress(json.dumps(envelope, ensure_ascii=False, separators=(',', ':'),
                                            allow_nan=False).encode(), compresslevel=3, mtime=0)
        path = self.spool_root / (envelope['batch_id'] + '.json.gz')
        temp = path.with_suffix('.tmp')
        with temp.open('wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        directory = os.open(self.spool_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return path

    @staticmethod
    def _load(path):
        payload = path.read_bytes()
        envelope = json.loads(gzip.decompress(payload))
        quotes = envelope['quotes']
        observed = datetime.fromisoformat(envelope['observed_at'])
        return {
            '_id': envelope['batch_id'], 'trade_date': observed.astimezone(CN_TZ).date().isoformat(),
            'observed_at': observed, 'received_at': datetime.fromisoformat(envelope['received_at']),
            'session': envelope['session'], 'phases': sorted({q['phase'] for q in quotes}),
            'clock_offset_seconds': envelope['clock_offset_seconds'],
            'requested': len(envelope['expected_codes']), 'returned': len({q['code'] for q in quotes}),
            'usable': len({q['code'] for q in quotes if not q['quality_issue']}),
            'missing_codes': sorted(set(envelope['expected_codes']) - {q['code'] for q in quotes}),
            'quality_issue_count': sum(bool(q['quality_issue']) for q in quotes),
            'metrics': envelope['metrics'], 'codec': 'json-gzip-v1',
            'sha256': hashlib.sha256(payload).hexdigest(), 'compressed_bytes': len(payload),
            'payload': Binary(payload),
        }

    async def _persist(self, path):
        document = await asyncio.to_thread(self._load, path)
        async with asyncio.timeout(3):
            await self.collection.update_one({'_id': document['_id']}, {'$setOnInsert': document}, upsert=True)
        await asyncio.to_thread(path.unlink)
        return document['compressed_bytes']

    async def replay_pending(self, *, limit=3):
        paths = await asyncio.to_thread(lambda: sorted(self.spool_root.glob('*.json.gz'))[:limit])
        count = 0
        for path in paths:
            try:
                await self._persist(path)
                count += 1
            except (PyMongoError, TimeoutError) as exc:
                logger.error('realtime_snapshot_replay_deferred error=%s', type(exc).__name__)
                break
        return count

    async def save_cycle(self, quotes, *, expected_codes, session, observed_at, received_at, metrics):
        envelope = {
            'version': 1, 'batch_id': received_at.strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex,
            'observed_at': observed_at.isoformat(), 'received_at': received_at.isoformat(),
            'clock_offset_seconds': (observed_at - received_at).total_seconds(), 'session': session,
            'expected_codes': expected_codes, 'metrics': metrics,
            'quotes': [snapshot_row(q) for q in quotes],
        }
        path = await asyncio.to_thread(self._spool, envelope)
        try:
            size = await self._persist(path)
            await self.replay_pending(limit=2)
            return {'persisted': True, 'compressed_bytes': size}
        except (PyMongoError, TimeoutError) as exc:
            logger.error('realtime_snapshot_spooled batch=%s error=%s', path.name, type(exc).__name__)
            return {'persisted': False, 'pending_file': path.name}
