from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.services import NewsIngestionService


logger = logging.getLogger(__name__)

_indexes_ready = False
_indexes_lock: Optional[asyncio.Lock] = None


def _get_indexes_lock() -> asyncio.Lock:
    global _indexes_lock

    if _indexes_lock is None:
        _indexes_lock = asyncio.Lock()

    return _indexes_lock


async def ensure_news_indexes() -> None:
    """
    确保新闻集合索引已创建。
    """
    global _indexes_ready

    if _indexes_ready:
        return

    async with _get_indexes_lock():
        if _indexes_ready:
            return

        service = NewsIngestionService()
        await service.ensure_indexes()
        _indexes_ready = True
        logger.info("news indexes ensured")


def _serialize_source_results(source_results: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "source": item.source,
            "fetched_count": item.fetched_count,
            "error_message": item.error_message,
        }
        for item in source_results
    ]


async def crawl_news_job() -> None:
    """
    定时爬虫任务。
    只负责调用 NewsIngestionService 抓取、清洗、去重、入库。
    不允许调用 LLM。
    不允许启动 worker。
    不允许投递队列。
    """
    logger.info("crawl_news_job start")

    try:
        service = NewsIngestionService()
        result = await service.ingest_latest_news()

        logger.info(
            (
                "crawl_news_job stats "
                "total_fetched_count=%s unique_count=%s inserted_count=%s "
                "existing_count=%s duplicate_count=%s"
            ),
            result.total_fetched_count,
            result.unique_count,
            result.inserted_count,
            result.existing_count,
            result.duplicate_count,
        )
        logger.info(
            "crawl_news_job source_results=%s",
            _serialize_source_results(result.source_results),
        )
    except Exception:
        logger.exception("crawl_news_job failed")
    finally:
        logger.info("crawl_news_job finished")