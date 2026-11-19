import asyncio
import json
import os
import socket

from app.scheduler.heartbeat import heartbeat, heartbeat_status, notify_watchdog


def test_heartbeat_distinguishes_live_stale_and_missing_process(tmp_path):
    path = tmp_path / 'heartbeat.json'
    assert heartbeat_status(path=path)['status'] == 'unavailable'
    path.write_text(json.dumps({'pid': os.getpid(), 'updated_at': 100}))
    assert heartbeat_status(path=path, now=105)['status'] == 'ok'
    assert heartbeat_status(path=path, now=161)['status'] == 'stale'
    path.write_text(json.dumps({'pid': 999999999, 'updated_at': 100}))
    assert heartbeat_status(path=path, now=105)['status'] == 'unavailable'


def test_watchdog_receives_event_loop_heartbeat(tmp_path, monkeypatch):
    address = str(tmp_path / 'notify.sock')
    monkeypatch.setenv('NOTIFY_SOCKET', address)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        receiver.settimeout(2)
        async def check():
            stopped = asyncio.Event()
            task = asyncio.create_task(heartbeat(stopped, path=tmp_path / 'heartbeat.json', interval=.01))
            await asyncio.sleep(.03)
            assert heartbeat_status(path=tmp_path / 'heartbeat.json')['status'] == 'ok'
            stopped.set()
            await task
        asyncio.run(check())
        assert receiver.recv(128) == b'WATCHDOG=1'


def test_manual_start_does_not_require_systemd(monkeypatch):
    monkeypatch.delenv('NOTIFY_SOCKET', raising=False)
    notify_watchdog()
