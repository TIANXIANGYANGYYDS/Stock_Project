from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
# APScheduler 中用于幂等替换盘前分析任务的稳定 ID。
MORNING_ANALYSIS_JOB_ID = "morning_market_analysis"


async def _run_morning_analysis(
    *,
    reference_datetime: datetime | None = None,
) -> Any:
    """
    构造盘前分析服务，并按业务模块固定的数据窗口参数执行一次分析。

    `reference_datetime` 可用于手工历史执行或测试；服务内部仍会判断该日期是否
    为交易日，并以固定的 09:00 作为新闻和博主观点的可用截止点。
    """
    from app.services.morning_analysis_service import (
        MORNING_ANALYSIS_CREATOR_LIMIT,
        MORNING_ANALYSIS_MAX_CREATOR_AGE_HOURS,
        MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        MORNING_ANALYSIS_RANKING_LIMIT,
        MorningAnalysisService,
    )

    service = MorningAnalysisService()
    return await service.run(
        reference_datetime=reference_datetime,
        ranking_limit=MORNING_ANALYSIS_RANKING_LIMIT,
        max_snapshot_age_minutes=MORNING_ANALYSIS_MAX_RANKING_AGE_MINUTES,
        max_creator_age_hours=MORNING_ANALYSIS_MAX_CREATOR_AGE_HOURS,
        creator_limit=MORNING_ANALYSIS_CREATOR_LIMIT,
    )


async def morning_analysis_job() -> None:
    """
    APScheduler 的盘前分析任务入口，记录跳过、成功或失败状态。

    非交易日或尚未到截止时点会作为正常跳过记录；异常在记录堆栈后重新抛出，
    使调度器能够感知任务失败。
    """
    logger.info("morning_analysis_job start")
    try:
        result = await _run_morning_analysis()
        if result.skipped:
            logger.info("morning_analysis_job skipped: %s", result.reason)
        else:
            logger.info(
                "morning_analysis_job completed analysis_date=%s",
                result.report.analysis_date,
            )
    except Exception:
        logger.exception("morning_analysis_job failed")
        raise
    finally:
        logger.info("morning_analysis_job finished")


def register_morning_analysis_job(scheduler: AsyncIOScheduler) -> None:
    """
    按固定的中国时区盘前时刻注册工作日 cron 任务。

    稳定任务 ID 与 `replace_existing` 保证重复初始化不会创建副本；单实例和
    coalesce 防止延迟补跑与当前任务重叠。实际是否为 A 股交易日由服务再次判断。
    """
    from app.services.morning_analysis_service import (
        MORNING_ANALYSIS_HOUR,
        MORNING_ANALYSIS_MINUTE,
    )

    job = scheduler.add_job(
        morning_analysis_job,
        trigger=CronTrigger(
            hour=MORNING_ANALYSIS_HOUR,
            minute=MORNING_ANALYSIS_MINUTE,
            day_of_week="mon-fri",
            timezone="Asia/Shanghai",
        ),
        id=MORNING_ANALYSIS_JOB_ID,
        name="生成每日盘前市场分析",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30 * 60,
    )
    logger.info("registered job id=%s", job.id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="手工生成指定交易日的盘前市场分析")
    parser.add_argument("--date", help="分析日期，格式 YYYY-MM-DD；默认使用今天")
    args = parser.parse_args()

    reference_datetime = None
    if args.date:
        from app.services.morning_analysis_service import (
            MORNING_ANALYSIS_HOUR,
            MORNING_ANALYSIS_MINUTE,
        )

        reference_datetime = datetime.strptime(args.date, "%Y-%m-%d").replace(
            hour=MORNING_ANALYSIS_HOUR,
            minute=MORNING_ANALYSIS_MINUTE,
        )
    run_result = asyncio.run(
        _run_morning_analysis(reference_datetime=reference_datetime)
    )
    if run_result.skipped:
        print(f"skipped: {run_result.reason}")
    else:
        print(f"completed: {run_result.report.analysis_date}")
