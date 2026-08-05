from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.crawlers.creator_platforms.douyin import (
    parse_douyin_session_cookie_expiry,
)
from app.models.creator_monitoring import CN_TZ
from app.services.trading_calendar_service import resolve_morning_trade_dates


logger = logging.getLogger(__name__)
CREATOR_INGESTION_JOB_ID = "creator_monitoring_ingestion"
DOUYIN_COOKIE_CHECK_JOB_ID = "douyin_session_cookie_expiry_check"
CREATOR_VERIFICATION_JOB_ID = "creator_daily_verification"
CREATOR_VERIFICATION_RETRY_JOB_ID = "creator_daily_verification_retry"
# 每小时整点串行扫描一次所有启用账号；错过触发只合并为一次补跑。
CREATOR_CRAWL_HOURS = "*"
CREATOR_CRAWL_MINUTE = "0"
DOUYIN_COOKIE_CHECK_HOUR = 9
DOUYIN_COOKIE_CHECK_MINUTE = 5
DOUYIN_COOKIE_WARNING_DAYS = 7
CREATOR_VERIFICATION_HOUR = 15
CREATOR_VERIFICATION_MINUTE = 40
CREATOR_VERIFICATION_RETRY_HOUR = 16
CREATOR_VERIFICATION_RETRY_MINUTE = 30


def _reference_datetime(value: datetime | None) -> datetime:
    """返回调度任务执行时间点查询所使用的中国时区参考时间。

    传入 ``None`` 时使用当前中国时间；显式传入的无时区时间会被拒绝，避免交易日
    选择和市场证据截止时间在未察觉的情况下发生偏移。
    """

    reference = value or datetime.now(CN_TZ)
    if reference.tzinfo is None:
        raise ValueError("reference_datetime 必须包含时区")
    return reference.astimezone(CN_TZ)


async def run_creator_ingestion(
    *, reference_datetime: datetime | None = None
) -> Any:
    """创建博主作品采集服务、确保索引存在并抓取所有启用账号。

    可选参考时间会原样传给采集服务，使测试或历史补采能够使用确定的回看窗口；
    正常调度执行时不传入该值，由服务使用当前时间。
    """

    from app.services.creator_ingestion_service import CreatorIngestionService

    service = CreatorIngestionService()
    await service.ensure_indexes()
    return await service.ingest_all(reference_datetime=reference_datetime)


async def creator_ingestion_job() -> None:
    """执行定时博主作品采集，记录批次统计信息并继续向上抛出失败。"""

    logger.info("creator_ingestion_job start")
    try:
        result = await run_creator_ingestion()
        logger.info(
            "creator_ingestion_job completed accounts=%s inserted=%s failed_accounts=%s",
            len(result.results),
            result.inserted_count,
            result.failed_account_count,
        )
    except Exception:
        logger.exception("creator_ingestion_job failed")
        raise
    finally:
        logger.info("creator_ingestion_job finished")


