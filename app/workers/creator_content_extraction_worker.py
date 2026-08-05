from __future__ import annotations

import asyncio
import logging

from app.services.creator_content_extraction_service import (
    CreatorContentExtractionService,
    CreatorExtractionBatchResult,
)
from app.workers.base_worker import (
    WORKER_ERROR_SLEEP_SECONDS,
    WORKER_IDLE_SLEEP_SECONDS,
    BasePollingWorker,
    run_worker_process,
)


logger = logging.getLogger(__name__)

# 单批只处理一个媒体作品，限制下载、解码、Whisper 和 OCR 的峰值内存。
CREATOR_EXTRACTION_BATCH_SIZE = 1


class CreatorContentExtractionWorker(BasePollingWorker[CreatorExtractionBatchResult]):
    """持续轮询 ``creator_works`` 中等待内容提取的作品。

    该进程只加载媒体下载、OCR 和 ASR 依赖，不导入或创建任何 LLM 客户端。队列
    空闲时服务会主动释放重量级本地模型，避免服务器长期保留无任务内存。
    """

    def __init__(
        self,
        service: CreatorContentExtractionService | None = None,
        *,
        batch_size: int = CREATOR_EXTRACTION_BATCH_SIZE,
        idle_sleep_seconds: float = WORKER_IDLE_SLEEP_SECONDS,
        error_sleep_seconds: float = WORKER_ERROR_SLEEP_SECONDS,
    ) -> None:
        """配置独立内容提取服务、单批上限和空闲及异常退避时间。"""

        active_service = service or CreatorContentExtractionService()
        super().__init__(
            worker_name="creator_content_extraction_worker",
            service=active_service,
            batch_size=batch_size,
            idle_sleep_seconds=idle_sleep_seconds,
            error_sleep_seconds=error_sleep_seconds,
            logger=logger,
        )


async def run_worker() -> None:
    """创建内容提取服务并运行通用轮询生命周期直至收到停止信号。"""

    service = CreatorContentExtractionService()
    worker = CreatorContentExtractionWorker(service=service)
    await run_worker_process(service=service, worker=worker, logger=logger)


def main() -> None:
    """启动异步内容提取 worker，并在交互式中断时记录正常退出。"""

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("creator_content_extraction_worker stopping")


if __name__ == "__main__":
    main()


__all__ = ["CreatorContentExtractionWorker"]
