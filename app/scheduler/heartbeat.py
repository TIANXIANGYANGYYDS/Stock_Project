"""Event-loop heartbeat for readiness checks and the systemd watchdog."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import time

from app.core.config import PROJECT_ROOT

HEARTBEAT_PATH = PROJECT_ROOT / '.local/run/scheduler_heartbeat.json'
HEARTBEAT_MAX_AGE_SECONDS = 60


def notify_watchdog():
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        channel.sendto(b'WATCHDOG=1', address)


async def heartbeat(stop_event, *, path=HEARTBEAT_PATH, interval=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid': os.getpid(), 'updated_at': time.time()}))
        temporary.replace(path)
        notify_watchdog()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass


def heartbeat_status(*, path=HEARTBEAT_PATH, now=None):
    try:
        value = json.loads(path.read_text())
        age = (time.time() if now is None else now) - float(value['updated_at'])
        os.kill(int(value['pid']), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'unavailable'}
    return {'status': 'ok' if 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS else 'stale',
            'age_seconds': round(age, 1), 'pid': value['pid']}