def check_douyin_session_cookie_expiry(
    *, reference_datetime: datetime | None = None
) -> datetime | None:
    """检查抖音登录会话到期时间，并按剩余时长记录脱敏告警。"""

    reference = _reference_datetime(reference_datetime)
    cookie = get_settings().douyin_session_cookie.get_secret_value().strip()
    try:
        expires_at = parse_douyin_session_cookie_expiry(cookie)
    except ValueError as exc:
        logger.warning(
            "douyin_session_cookie_expiry_unavailable reason=%s action=replace_cookie",
            exc,
        )
        return None

    expires_at_cn = expires_at.astimezone(CN_TZ)
    remaining = expires_at_cn - reference
    expires_text = expires_at_cn.strftime("%Y-%m-%d %H:%M:%S %Z")
    if remaining <= timedelta(0):
        logger.error(
            "douyin_session_cookie_expired expires_at=%s action=replace_cookie",
            expires_text,
        )
    elif remaining <= timedelta(days=DOUYIN_COOKIE_WARNING_DAYS):
        remaining_hours = int(remaining.total_seconds() // 3600)
        logger.warning(
            "douyin_session_cookie_expiring expires_at=%s remaining_hours=%s "
            "action=replace_cookie",
            expires_text,
            remaining_hours,
        )
    else:
        remaining_days = int((remaining.total_seconds() + 86399) // 86400)
        logger.info(
            "douyin_session_cookie_healthy expires_at=%s remaining_days=%s",
            expires_text,
            remaining_days,
        )
    return expires_at


async def run_creator_daily_verification(
    *, reference_datetime: datetime | None = None
) -> Any | None:
    """在交易日收盘后运行统一的博主观点联网验证与评分。

    非交易日在创建服务前直接返回 ``None``。统一服务从 ``creator_works`` 读取
    LLM 1 观点，并在 ``creator_opinion_analyses`` 内原子完成 pending 到 verified
    的迁移及累计准确率更新。
    """

    from app.services.creator_daily_verification_service import (
        CreatorDailyVerificationService,
    )

    reference = _reference_datetime(reference_datetime)
    decision = resolve_morning_trade_dates(reference.date())
    if not decision.is_current_trade_day:
        return None
    service = CreatorDailyVerificationService()
    await service.ensure_indexes()
    return await service.run(
        score_date=decision.analysis_date,
        as_of=reference,
    )


async def creator_daily_verification_job() -> None:
    """执行统一收盘验证，记录完成及失败博主数量并向上抛出全局异常。

    单博主异常已由服务写入自己的失败文档并隔离；只有公共行情无法构建等批次级
    问题才会抛出，使调度器能够在 16:30 再次补偿执行。
    """

    logger.info("creator_daily_verification_job start")
    try:
        result = await run_creator_daily_verification()
        if result is None:
            logger.info("creator_daily_verification_job skipped non-trading day")
        else:
            scored_count = sum(item.score is not None for item in result.results)
            failed_count = sum(item.status == "failed" for item in result.results)
            logger.info(
                "creator_daily_verification_job completed score_date=%s "
                "scored=%s failed=%s total=%s",
                result.score_date,
                scored_count,
                failed_count,
                len(result.results),
            )
    except Exception:
        logger.exception("creator_daily_verification_job failed")
        raise
    finally:
        logger.info("creator_daily_verification_job finished")


def register_creator_monitoring_jobs(scheduler: AsyncIOScheduler) -> None:
    """注册每小时采集和收盘观点验证任务。

    采集、内容提取、观点分析和验证都限制单实例；采集服务内部按账号顺序串行，
    避免平台请求或 LLM 调用重叠放大服务器资源。观点汇总不再有独立定时任务，
    因为 LLM 1 成功时直接同步第二张业务表，收盘验证也会幂等补齐。
    """

    ingestion = scheduler.add_job(
        creator_ingestion_job,
        trigger=CronTrigger(
            hour=CREATOR_CRAWL_HOURS,
            minute=CREATOR_CRAWL_MINUTE,
            timezone="Asia/Shanghai",
        ),
        id=CREATOR_INGESTION_JOB_ID,
        name="抓取20位跨平台博主公开作品",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=10 * 60,
    )
    cookie_check = scheduler.add_job(
        check_douyin_session_cookie_expiry,
        trigger=CronTrigger(
            hour=DOUYIN_COOKIE_CHECK_HOUR,
            minute=DOUYIN_COOKIE_CHECK_MINUTE,
            timezone="Asia/Shanghai",
        ),
        id=DOUYIN_COOKIE_CHECK_JOB_ID,
        name="检查抖音登录会话到期时间",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
        next_run_time=datetime.now(CN_TZ),
    )
    verification = scheduler.add_job(
        creator_daily_verification_job,
        trigger=CronTrigger(
            hour=CREATOR_VERIFICATION_HOUR,
            minute=CREATOR_VERIFICATION_MINUTE,
            day_of_week="mon-fri",
            timezone="Asia/Shanghai",
        ),
        id=CREATOR_VERIFICATION_JOB_ID,
        name="收盘后联网核验博主观点并评分",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
    )
    retry = scheduler.add_job(
        creator_daily_verification_job,
        trigger=CronTrigger(
            hour=CREATOR_VERIFICATION_RETRY_HOUR,
            minute=CREATOR_VERIFICATION_RETRY_MINUTE,
            day_of_week="mon-fri",
            timezone="Asia/Shanghai",
        ),
        id=CREATOR_VERIFICATION_RETRY_JOB_ID,
        name="补偿重跑博主观点核验与评分",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60 * 60,
    )
    logger.info(
        "registered creator jobs ids=%s",
        ",".join([ingestion.id, cookie_check.id, verification.id, retry.id]),
    )
