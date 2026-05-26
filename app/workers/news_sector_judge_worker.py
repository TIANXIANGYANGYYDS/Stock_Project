from __future__ import annotations

import asyncio
import logging
import signal

from app.core.config import get_settings
from app.services import NewsSectorJudgeBatchResult, NewsSectorJudgeService


logger = logging.getLogger(__name__)


class NewsSectorJudgeWorker:
    """
    新闻板块判断常驻 worker。

    worker 只负责循环、节流和生命周期；具体业务闭环由
    NewsSectorJudgeService.process_batch 处理。

    设计上把 worker 和 service 拆开，是为了让：
    1. service 可以被单元测试直接调用；
    2. worker 只处理常驻进程的通用问题；
    3. 后续新增其他 LLM worker 时可以复用同样模式。
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
        初始化 worker 运行参数。

        service：
            业务处理服务。生产环境默认创建 NewsSectorJudgeService；测试或调试时可注入
            自定义 service。

        batch_size：
            每一轮最多处理多少条新闻。None 时读取配置
            NEWS_SECTOR_JUDGE_BATCH_SIZE。

        idle_sleep_seconds：
            本轮没有领取到新闻时等待多久再查库。None 时读取配置
            NEWS_SECTOR_JUDGE_IDLE_SLEEP_SECONDS。

        error_sleep_seconds：
            worker 循环自身出现未预期异常时等待多久再恢复。None 时读取配置
            NEWS_SECTOR_JUDGE_ERROR_SLEEP_SECONDS。
        """

        settings = get_settings()

        # 负责实际处理新闻；worker 不直接调用 repository 或 LLM。
        self.service = service or NewsSectorJudgeService()

        # 单轮最大处理数量，用来控制每次循环的工作量。
        self.batch_size = batch_size if batch_size is not None else settings.news_sector_judge_batch_size

        # 没有任务时的休眠时间，避免空转频繁打 MongoDB。
        self.idle_sleep_seconds = (
            idle_sleep_seconds
            if idle_sleep_seconds is not None
            else settings.news_sector_judge_idle_sleep_seconds
        )

        # worker 循环发生非预期异常后的退避时间。
        self.error_sleep_seconds = (
            error_sleep_seconds
            if error_sleep_seconds is not None
            else settings.news_sector_judge_error_sleep_seconds
        )

        if self.batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        if self.idle_sleep_seconds < 0:
            raise ValueError("idle_sleep_seconds 不能小于 0")

        if self.error_sleep_seconds < 0:
            raise ValueError("error_sleep_seconds 不能小于 0")

    async def run_once(self) -> NewsSectorJudgeBatchResult:
        """
        执行一轮批量消费。

        这个方法只跑一轮，适合：
        1. 单元测试；
        2. 手动调试；
        3. run_forever 内部循环调用。

        返回的 batch result 会用于日志统计和空闲判断。
        """

        result = await self.service.process_batch(batch_size=self.batch_size)

        logger.info(
            (
                "news_sector_judge_worker batch "
                "claimed=%s success=%s failed=%s"
            ),
            result.total_claimed_count,
            result.success_count,
            result.failed_count,
        )

        return result

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """
        常驻运行 worker。

        循环逻辑：
        1. 调用 run_once 处理一批新闻；
        2. 如果本轮没有领取到任务，按 idle_sleep_seconds 等待；
        3. 如果循环本身出现未预期异常，记录日志并按 error_sleep_seconds 等待；
        4. stop_event 被设置时退出。

        stop_event 通常由 SIGINT/SIGTERM 信号处理器设置，用于优雅停止进程。
        """

        active_stop_event = stop_event or asyncio.Event()

        logger.info("news_sector_judge_worker started")

        while not active_stop_event.is_set():
            try:
                result = await self.run_once()

                if result.total_claimed_count == 0:
                    await self._sleep(active_stop_event, self.idle_sleep_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("news_sector_judge_worker loop failed")
                await self._sleep(active_stop_event, self.error_sleep_seconds)

        logger.info("news_sector_judge_worker stopped")

    @staticmethod
    async def _sleep(stop_event: asyncio.Event, seconds: float) -> None:
        """
        可被 stop_event 打断的异步 sleep。

        直接 asyncio.sleep(seconds) 无法在停止信号到来时立即醒来。这里用
        wait_for(stop_event.wait(), timeout=seconds)，可以做到：
        - 超时：正常结束休眠，继续下一轮；
        - stop_event 被设置：立刻结束休眠，让 run_forever 尽快退出。
        """

        if seconds <= 0:
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return


def configure_logging() -> None:
    """
    配置 worker 进程日志。

    日志级别读取 LOG_LEVEL，格式保持和 scheduler 一致，方便两个进程的日志并排
    查看。force=True 用于确保命令行直接启动时配置能生效。
    """

    settings = get_settings()
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        force=True,
    )


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    """
    安装进程停止信号处理器。

    收到 SIGINT 或 SIGTERM 时只设置 stop_event，不在信号回调里直接关闭数据库或
    取消任务。真正的退出动作由 run_forever 和 run_worker 的 finally 完成。
    Windows 某些运行环境不支持 loop.add_signal_handler，所以这里会降级记录警告。
    """

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("signal handlers unavailable, signal=%s", sig.name)


async def run_worker() -> None:
    """
    worker 进程的异步入口。

    负责把启动前准备和退出后清理串起来：
    1. 初始化日志；
    2. 创建 service 并确保索引；
    3. 创建 worker 和 stop_event；
    4. 安装信号处理器；
    5. 常驻运行；
    6. 退出时关闭 MongoDB client。
    """

    configure_logging()

    service = NewsSectorJudgeService()
    await service.ensure_indexes()

    worker = NewsSectorJudgeWorker(service=service)
    stop_event = asyncio.Event()

    try:
        install_signal_handlers(stop_event)
        await worker.run_forever(stop_event)
    finally:
        from app.db.mongo import client

        client.close()


def main() -> None:
    """
    worker 命令行入口。

    这个函数让模块可以通过 `python -m app.workers.news_sector_judge_worker`
    直接启动。KeyboardInterrupt 只做兜底日志，正常停止主要依赖 signal handler。
    """

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("news_sector_judge_worker stopping")


if __name__ == "__main__":
    main()
