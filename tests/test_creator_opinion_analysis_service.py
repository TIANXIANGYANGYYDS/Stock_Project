from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.creator_monitoring import (
    CreatorOpinion,
    CreatorWork,
    CreatorWorkAnalysis,
    CreatorWorkStatus,
)
from app.services.creator_opinion_analysis_service import CreatorOpinionAnalysisService


UTC = timezone.utc
NOW = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)


def analysis() -> CreatorWorkAnalysis:
    return CreatorWorkAnalysis(
        summary="看好半导体。",
        opinions=[
            CreatorOpinion(
                opinion_id="douyin:work-1:1",
                work_key="douyin:work-1",
                target_type="sector",
                target_name="半导体",
                direction="bullish",
                stance_score=70,
                claim="半导体次日走强",
                horizon="次日",
                valid_from=NOW,
                valid_until=datetime(2026, 7, 24, 8, 0, tzinfo=UTC),
                metric="相对收益",
                confidence=0.8,
                source_quote="半导体次日走强",
            )
        ],
        analysis_version="v1",
        analysis_model="test",
        analyzed_at=NOW,
    )


def work(*, finished: bool = False) -> CreatorWork:
    values = dict(
        creator_id="creator-1",
        account_id="douyin:account-1",
        platform="douyin",
        platform_work_id="work-1",
        content_type="video",
        canonical_url="https://example.com/work-1",
        published_at=NOW,
        first_seen_at=NOW,
        fetched_at=NOW,
        extracted_text="半导体次日走强",
        processing_attempts=1,
    )
    if finished:
        values.update(status=CreatorWorkStatus(status="finished"), analysis=analysis())
    else:
        values.update(status=CreatorWorkStatus(status="analyzing"))
    return CreatorWork(**values)


class FakeRepository:
    def __init__(self, rows):
        self.rows = list(rows)
        self.saved = []
        self.failed = []

    async def claim_next_for_analysis(self, **kwargs):
        return self.rows.pop(0) if self.rows else None

    async def mark_analysis_success(self, work_key, value, *, expected_attempt):
        self.saved.append((work_key, value, expected_attempt))
        return SimpleNamespace(modified_count=1)

    async def mark_analysis_failed(self, work_key, reason, **kwargs):
        self.failed.append((work_key, reason))
        return SimpleNamespace(modified_count=1)

class FakeAnalyzer:
    """返回固定内容分析结果的单作品分析器替身。"""

    async def analyze(self, **kwargs):
        """忽略作品参数并返回预先构造的结构化观点。"""

        return analysis()


class FakeOpinionRepository:
    def __init__(self) -> None:
        self.rows = []

    async def create_indexes(self) -> None:
        pass

    async def sync_work_opinions(self, value: CreatorWork) -> None:
        self.rows.append(value)


class FailingOpinionRepository(FakeOpinionRepository):
    async def sync_work_opinions(self, value: CreatorWork) -> None:
        raise RuntimeError("pending sync unavailable")


def test_process_once_persists_analysis() -> None:
    repository = FakeRepository([work()])
    opinions = FakeOpinionRepository()
    service = CreatorOpinionAnalysisService(
        repository=repository,  # type: ignore[arg-type]
        opinion_repository=opinions,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.process_once())

    assert result.success is True
    assert result.stage == "finished"
    assert repository.saved[0][0] == "douyin:work-1"
    assert opinions.rows[0].status.status == "finished"
    assert opinions.rows[0].a_share_opinions[0].claim == "半导体次日走强"


def test_process_once_retries_when_pending_opinion_sync_fails() -> None:
    repository = FakeRepository([work()])
    service = CreatorOpinionAnalysisService(
        repository=repository,  # type: ignore[arg-type]
        opinion_repository=FailingOpinionRepository(),
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.process_once())

    assert result.success is False
    assert result.stage == "analysis"
    assert repository.saved == []
    assert repository.failed == [("douyin:work-1", "pending sync unavailable")]


def test_analysis_moves_weekend_verification_to_next_trade_day() -> None:
    class WeekendAnalyzer:
        async def analyze(self, **kwargs):
            result = analysis()
            opinion = result.opinions[0].model_copy(
                update={"valid_until": datetime(2026, 7, 25, 16, tzinfo=UTC)}
            )
            return result.model_copy(update={"opinions": [opinion]})

    service = CreatorOpinionAnalysisService(
        repository=FakeRepository([work()]),
        opinion_repository=FakeOpinionRepository(),  # type: ignore[arg-type]
        analyzer=WeekendAnalyzer(),  # type: ignore[arg-type]
    )

    result = asyncio.run(service.analyze_work(work()))

    assert result.opinions[0].verification_date == "2026-07-27"
