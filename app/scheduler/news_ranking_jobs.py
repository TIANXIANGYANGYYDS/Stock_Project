from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
# APScheduler 中用于幂等替换新闻榜单刷新任务的稳定 ID。
NEWS_RANKING_JOB_ID = "news_ranking_snapshot"
# 新闻榜单固定每 5 分钟刷新一次，以保证 08:20 盘前读取到足够新的快照。
NEWS_RANKING_INTERVAL_MINUTES = 5


async def run_news_ranking_snapshot(
    *,
    reference_datetime: datetime | None = None,
) -> Any:
    """
    按给定参考时点执行一次新闻板块榜单快照生成。

    方法统一服务于调度器和命令行手工调用，读取业务模块固定的滚动窗口、榜单
    条数和盘前截止时刻后交给快照服务。`reference_datetime` 决定新闻窗口截止时间，
    缺省时由服务使用当前时间。
    """
    from app.services.morning_analysis_service import (
        MORNING_ANALYSIS_HOUR,
        MORNING_ANALYSIS_MINUTE,
    )
    from app.services.news_ranking_snapshot_service import (
        NEWS_RANKING_LIMIT,
        NEWS_RANKING_WINDOW_HOURS,
        NewsRankingSnapshotService,
    )

    service = NewsRankingSnapshotService()
    return await service.run(
        reference_datetime=reference_datetime,
        window_hours=NEWS_RANKING_WINDOW_HOURS,
        ranking_limit=NEWS_RANKING_LIMIT,
        morning_cutoff_hour=MORNING_ANALYSIS_HOUR,
        morning_cutoff_minute=MORNING_ANALYSIS_MINUTE,
    )


async def news_ranking_job() -> None:
    """
    APScheduler 的新闻榜单刷新入口，记录一次任务的成功、失败和结束状态。

    异常在写入完整日志后继续抛出，使调度器能够记录失败并按后续周期再次执行。
    """
    logger.info("news_ranking_job start")
    try:
        await run_news_ranking_snapshot()
        logger.info("news_ranking_job completed")
    except Exception:
        logger.exception("news_ranking_job failed")
        raise
    finally:
        logger.info("news_ranking_job finished")


def register_news_ranking_job(scheduler: AsyncIOScheduler) -> None:
    """
    向调度器注册固定启用的周期性新闻榜单快照任务。

    任务每 5 分钟运行，稳定 ID 与 `replace_existing` 防止重复注册；
    单实例和 coalesce 避免上一次计算未结束时产生并发快照。
    """
    job = scheduler.add_job(
        news_ranking_job,
        trigger="interval",
        minutes=NEWS_RANKING_INTERVAL_MINUTES,
        id=NEWS_RANKING_JOB_ID,
        name="刷新新闻板块排行榜快照",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    logger.info("registered job id=%s", job.id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="手工刷新新闻板块排行榜快照")
    parser.add_argument(
        "--datetime",
        help="快照截止时间，格式 'YYYY-MM-DD HH:MM'；默认使用当前时间",
    )
    args = parser.parse_args()
    reference_datetime = (
        datetime.strptime(args.datetime, "%Y-%m-%d %H:%M")
        if args.datetime
        else None
    )
    result = asyncio.run(
        run_news_ranking_snapshot(reference_datetime=reference_datetime)
    )
    print(f"completed: {result.snapshot_id}")
