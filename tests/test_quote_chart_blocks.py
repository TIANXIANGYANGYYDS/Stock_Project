from datetime import datetime

from app.manually_execute_script.build_quote_chart_blocks import make_blocks
from app.repositories.quote_chart_repository import CHART_FIELDS, SERIES_FIELDS, block_key, unpack_symbol
from app.repositories.realtime_snapshot_repository import AUCTION_COLLECTION, encode_batch, decode_symbol_batch
from collections import defaultdict


def test_chart_views_match_archives_with_duplicates_missing_symbols_and_late_batches():
    time = '2026-11-19T09:20:01.123456+08:00'
    key = block_key('2026-11-19', AUCTION_COLLECTION, datetime.fromisoformat(time))
    assert key == '2026-11-19:a:0120'
    rows = [{'code': '300308', 'price': 308, 'provider': 'primary', 'bids': [[308, 100]], 'phase': 'opening_result'},
            {'code': '300308', 'price': 307, 'provider': 'backup', 'quality_issue': 'stale'},
            {'code': '000008', 'price': 8, 'provider': 'primary'}]
    batches = [dict(encode_batch({'observed_at': time, 'quotes': data}), _id=i)
               for i, data in enumerate([rows, [], rows[::-1]])]
    blocks = {doc['_id']: doc for doc in make_blocks(batches, key)}
    assert len(blocks) == 100
    for code in ('300308', '000008', '100008', '000009'):
        for fields in (CHART_FIELDS, SERIES_FIELDS):
            block = blocks[key + ':' + code[-2:]]
            found = defaultdict(list)
            wanted = {1, 2, 3}
            unpack_symbol(block, code, fields, wanted, found)
            assert wanted.difference(block['ids']) == {3}
            assert found[1] == []
            assert found[2] == [{k: row.get(k) for k in (*fields, 'code', 'observed_at')}
                                for row in decode_symbol_batch(batches[2], code)]
