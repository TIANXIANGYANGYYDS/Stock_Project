"""收盘数据观测任务；独立进程隔离同步 SDK，不注册交易策略。"""
import asyncio
import logging
from pathlib import Path
import sys

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
JOB_ID = "market_sentiment_daily"


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
    scheduler.add_job(record_market_sentiment_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour="18,20,22", minute=30, timezone="Asia/Shanghai"),
        id=JOB_ID, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
