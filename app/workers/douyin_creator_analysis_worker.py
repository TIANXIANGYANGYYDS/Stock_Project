from __future__ import annotations

import asyncio
import logging

from app.services.douyin_creator_analysis_service import (
    DouyinCreatorAnalysisService,
    DouyinCreatorBatchResult,
)
from app.workers.base_worker import (
    WORKER_ERROR_SLEEP_SECONDS,
    WORKER_IDLE_SLEEP_SECONDS,
    BasePollingWorker,
    run_worker_process,
)


logger = logging.getLogger(__name__)
# ASR/OCR 对 CPU 和内存占用较高，因此抖音 worker 每轮固定只处理 1 个作品。
DOUYIN_WORKER_BATCH_SIZE = 1


class DouyinCreatorAnalysisWorker(BasePollingWorker[DouyinCreatorBatchResult]):
    """
    常驻轮询抖音作品队列并批量执行转写和观点分析的 worker。

    通用循环、退避和信号处理由 `BasePollingWorker` 提供；本类只把抖音服务与
    对应批大小、空闲等待和错误等待配置绑定起来。
    """

    def __init__(
        self,
        service: DouyinCreatorAnalysisService | None = None,
        *,
        batch_size: int | None = None,
        idle_sleep_seconds: float | None = None,
        error_sleep_seconds: float | None = None,
    ) -> None:
        """
        初始化抖音分析服务以及轮询批量和退避参数。

        显式参数优先，未传入时使用本模块固定批量与通用 worker 等待时长；服务
        也可注入，便于测试队列状态而不构造真实下载和 ASR 依赖。
        """
        # 当前 worker 实际调用的抖音分析服务。
        active_service = service or DouyinCreatorAnalysisService()
        super().__init__(
            worker_name="douyin_creator_analysis_worker",
            service=active_service,
            batch_size=(
                batch_size
                if batch_size is not None
                else DOUYIN_WORKER_BATCH_SIZE
            ),
            idle_sleep_seconds=(
                WORKER_IDLE_SLEEP_SECONDS
                if idle_sleep_seconds is None
                else idle_sleep_seconds
            ),
            error_sleep_seconds=(
                WORKER_ERROR_SLEEP_SECONDS
                if error_sleep_seconds is None
                else error_sleep_seconds
            ),
            logger=logger,
        )


async def run_worker() -> None:
    """
    构造固定启用的抖音分析服务和 worker，并进入受信号控制的常驻轮询循环。
    """
    service = DouyinCreatorAnalysisService()
    worker = DouyinCreatorAnalysisWorker(service=service)
    await run_worker_process(service=service, worker=worker, logger=logger)


def main() -> None:
    """启动异步 worker 进程，并把人工中断转换为正常停止日志。"""
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("douyin_creator_analysis_worker stopping")


if __name__ == "__main__":
    main()
