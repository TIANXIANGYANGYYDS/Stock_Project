import gzip
import hashlib
import json

import pytest

from app.manually_execute_script.repack_realtime_snapshots import repack_document
from app.repositories.realtime_snapshot_repository import (
    decode_batch, decode_symbol_batch, encode_batch, symbol_projection,
)


def legacy(envelope):
    payload = gzip.compress(json.dumps(envelope).encode())
    return {'codec': 'json-gzip-v1', 'payload': payload,
            'sha256': hashlib.sha256(payload).hexdigest()}


@pytest.fixture
def envelope():
    return {'observed_at': '2026-10-08T09:20:00+08:00', 'version': 2,
            'quotes': [{'code': '000001', 'provider': 'primary', 'raw_fields': ['a']},
                       {'code': '002242', 'provider': 'primary'},
                       {'code': '600001', 'provider': 'primary'},
                       {'code': '000001', 'provider': 'backup', 'quality_issue': 'stale_source_time'}]}


def projected(document, code):
    """Emulate Mongo's nested inclusion, leaving unrelated payloads unread."""
    result = {}
    for path in symbol_projection(code):
        if '.' not in path:
            if path in document:
                result[path] = document[path]
        else:
            parent, key = path.split('.')
            if key in document.get(parent, {}):
                result.setdefault(parent, {})[key] = document[parent][key]
    return result


def test_repack_preserves_every_field_duplicate_and_original_order(envelope):
    old = legacy(envelope)
    packed = repack_document(old)
    assert decode_batch(packed) == decode_batch(old) == envelope
    assert packed['compressed_bytes'] == len(packed['payload']) + sum(map(len, packed['quote_chunks'].values()))


@pytest.mark.parametrize('code', ['000001', '002242', '600001', '000042', '999999'])
def test_projected_reads_match_legacy_and_ignore_other_chunks(envelope, code):
    old = legacy(envelope)
    packed = encode_batch(envelope)
    view = projected(packed, code)
    assert set(view.get('quote_chunks', {})) <= {code[-2:]}
    assert decode_symbol_batch(view, code) == decode_symbol_batch(old, code)


def test_detects_corruption_of_selected_partition(envelope):
    packed = encode_batch(envelope)
    packed['quote_chunks']['01'] = b'corrupted'
    with pytest.raises(ValueError, match='checksum'):
        decode_symbol_batch(projected(packed, '000001'), '000001')
    with pytest.raises(ValueError, match='checksum'):
        decode_batch(packed)
    assert len(decode_symbol_batch(projected(packed, '002242'), '002242')) == 1


def test_empty_batch_roundtrip():
    envelope = {'observed_at': '2026-10-08T09:20:00+08:00', 'quotes': []}
    packed = encode_batch(envelope)
    assert decode_batch(packed) == envelope
    assert decode_symbol_batch(projected(packed, '000001'), '000001') == []


def test_api_reads_mixed_encodings_across_decode_windows(envelope):
    import asyncio
    from datetime import date, datetime, timedelta
    from app.api.routers.stocks import list_stock_auctions
    from app.repositories.realtime_snapshot_repository import AUCTION_COLLECTION

    documents = []
    expected = []
    for index in range(70):
        at = datetime.fromisoformat(envelope['observed_at']) + timedelta(seconds=index)
        value = dict(envelope, observed_at=at.isoformat())
        doc = legacy(value) if index == 35 else encode_batch(value)
        doc['observed_at'] = at
        documents.append(projected(doc, '000001'))
        expected.extend(decode_symbol_batch(legacy(value), '000001'))

    class Cursor:
        def sort(self, fields):return self
        def limit(self, value):return self
        async def __aiter__(self):
            for doc in documents:yield doc

    class Collection:
        def find(self, filters, projection):return Cursor()

    result = asyncio.run(list_stock_auctions(
        '000001', date(2026, 10, 8), '09:15:00', '09:30:00', 1000,
        {AUCTION_COLLECTION: Collection()},
    ))
    assert result['data'] == expected
    assert result['batches'] == 70
    assert result['last_observed_at'] == documents[-1]['observed_at'].isoformat()
