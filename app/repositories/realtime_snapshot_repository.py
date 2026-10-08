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
AUCTION_COLLECTION = 'stock_realtime_auction_batches'
SPOOL_ROOT = PROJECT_ROOT / '.local' / 'realtime_quote_spool'
PARTITIONED_CODEC = 'json-gzip-partitioned-v2'


def snapshot_kind(observed_at: datetime) -> str:
    """Route by calibrated capture time, including empty/invalid batches.

    A stale quote's source phase cannot turn a continuous-session poll into
    auction data. Individual source phases/timestamps remain in the payload.
    """
    if observed_at.tzinfo is None:
        raise ValueError('snapshot observation time must include timezone')
    phase = quote_phase(observed_at)
    if phase in {'opening_auction_cancelable', 'opening_auction_locked',
                 'opening_result', 'closing_auction'}:
        return 'auction'
    # Include the closing uncross at precisely 15:00:00.
    if observed_at.astimezone(CN_TZ).strftime('%H:%M:%S') == '15:00:00':
        return 'auction'
    return 'snapshot'


def snapshot_collection(kind: str) -> str:
    if kind not in {'auction', 'snapshot'}:
        raise ValueError('unknown snapshot kind')
    return AUCTION_COLLECTION if kind == 'auction' else SNAPSHOT_COLLECTION


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


def _decode_checked(payload, checksum):
    payload = bytes(payload)
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError('realtime snapshot checksum mismatch')
    return json.loads(gzip.decompress(payload))


def encode_batch(envelope):
    """Partition by code suffix so a symbol query reads about 1% of quotes.

    Keep original row positions: duplicate providers and their ordering are
    evidence, not rows to deduplicate. The on-disk WAL stays in the old format.
    """
    groups = {}
    for index, row in enumerate(envelope['quotes']):
        groups.setdefault(row['code'][-2:], []).append([index, row])

    def compress(value):
        return gzip.compress(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                        allow_nan=False).encode(), compresslevel=3, mtime=0)

    chunks = {key: Binary(compress(rows)) for key, rows in groups.items()}
    header = {key: value for key, value in envelope.items() if key != 'quotes'}
    payload = compress(header)
    return {'codec': PARTITIONED_CODEC, 'payload': Binary(payload),
            'sha256': hashlib.sha256(payload).hexdigest(), 'quote_chunks': chunks,
            'chunk_sha256': {key: hashlib.sha256(value).hexdigest() for key, value in chunks.items()},
            'compressed_bytes': len(payload) + sum(map(len, chunks.values()))}


def decode_batch(document):
    envelope = _decode_checked(document['payload'], document['sha256'])
    if document.get('codec', 'json-gzip-v1') == PARTITIONED_CODEC:
        rows = []
        for key, checksum in document['chunk_sha256'].items():
            rows.extend(_decode_checked(document['quote_chunks'][key], checksum))
        envelope['quotes'] = [row for _, row in sorted(rows, key=lambda item: item[0])]
    return envelope


def symbol_projection(code):
    key = code[-2:]
    return {'_id': 0, 'observed_at': 1, 'codec': 1, 'payload': 1, 'sha256': 1,
            f'quote_chunks.{key}': 1, f'chunk_sha256.{key}': 1}


def decode_symbol_batch(document, code):
    """Read projected v2 batches and full legacy batches during migration."""
    if document.get('codec', 'json-gzip-v1') != PARTITIONED_CODEC:
        envelope = decode_batch(document)
    else:
        envelope = _decode_checked(document['payload'], document['sha256'])
        key = code[-2:]
        checksums = document.get('chunk_sha256', {})
        rows = (_decode_checked(document['quote_chunks'][key], checksums[key])
                if key in checksums else [])
        envelope['quotes'] = [row for _, row in rows]
    return [dict(row, observed_at=envelope['observed_at'])
            for row in envelope['quotes'] if row['code'] == code]


def decode_symbol_batches(documents, code):
    return [row for document in documents for row in decode_symbol_batch(document, code)]


class RealtimeSnapshotRepository(BaseMongoRepository):
    collection_name = SNAPSHOT_COLLECTION

    def __init__(self, database=None, *, spool_root=SPOOL_ROOT):
        super().__init__(database)
        self.spool_root = Path(spool_root)

    async def create_indexes(self):
        for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION):
            collection = self.database[name]
            await collection.create_index([('trade_date', 1), ('observed_at', 1)],
                                           name='idx_quote_day_observed')
            await collection.create_index([('trade_date', 1), ('phases', 1), ('observed_at', 1)],
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
        # Derive for both v1 pending files and new v2 files. Routing must not
        # depend on today's date or whether an upstream returned any quotes.
        kind = snapshot_kind(observed)
        return {
            '_id': envelope['batch_id'], 'trade_date': observed.astimezone(CN_TZ).date().isoformat(),
            'data_kind': kind,
            'observed_at': observed, 'received_at': datetime.fromisoformat(envelope['received_at']),
            'session': envelope['session'], 'phases': sorted({q['phase'] for q in quotes}),
            'clock_offset_seconds': envelope['clock_offset_seconds'],
            'requested': len(envelope['expected_codes']), 'returned': len({q['code'] for q in quotes}),
            'usable': len({q['code'] for q in quotes if not q['quality_issue']}),
            'missing_codes': sorted(set(envelope['expected_codes']) - {q['code'] for q in quotes}),
            'quality_issue_count': sum(bool(q['quality_issue']) for q in quotes),
            'metrics': envelope['metrics'], **encode_batch(envelope),
        }

    async def _persist(self, path):
        document = await asyncio.to_thread(self._load, path)
        async with asyncio.timeout(3):
            collection = self.database[snapshot_collection(document['data_kind'])]
            await collection.update_one({'_id': document['_id']}, {'$setOnInsert': document}, upsert=True)
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
            'version': 2, 'data_kind': snapshot_kind(observed_at),
            'batch_id': received_at.strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex,
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
