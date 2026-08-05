from types import SimpleNamespace

from app.workers.creator_content_extraction_worker import (
    CreatorContentExtractionWorker,
)
from app.workers.creator_opinion_analysis_worker import CreatorOpinionAnalysisWorker


class FakeBatchService:
    """提供 worker 构造测试所需的最小批处理服务。"""

    async def ensure_indexes(self) -> None:
        """模拟启动前幂等索引创建。"""

    async def process_batch(self, *, batch_size: int):
        """返回带标准统计字段的空批次。"""

        return SimpleNamespace(
            total_claimed_count=0,
            success_count=0,
            failed_count=0,
            batch_size=batch_size,
        )


def test_creator_pipeline_uses_two_independent_worker_process_types() -> None:
    """验证内容提取和 LLM 1 使用不同 worker 名称及独立服务实例。"""

    extraction_service = FakeBatchService()
    analysis_service = FakeBatchService()
    extraction = CreatorContentExtractionWorker(  # type: ignore[arg-type]
        service=extraction_service
    )
    analysis = CreatorOpinionAnalysisWorker(  # type: ignore[arg-type]
        service=analysis_service
    )

    assert extraction.worker_name == "creator_content_extraction_worker"
    assert analysis.worker_name == "creator_opinion_analysis_worker"
    assert extraction.service is extraction_service
    assert analysis.service is analysis_service
    assert extraction.service is not analysis.service
