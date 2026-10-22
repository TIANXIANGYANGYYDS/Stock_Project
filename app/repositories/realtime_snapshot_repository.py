"""Durable compressed quote batches, with local write-ahead spooling.

One document per poll avoids millions of per-symbol index entries per day.
Raw batches retain five trading sessions using a calibrated-clock job, not
MongoDB's host-clock TTL. Unexpired pending files are removed only after Mongo
acknowledges their immutable batch IDs.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
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
from app.services.trading_calendar_service import get_a_share_calendar

logger = logging.getLogger(__name__)
SNAPSHOT_COLLECTION = 'stock_realtime_quote_batches'
AUCTION_COLLECTION = 'stock_realtime_auction_batches'
SPOOL_ROOT = PROJECT_ROOT / '.local' / 'realtime_quote_spool'
PARTITIONED_CODEC = 'json-gzip-partitioned-v2'
QUALITY_INDEX_NAME = 'idx_quote_quality_metadata'
QUALITY_INDEX_FIELDS = ('trade_date', 'session', 'observed_at', 'requested',
                        'returned', 'usable', 'quality_issue_count', 'compressed_bytes')
SNAPSHOT_RETENTION_SESSIONS = 5
SOURCE_CURSOR_INDEX = 'idx_short_sentiment_source_cursor'


@lru_cache(maxsize=8)
def snapshot_retained_sessions(latest_trade_date):
    calendar = get_a_share_calendar()
    if not calendar.is_session(latest_trade_date):
        raise ValueError('snapshot retention anchor must be a trading session')
    session = calendar.date_to_session(latest_trade_date)
    sessions = [session.strftime('%Y-%m-%d')]
    for _ in range(SNAPSHOT_RETENTION_SESSIONS - 1):
        session = calendar.previous_session(session)
        sessions.append(session.strftime('%Y-%m-%d'))
    return tuple(reversed(sessions))


def snapshot_retention_cutoff(latest_trade_date):
    oldest = snapshot_retained_sessions(latest_trade_date)[0]
    return datetime.fromisoformat(oldest).replace(tzinfo=CN_TZ)


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
        self._pending_cleanup_cutoff = None

    async def create_indexes(self):
        for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION):
            collection = self.database[name]
            await collection.create_index([('trade_date', 1), ('observed_at', 1)],
                                           name='idx_quote_day_observed')
            await collection.create_index([('trade_date', 1), ('phases', 1), ('observed_at', 1)],
                                           name='idx_quote_day_phase_observed')
            await collection.create_index([(field, 1) for field in QUALITY_INDEX_FIELDS],
                                           name=QUALITY_INDEX_NAME)
            await collection.create_index([('trade_date', 1), ('observed_at', 1), ('_id', 1)],
                                           name=SOURCE_CURSOR_INDEX)

    async def retention_window(self, calibrated_now):
        if calibrated_now.tzinfo is None or calibrated_now.utcoffset() is None:
            raise ValueError('snapshot retention requires a timezone-aware calibrated time')
        today = calibrated_now.astimezone(CN_TZ).date().isoformat()
        # Validate calendar coverage before reading or deleting anything.
        get_a_share_calendar().date_to_session(today, direction='previous')
        rows = await asyncio.gather(*[
            self.database[name].find_one(
                {'trade_date': {'$lte': today}, 'observed_at': {'$lte': calibrated_now}},
                {'trade_date': 1}, sort=[('trade_date', -1), ('observed_at', -1)])
            for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)])
        dates = [row['trade_date'] for row in rows if row]
        if not dates:
            return None
        latest = max(dates)
        return {'latest_trade_date': latest, 'retained_trade_dates': list(snapshot_retained_sessions(latest)),
                'cutoff': snapshot_retention_cutoff(latest)}

    async def delete_expired(self, *, calibrated_now):
        """Keep five whole trading sessions ending at the latest captured day."""
        window = await self.retention_window(calibrated_now)
        if window is None:
            return {'status': 'skipped', 'reason': 'no_observations'}
        cutoff = window['cutoff']
        pending = await asyncio.to_thread(self._delete_expired_pending, cutoff)
        counts = {}
        # This allowlist deliberately excludes minute bars and all derived data.
        query = {'trade_date': {'$lt': window['retained_trade_dates'][0]},
                 'observed_at': {'$lt': cutoff}}
        for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION):
            collection = self.database[name]
            counts[name] = 0
            while True:
                rows = await collection.find(query, {'_id': 1}).hint(SOURCE_CURSOR_INDEX).limit(100).to_list(length=100)
                if not rows:
                    break
                result = await collection.delete_many({**query, '_id': {'$in': [r['_id'] for r in rows]}})
                counts[name] += result.deleted_count
                # Yield between bounded batches so collectors/readers can run.
                await asyncio.sleep(0.05)
            logger.info('realtime_snapshot_retention collection=%s cutoff=%s deleted=%s',
                        name, cutoff.isoformat(), counts[name])
        return {**window, 'status': 'success', 'cutoff': cutoff.isoformat(),
                'retention_trading_days': SNAPSHOT_RETENTION_SESSIONS,
                'deleted': counts, 'pending_files_deleted': pending}

    def _delete_expired_pending(self, cutoff):
        count = 0
        for path in self.spool_root.glob('*.json.gz'):
            try:
                envelope = json.loads(gzip.decompress(path.read_bytes()))
                observed = datetime.fromisoformat(envelope['observed_at'])
                if observed.tzinfo is None:
                    raise ValueError('missing observation timezone')
                if observed < cutoff:
                    path.unlink()
                    count += 1
            except FileNotFoundError:
                pass  # The collector acknowledged/replayed the same file.
            except (OSError, EOFError, ValueError, KeyError, TypeError):
                logger.exception('realtime_snapshot_retention_invalid_pending file=%s', path.name)
        return count

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

    async def _persist(self, path, *, cutoff=None):
        document = await asyncio.to_thread(self._load, path)
        if cutoff is not None and document['observed_at'] < cutoff:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            logger.info('realtime_snapshot_pending_expired batch=%s', path.name)
            return 0
        async with asyncio.timeout(3):
            collection = self.database[snapshot_collection(document['data_kind'])]
            await collection.update_one({'_id': document['_id']}, {'$setOnInsert': document}, upsert=True)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        return document['compressed_bytes']

    async def replay_pending(self, *, limit=3, cutoff=None, calibrated_now=None):
        if calibrated_now is not None:
            try:
                window = await self.retention_window(calibrated_now)
                cutoff = window['cutoff'] if window else None
            except ValueError:
                # Retention must not stop capture when the calendar is stale.
                logger.exception('realtime_snapshot_retention_calendar_unavailable')
        paths = await asyncio.to_thread(lambda: sorted(self.spool_root.glob('*.json.gz'))[:limit])
        count = 0
        for path in paths:
            try:
                await self._persist(path, cutoff=cutoff)
                count += 1
            except FileNotFoundError:
                continue  # Concurrent retention can remove expired pending data.
            except (PyMongoError, TimeoutError) as exc:
                logger.error('realtime_snapshot_replay_deferred error=%s', type(exc).__name__)
                break
        return count

    async def save_cycle(self, quotes, *, expected_codes, session, observed_at, received_at, metrics,
                         clock_verified=False):
        envelope = {
            'version': 2, 'data_kind': snapshot_kind(observed_at),
            'batch_id': received_at.strftime('%Y%m%dT%H%M%S%f') + '-' + uuid.uuid4().hex,
            'observed_at': observed_at.isoformat(), 'received_at': received_at.isoformat(),
            'clock_offset_seconds': (observed_at - received_at).total_seconds(), 'session': session,
            'expected_codes': expected_codes, 'metrics': metrics,
            'quotes': [snapshot_row(q) for q in quotes],
        }
        path = await asyncio.to_thread(self._spool, envelope)
        cutoff = None
        if clock_verified:
            # The durable local batch proves capture even when Mongo is down.
            # Prune independently of database acknowledgement, once per window,
            # so an extended outage cannot retain expired quote files forever.
            try:
                cutoff = snapshot_retention_cutoff(observed_at.astimezone(CN_TZ).date().isoformat())
            except ValueError:
                logger.exception('realtime_snapshot_retention_calendar_unavailable')
            if cutoff is not None and (self._pending_cleanup_cutoff is None or cutoff > self._pending_cleanup_cutoff):
                try:
                    deleted = await asyncio.to_thread(self._delete_expired_pending, cutoff)
                    self._pending_cleanup_cutoff = cutoff
                    logger.info('realtime_snapshot_pending_retention cutoff=%s deleted=%s', cutoff.isoformat(), deleted)
                except OSError:
                    # A local scan failure must not block today's Mongo write.
                    # Leave the marker unchanged so the next capture retries.
                    logger.exception('realtime_snapshot_pending_retention_failed')
        try:
            size = await self._persist(path)
            await self.replay_pending(limit=2, cutoff=cutoff)
            return {'persisted': True, 'compressed_bytes': size}
        except (PyMongoError, TimeoutError) as exc:
            logger.error('realtime_snapshot_spooled batch=%s error=%s', path.name, type(exc).__name__)
            return {'persisted': False, 'pending_file': path.name}
