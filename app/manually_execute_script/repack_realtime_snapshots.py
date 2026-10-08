"""Repack legacy quote batches without changing any observation.

Deploy the compatible reader first. Each original gzip is backed up before
an atomic, checksum-guarded update. Re-running skips already converted batches.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path

from app.manually_execute_script.repair_realtime_minutes_ths import connect
from app.repositories.realtime_snapshot_repository import (
    AUCTION_COLLECTION, SNAPSHOT_COLLECTION, decode_batch, encode_batch,
)


def repack_document(document):
    envelope = decode_batch(document)
    packed = encode_batch(envelope)
    if decode_batch(packed) != envelope:
        raise ValueError('repacked observation mismatch')
    return packed


def run(*, trade_date, output, apply=False):
    date.fromisoformat(trade_date)
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    client, db = connect()
    summary = {'trade_date': trade_date, 'applied': apply, 'checked': 0, 'updated': 0,
               'before_bytes': 0, 'after_bytes': 0}
    try:
        for name in (AUCTION_COLLECTION, SNAPSHOT_COLLECTION):
            folder = root / name
            folder.mkdir(exist_ok=True)
            collection = db[name]
            # One batch at a time keeps memory and database traffic bounded.
            cursor = collection.find({'trade_date': trade_date, 'codec': 'json-gzip-v1'}).batch_size(1)
            for doc in cursor:
                packed = repack_document(doc)
                if apply:
                    backup = folder / (doc['_id'] + '.json.gz')
                    if not backup.exists():
                        temp = backup.with_suffix('.tmp')
                        with temp.open('wb') as stream:
                            stream.write(bytes(doc['payload']))
                            stream.flush()
                            os.fsync(stream.fileno())
                        temp.replace(backup)
                        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    if hashlib.sha256(backup.read_bytes()).hexdigest() != doc['sha256']:
                        raise ValueError('original batch backup checksum mismatch')
                    # An acknowledged v1 batch is immutable; guard against a
                    # concurrent repack or repair anyway.
                    result = collection.update_one(
                        {'_id': doc['_id'], 'codec': 'json-gzip-v1', 'sha256': doc['sha256']},
                        {'$set': packed})
                    summary['updated'] += result.modified_count
                summary['checked'] += 1
                summary['before_bytes'] += doc['compressed_bytes']
                summary['after_bytes'] += packed['compressed_bytes']
                if summary['checked'] % 50 == 0:
                    print(json.dumps(summary), flush=True)
        (root / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        return summary
    finally:
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trade-date', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    run(trade_date=args.trade_date, output=args.output, apply=args.apply)
