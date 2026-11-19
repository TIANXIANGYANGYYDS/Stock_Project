"""个股图表读取。以原始索引为准，只从派生数据中取本次查询实际命中的采集批次。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC
import zlib

import orjson

from app.repositories.realtime_snapshot_repository import (
    AUCTION_COLLECTION, SOURCE_CURSOR_INDEX, decode_symbol_batch, symbol_projection,
)

CHART_COLLECTION = 'stock_quote_chart_blocks_v1'
SERIES_FIELDS = ('market_data_time', 'provider', 'phase', 'quality_issue', 'price', 'previous_close')
CHART_FIELDS = (*SERIES_FIELDS, 'volume', 'amount', 'auction_semantics', 'bids', 'asks')


def block_key(day, collection, observed):
    utc = observed.astimezone(UTC) if observed.tzinfo else observed
    kind = 'a' if collection == AUCTION_COLLECTION else 's'
    return f'{day}:{kind}:{utc.hour:02}{utc.minute // 5 * 5:02}'


def unpack_symbol(block, code, fields, wanted, rows):
    ids = block['ids']
    payload = block['symbols'].get(code) if 'symbols' in block else None
    if payload:
        for ordinal, *values in orjson.loads(zlib.decompress(payload)):
            batch_id = ids[ordinal]
            if batch_id in wanted:
                rows[batch_id].append(dict(zip(fields, values), code=code,
                                           observed_at=block['times'][ordinal]))


async def read_chart_window(db, collection, day, start, end, limit, code, view):
    filters = {'trade_date': day, 'observed_at': {'$gte': start, '$lt': end}}
    raw = db[collection]
    batches = await (raw.find(filters, {'_id': 1, 'observed_at': 1})
                     .sort([('observed_at', 1), ('_id', 1)]).hint(SOURCE_CURSOR_INDEX)
                     .limit(limit).to_list(length=limit))
    fields = SERIES_FIELDS if view == 'series' else CHART_FIELDS
    ids = [r['_id'] for r in batches]
    keys = {block_key(day, collection, r['observed_at']) + ':' + code[-2:] for r in batches}
    missing = set(ids)
    rows = defaultdict(list)
    projection = {'ids': 1, 'times': 1, f'symbols.{code}': 1}
    if keys:
        async for block in db[CHART_COLLECTION].find({'_id': {'$in': list(keys)}}, projection):
            unpack_symbol(block, code, fields, missing, rows)
            missing.difference_update(block['ids'])
    # 正在采集、迟到补写和旧格式的批次都能立即读到，不必等待图表块生成。
    if missing:
        projection = dict(symbol_projection(code), _id=1)
        async for batch in raw.find({'_id': {'$in': list(missing)}}, projection):
            full = await asyncio.to_thread(decode_symbol_batch, batch, code)
            rows[batch['_id']] = [{k: row.get(k) for k in ('code', 'observed_at', *fields)} for row in full]
    last = batches[-1]['observed_at'] if batches else None
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return {'data': [row for key in ids for row in rows[key]], 'batches': len(ids),
            'possibly_truncated': len(ids) == limit, 'collection': collection,
            'last_observed_at': last.isoformat() if last is not None else None,
            'sampling': 'public_quote_snapshots_not_exchange_ticks'}
