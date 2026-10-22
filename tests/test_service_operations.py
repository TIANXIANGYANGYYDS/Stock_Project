import asyncio
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import sys
import time

from app.manually_execute_script.run_logged import run
from app.scheduler import sentiment_jobs as jobs
from app.services.short_sentiment.facts import CN


def test_service_log_rotation_includes_stderr_and_preserves_exit_status(tmp_path):
    log = tmp_path / 'service.log'
    code = "import sys; [print('x'*100) for _ in range(100)]; print('last error',file=sys.stderr); sys.exit(7)"
    assert run([sys.executable, '-u', '-c', code], log, max_bytes=1024, backups=2) == 7
    assert len(list(tmp_path.iterdir())) == 3
    assert all(p.stat().st_size <= 1024 for p in tmp_path.iterdir())
    assert 'last error' in log.read_text()


def test_service_wrapper_forwards_termination_and_drains_final_output(tmp_path):
    log = tmp_path / 'service.log'
    child = "import signal,time; signal.signal(signal.SIGTERM,lambda *_:(print('stopped',flush=True),exit(0))); print('ready',flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, '-m', 'app.manually_execute_script.run_logged', '--log', str(log), '--', sys.executable, '-u', '-c', child])
    try:
        deadline = time.monotonic()+5
        while time.monotonic()<deadline and (not log.exists() or 'ready' not in log.read_text()):
            time.sleep(.02)
        assert 'ready' in log.read_text()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert 'stopped' in log.read_text()
    finally:
        if process.poll() is None: process.kill(); process.wait()


def test_idle_sentiment_checks_are_throttled_but_open_and_backlog_resume(monkeypatch):
    calls = []; now = [datetime(2026, 10, 21, 8, tzinfo=CN)]; result = ['waiting_finalization']
    async def clock(): return now[0], -317
    class Worker:
        async def sync(self):
            calls.append(now[0]); return {'status': result[0], 'processed': int(result[0]=='updated')}
    monkeypatch.setattr(jobs, 'observation_now', clock)
    monkeypatch.setattr(jobs, '_next_idle_check', 0.)
    monkeypatch.setattr(jobs, '_live_workers', {s:Worker() for s in ('all_a','hs_mainboard')})
    async def check():
        await jobs.update_short_sentiment_job(); await jobs.update_short_sentiment_job()
        assert len(calls)==2
        now[0]=now[0].replace(hour=9,minute=30)
        await jobs.update_short_sentiment_job(); await jobs.update_short_sentiment_job()
        assert len(calls)==6
        now[0]=now[0].replace(hour=16); jobs._next_idle_check=0; result[0]='updated'
        await jobs.update_short_sentiment_job(); await jobs.update_short_sentiment_job()
        assert len(calls)==10
    asyncio.run(check())
