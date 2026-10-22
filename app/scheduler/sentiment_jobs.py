"""收盘数据观测任务；独立进程隔离同步 SDK，不注册交易策略。"""
import asyncio
import logging
from pathlib import Path
import sys
import time

from apscheduler.triggers.cron import CronTrigger
from app.services.market_observation_service import observation_now
from app.services.trading_calendar_service import get_a_share_calendar

logger = logging.getLogger(__name__)
JOB_ID = "market_sentiment_daily"
LIVE_JOB_ID = "short_sentiment_live"
_live_workers = {}
_next_idle_check = 0.0


async def update_short_sentiment_job():
    global _next_idle_check
    from app.db.mongo import db
    from app.services.short_sentiment.service import SentimentWorker
    now, _ = await observation_now()
    clock = now.strftime('%H:%M:%S')
    active = get_a_share_calendar().is_session(now.date()) and (
        '09:15:00' <= clock <= '11:35:00' or '13:00:00' <= clock <= '15:06:00')
    if not active and time.monotonic() < _next_idle_check:
        return
    _next_idle_check = time.monotonic() + 60
    for scope in ("all_a", "hs_mainboard"):
        try:
            if scope not in _live_workers:
                _live_workers[scope] = SentimentWorker(db, scope=scope)
            result = await _live_workers[scope].sync()
            if result.get('status') == 'updated' and result['processed']:
                _next_idle_check = 0.0  # Drain an archive backlog without idle throttling.
            if result["processed"]:
                logger.info("short_sentiment_updated scope=%s %s", scope, result)
        except Exception:
            # An unavailable board/source must not stop the other scope.
            logger.exception("short_sentiment_failed scope=%s", scope)


async def record_market_sentiment_job():
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.quant.cli.record_market_sentiment", "sync",
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise
    if process.returncode:
        logger.error("sentiment recording failed: %s", stderr.decode(errors="replace")[-2000:])
        raise RuntimeError(f"情绪日记录失败，退出码 {process.returncode}")
    logger.info("sentiment recording completed: %s", stdout.decode(errors="replace")[-1000:])


def register_sentiment_job(scheduler):
    scheduler.add_job(update_short_sentiment_job, trigger="interval", seconds=5,
        id=LIVE_JOB_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(record_market_sentiment_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour="18,20,22", minute=30, timezone="Asia/Shanghai"),
        id=JOB_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
