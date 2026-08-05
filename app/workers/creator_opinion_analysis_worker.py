from __future__ import annotations

import asyncio
import logging

from app.services.creator_opinion_analysis_service import (
    CreatorOpinionAnalysisService,
    CreatorOpinionBatchResult,
)
from app.workers.base_worker import (
    WORKER_ERROR_SLEEP_SECONDS,
    WORKER_IDLE_SLEEP_SECONDS,
    BasePollingWorker,
    run_worker_process,
)


logger = logging.getLogger(__name__)

# 每批只分析一篇作品，避免同时发送多个长上下文请求并放大内存和模型限流压力。
CREATOR_OPINION_ANALYSIS_BATCH_SIZE = 1


class CreatorOpinionAnalysisWorker(BasePollingWorker[CreatorOpinionBatchResult]):
    """持续轮询已经提取文本、等待 LLM 1 观点分析的作品。

    该进程只读取 ``creator_works`` 并把结构化观点写回 ``analysis``；
    它不会加载 OCR/ASR，也不会调用收盘后的联网验证 LLM 2。
    """

    def __init__(
        self,
        service: CreatorOpinionAnalysisService | None = None,
        *,
        batch_size: int = CREATOR_OPINION_ANALYSIS_BATCH_SIZE,
        idle_sleep_seconds: float = WORKER_IDLE_SLEEP_SECONDS,
        error_sleep_seconds: float = WORKER_ERROR_SLEEP_SECONDS,
    ) -> None:
        """配置独立 LLM 1 服务、单批上限和空闲及异常退避时间。"""

        active_service = service or CreatorOpinionAnalysisService()
        super().__init__(
            worker_name="creator_opinion_analysis_worker",
            service=active_service,
            batch_size=batch_size,
            idle_sleep_seconds=idle_sleep_seconds,
            error_sleep_seconds=error_sleep_seconds,
            logger=logger,
        )


async def run_worker() -> None:
    """创建 LLM 1 观点分析服务并运行通用轮询生命周期直至停止。"""

    service = CreatorOpinionAnalysisService()
    worker = CreatorOpinionAnalysisWorker(service=service)
    await run_worker_process(service=service, worker=worker, logger=logger)


def main() -> None:
    """启动异步观点分析 worker，并在交互式中断时记录正常退出。"""

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("creator_opinion_analysis_worker stopping")


if __name__ == "__main__":
    main()


__all__ = ["CreatorOpinionAnalysisWorker"]
