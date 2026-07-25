from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.models.douyin_creator_work import CN_TZ


logger = logging.getLogger(__name__)
# APScheduler 中用于幂等替换抖音发现任务的稳定 ID。
DOUYIN_CREATOR_JOB_ID = "douyin_creator_ingestion"
# 每 15 分钟检查一次目标博主的新作品，在抓取及时性与平台访问频率间取平衡。
DOUYIN_CRAWL_INTERVAL_MINUTES = 15
# 每轮最多读取 15 个候选作品，避免单次公开页面抓取和详情请求持续过久。
DOUYIN_FETCH_LIMIT = 15
# 候选发现固定回看 96 小时，覆盖周末及短期抓取失败后的补录窗口。
DOUYIN_LOOKBACK_HOURS = 96


async def run_douyin_creator_ingestion(
    *,
    reference_datetime: datetime | None = None,
) -> Any:
    """
    按给定参考时间执行一次抖音作品发现和幂等入库。

    调用时创建服务并确保索引存在，随后把参考时间转换为作品发布时间上限；
    回看小时数和候选数量使用本模块固定值。`reference_datetime` 主要用于测试和
    历史诊断，缺省时使用中国时区当前时间。
    """
    from app.services.douyin_creator_ingestion_service import (
        DouyinCreatorIngestionService,
    )

    service = DouyinCreatorIngestionService()
    await service.ensure_indexes()
    reference = reference_datetime or datetime.now(CN_TZ)
    return await service.ingest_latest_works(
        cutoff_ts=int(reference.timestamp()),
        lookback_hours=DOUYIN_LOOKBACK_HOURS,
        limit=DOUYIN_FETCH_LIMIT,
    )


async def douyin_creator_ingestion_job() -> None:
    """
    APScheduler 的抖音发现任务入口，记录完整执行统计和异常日志。

    异常会在记录后继续抛出，让调度器保留失败状态；`finally` 日志用于明确
    单次任务生命周期已经结束。
    """
    logger.info("douyin_creator_ingestion_job start")
    try:
        result = await run_douyin_creator_ingestion()
        logger.info(
            "douyin_creator_ingestion_job completed discovered=%s inserted=%s "
            "existing=%s detail_failed=%s",
            result.discovered_count,
            result.inserted_count,
            result.existing_count,
            result.detail_failed_count,
        )
    except Exception:
        logger.exception("douyin_creator_ingestion_job failed")
        raise
    finally:
        logger.info("douyin_creator_ingestion_job finished")


def register_douyin_creator_job(scheduler: AsyncIOScheduler) -> None:
    """
    向调度器注册固定启用的周期性抖音作品发现任务。

    任务每 15 分钟运行，进程启动后立即执行一次，并通过单实例、合并补跑和
    稳定 ID 避免重叠；目标账号由抖音爬虫模块中的固定常量提供。
    """
    job = scheduler.add_job(
        douyin_creator_ingestion_job,
        trigger="interval",
        minutes=DOUYIN_CRAWL_INTERVAL_MINUTES,
        id=DOUYIN_CREATOR_JOB_ID,
        name="抓取全能的野人公开视频",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=5 * 60,
        next_run_time=datetime.now(CN_TZ),
    )
    logger.info("registered job id=%s", job.id)
