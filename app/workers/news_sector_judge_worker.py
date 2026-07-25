from __future__ import annotations

import asyncio
import logging

from app.services import NewsSectorJudgeBatchResult, NewsSectorJudgeService
from app.workers.base_worker import (
    NEWS_LLM_WORKER_BATCH_SIZE,
    WORKER_ERROR_SLEEP_SECONDS,
    WORKER_IDLE_SLEEP_SECONDS,
    BasePollingWorker,
    run_worker_process,
)


logger = logging.getLogger(__name__)


class NewsSectorJudgeWorker(BasePollingWorker[NewsSectorJudgeBatchResult]):
    """
    新闻板块判断 worker。

    业务逻辑在 NewsSectorJudgeService 中；这个类只负责把板块判断 service 接入
    BasePollingWorker 的通用轮询框架。
    """

    def __init__(
        self,
        service: NewsSectorJudgeService | None = None,
        *,
        batch_size: int | None = None,
        idle_sleep_seconds: float | None = None,
        error_sleep_seconds: float | None = None,
    ) -> None:
        """
        初始化板块判断 worker。

        不传参数时使用 worker 模块的固定批量与等待时长；测试可显式覆盖参数。
        """
        active_service = service or NewsSectorJudgeService()

        super().__init__(
            worker_name="news_sector_judge_worker",
            service=active_service,
            batch_size=(
                batch_size
                if batch_size is not None
                else NEWS_LLM_WORKER_BATCH_SIZE
            ),
            idle_sleep_seconds=(
                idle_sleep_seconds
                if idle_sleep_seconds is not None
                else WORKER_IDLE_SLEEP_SECONDS
            ),
            error_sleep_seconds=(
                error_sleep_seconds
                if error_sleep_seconds is not None
                else WORKER_ERROR_SLEEP_SECONDS
            ),
            logger=logger,
        )


async def run_worker() -> None:
    """
    板块判断 worker 的异步入口。
    """

    service = NewsSectorJudgeService()
    worker = NewsSectorJudgeWorker(service=service)
    await run_worker_process(service=service, worker=worker, logger=logger)


def main() -> None:
    """
    命令行入口。

    支持通过 `python -m app.workers.news_sector_judge_worker` 启动。
    """

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("news_sector_judge_worker stopping")


if __name__ == "__main__":
    main()
