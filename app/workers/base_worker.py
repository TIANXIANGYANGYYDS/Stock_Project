from __future__ import annotations

import asyncio
import logging
import signal
from typing import Generic, Protocol, TypeVar

from app.core.config import get_settings


class BatchProcessResult(Protocol):
    """
    批处理结果协议。

    具体 service 的 batch result 只要提供这三个统计属性，就可以被通用 worker
    框架消费并统一打日志、判断空闲状态。
    """

    @property
    def total_claimed_count(self) -> int:
        """
        本批次实际领取并处理的任务数量。
        """

        ...

    @property
    def success_count(self) -> int:
        """
        本批次成功处理的任务数量。
        """

        ...

    @property
    def failed_count(self) -> int:
        """
        本批次处理失败的任务数量。
        """

        ...


BatchResultT = TypeVar("BatchResultT", bound=BatchProcessResult)


class BatchProcessingService(Protocol[BatchResultT]):
    """
    批处理 service 协议。

    独立业务 service 只需要实现 ensure_indexes 和 process_batch，就可以接入
    BasePollingWorker，不需要重复写 worker 循环、sleep、日志和信号处理。
    """

    async def ensure_indexes(self) -> None:
        """
        worker 启动前的准备动作，通常用于创建 MongoDB 索引。
        """

        ...

    async def process_batch(self, *, batch_size: int) -> BatchResultT:
        """
        执行一批业务处理。
        """

        ...


class BasePollingWorker(Generic[BatchResultT]):
    """
    通用轮询 worker 框架。

    它只处理所有 worker 都共有的部分：
    1. 按 batch_size 调用 service.process_batch；
    2. 没任务时 idle sleep；
    3. 循环异常时 error sleep；
    4. stop_event 触发时优雅退出；
    5. 统一输出 claimed/success/failed 日志。

    具体业务逻辑仍然放在各自 service 中，避免多个 workflow 混在一起。
    """

    def __init__(
        self,
        *,
        worker_name: str,
        service: BatchProcessingService[BatchResultT],
        batch_size: int,
        idle_sleep_seconds: float,
        error_sleep_seconds: float,
        logger: logging.Logger,
    ) -> None:
        """
        初始化通用 worker。

        worker_name：
            日志中显示的 worker 名称。

        service：
            实际执行业务处理的独立 service。

        batch_size：
            每轮最多处理多少条任务。

        idle_sleep_seconds：
            没有任务时等待多久再轮询。

        error_sleep_seconds：
            worker 循环发生未预期异常时等待多久再恢复。

        logger：
            当前 worker 模块自己的 logger。
        """

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        if idle_sleep_seconds < 0:
            raise ValueError("idle_sleep_seconds 不能小于 0")

        if error_sleep_seconds < 0:
            raise ValueError("error_sleep_seconds 不能小于 0")

        # worker 名称只用于日志，业务状态仍由 service/repository 决定。
        self.worker_name = worker_name

        # 独立业务 service，通用 worker 不直接操作数据库或 LLM。
        self.service = service

        # 单轮最大处理数量，用来控制每次循环的工作量。
        self.batch_size = batch_size

        # 空闲时的休眠时间，避免无任务时频繁打数据库。
        self.idle_sleep_seconds = idle_sleep_seconds

        # 非预期异常后的退避时间，避免异常情况下疯狂重试。
        self.error_sleep_seconds = error_sleep_seconds

        # 每个业务 worker 使用自己的 logger 名称，方便分日志排查。
        self.logger = logger

    async def run_once(self) -> BatchResultT:
        """
        执行一轮批处理。

        这个方法适合单元测试、手动调试，也会被 run_forever 循环调用。
        """

        result = await self.service.process_batch(batch_size=self.batch_size)

        self.logger.info(
            "%s batch claimed=%s success=%s failed=%s",
            self.worker_name,
            result.total_claimed_count,
            result.success_count,
            result.failed_count,
        )

        return result

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """
        常驻运行 worker。

        stop_event 被设置后，worker 会在当前批次结束或 sleep 被打断后退出。
        """

        active_stop_event = stop_event or asyncio.Event()

        self.logger.info("%s started", self.worker_name)

        while not active_stop_event.is_set():
            try:
                result = await self.run_once()

                if result.total_claimed_count == 0:
                    await self._sleep(active_stop_event, self.idle_sleep_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("%s loop failed", self.worker_name)
                await self._sleep(active_stop_event, self.error_sleep_seconds)

        self.logger.info("%s stopped", self.worker_name)

    @staticmethod
    async def _sleep(stop_event: asyncio.Event, seconds: float) -> None:
        """
        可被 stop_event 打断的异步 sleep。

        这样收到停止信号时，不需要等完整 sleep 时间结束才能退出。
        """

        if seconds <= 0:
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return


def configure_worker_logging() -> None:
    """
    配置 worker 进程日志。

    日志级别读取 LOG_LEVEL，格式与 scheduler 保持一致。
    """

    settings = get_settings()
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )


def install_signal_handlers(
    stop_event: asyncio.Event,
    *,
    logger: logging.Logger,
) -> None:
    """
    安装 SIGINT/SIGTERM 处理器。

    信号到来时只设置 stop_event，具体退出和资源关闭由 run_worker_process 负责。
    """

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("signal handlers unavailable, signal=%s", sig.name)


async def run_worker_process(
    *,
    service: BatchProcessingService[BatchResultT],
    worker: BasePollingWorker[BatchResultT],
    logger: logging.Logger,
) -> None:
    """
    通用 worker 进程入口。

    负责启动前准备和退出后清理：
    1. 配置日志；
    2. 确保业务 service 需要的索引；
    3. 安装停止信号；
    4. 运行 worker；
    5. 退出时关闭 MongoDB client。
    """

    configure_worker_logging()

    await service.ensure_indexes()

    stop_event = asyncio.Event()

    try:
        install_signal_handlers(stop_event, logger=logger)
        await worker.run_forever(stop_event)
    finally:
        from app.db.mongo import client

        client.close()
