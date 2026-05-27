from .base_worker import (
    BasePollingWorker,
    BatchProcessingService,
    BatchProcessResult,
    configure_worker_logging,
    install_signal_handlers,
    run_worker_process,
)

__all__ = (
    "BasePollingWorker",
    "BatchProcessingService",
    "BatchProcessResult",
    "NewsSectorDetailWorker",
    "NewsSectorJudgeWorker",
    "configure_worker_logging",
    "install_signal_handlers",
    "run_worker_process",
)


def __getattr__(name: str):
    """
    懒加载具体 worker 类。

    不在包初始化时 import news_sector_judge_worker / news_sector_detail_worker，
    避免 `python -m app.workers.xxx` 时 runpy 发现目标模块已提前进入 sys.modules。
    """

    if name == "NewsSectorJudgeWorker":
        from .news_sector_judge_worker import NewsSectorJudgeWorker

        return NewsSectorJudgeWorker

    if name == "NewsSectorDetailWorker":
        from .news_sector_detail_worker import NewsSectorDetailWorker

        return NewsSectorDetailWorker

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
