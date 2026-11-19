"""从不可变的原始批次生成按股票读取的五分钟图表块，允许中断后重跑。"""
import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import logging
import zlib

from bson import Binary
import orjson
from pymongo import MongoClient, ReplaceOne

from app.core.config import get_settings
from app.repositories.quote_chart_repository import CHART_COLLECTION, CHART_FIELDS, block_key
from app.repositories.realtime_snapshot_repository import (
    AUCTION_COLLECTION, PARTITIONED_CODEC, SNAPSHOT_COLLECTION, SOURCE_CURSOR_INDEX,
)

logger = logging.getLogger(__name__)


def checked(payload, checksum):
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError('原始行情校验失败')
    return orjson.loads(gzip.decompress(payload))


def make_blocks(documents, key):
    ids, times = [], []
    symbols = defaultdict(list)
    for ordinal, batch in enumerate(documents):
        header = checked(batch['payload'], batch['sha256'])
        ids.append(batch['_id'])
        times.append(header['observed_at'])
        if batch.get('codec') == PARTITIONED_CODEC:
            quotes = (row for suffix, payload in batch['quote_chunks'].items()
                      for _, row in checked(payload, batch['chunk_sha256'][suffix]))
        else:
            quotes = header['quotes']
        for row in quotes:
            symbols[row['code']].append(orjson.dumps([ordinal, *(row.get(k) for k in CHART_FIELDS)]))
    groups = defaultdict(dict)
    for code, samples in symbols.items():
        groups[code[-2:]][code] = Binary(zlib.compress(b'[' + b','.join(samples) + b']', 1))
    # 空分组也记录覆盖范围，以区分“该股票没有报价”和“这段尚未建立索引”。
    for suffix in range(100):
        tail = f'{suffix:02}'
        yield {'_id': f'{key}:{tail}', 'ids': ids, 'times': times, 'symbols': groups[tail]}


def sync_blocks(db, max_blocks=None, *, trade_date=None):
    index = db[CHART_COLLECTION]
    manifests = {d['_id'][:-2]: d['ids'] for d in index.find({'manifest': True}, {'ids': 1})}
    work = defaultdict(list)
    days = set()
    source_filter = {'trade_date': trade_date} if trade_date else {}
    for name in (AUCTION_COLLECTION, SNAPSHOT_COLLECTION):
        for b in db[name].find(source_filter, {'trade_date': 1, 'observed_at': 1}).hint(SOURCE_CURSOR_INDEX):
            day, observed = b['trade_date'], b['observed_at']
            days.add(day)
            work[(day, block_key(day, name, observed), name)].append((observed, b['_id']))
    # 只清理已离开原始保留期的派生块；没有源数据时保留现状。
    if days and trade_date is None:
        index.delete_many({'_id': {'$lt': min(days)}})
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    count = 0
    for day, key, name in sorted(work, key=lambda x: (-int(x[0].replace('-', '')), x[1][-4:], x[1])):
        batch_info = sorted(work[(day, key, name)])
        last = batch_info[-1][0]
        end = last.replace(minute=last.minute // 5 * 5, second=0, microsecond=0) + timedelta(minutes=5)
        ids = [batch_id for _, batch_id in batch_info]
        if end > cutoff or manifests.get(key) == ids:
            continue
        # 按原始时间索引读，避免为大文档做阻塞排序。
        low, high = batch_info[0][0], batch_info[-1][0]
        raw = db[name].find({'trade_date': day, 'observed_at': {'$gte': low, '$lte': high}},
                            {'codec': 1, 'payload': 1, 'sha256': 1, 'quote_chunks': 1, 'chunk_sha256': 1}
                            ).sort([('observed_at', 1), ('_id', 1)]).hint(SOURCE_CURSOR_INDEX).batch_size(3)
        blocks = list(make_blocks(raw, key))
        if blocks[0]['ids'] != ids:
            continue
        index.bulk_write([ReplaceOne({'_id': b['_id']}, b, upsert=True) for b in blocks], ordered=False)
        index.replace_one({'_id': key + ':m'}, {'_id': key + ':m', 'manifest': True, 'ids': ids}, upsert=True)
        count += 1
        logger.info('图表块已完成 %s 批次=%d', key, len(ids))
        if max_blocks and count >= max_blocks:
            break
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-blocks', type=int)
    parser.add_argument('--trade-date', help='只补指定交易日，不清理其他日期')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    settings = get_settings()
    with MongoClient(settings.mongo_uri) as client:
        db = client[settings.mongo_db_name]
        db[CHART_COLLECTION].create_index('manifest', sparse=True)
        sync_blocks(db, args.max_blocks, trade_date=args.trade_date)
