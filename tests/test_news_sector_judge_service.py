from __future__ import annotations

import asyncio

from app.models import News, NewsSectorLLMAnalysis
from app.services import NewsSectorJudgeService


def build_news(event_id: str = "news-1") -> News:
    return News(
        event_id=event_id,
        publish_time="2026-05-21 09:30:00",
        publish_ts=1779327000,
        title="光模块需求受 AI 数据中心拉动",
        content="多家公司表示高速光模块订单增长。",
        source="cls",
    )


class FakeNewsRepository:
    def __init__(self, rows: list[News]) -> None:
        self.rows = rows
        self.success_updates: list[tuple[str, list[NewsSectorLLMAnalysis]]] = []
        self.failed_updates: list[tuple[str, str]] = []

    async def create_indexes(self) -> None:
        return None

    async def claim_next_sector_judge_news(self) -> News | None:
        if not self.rows:
            return None

        return self.rows.pop(0)

    async def mark_sector_judge_success(
        self,
        event_id: str,
        analysis: list[NewsSectorLLMAnalysis],
    ) -> None:
        self.success_updates.append((event_id, analysis))

    async def mark_sector_judge_failed(
        self,
        event_id: str,
        reason: str,
    ) -> None:
        self.failed_updates.append((event_id, reason))


class FakeAnalyzer:
    def __init__(
        self,
        result: list[NewsSectorLLMAnalysis] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or [
            NewsSectorLLMAnalysis(
                sector_name="通信设备",
                sector_llm_analysis=None,
            )
        ]
        self.error = error
        self.calls: list[dict[str, str]] = []

    async def analyze(
        self,
        *,
        title: str,
        content: str,
        publish_time: str,
    ) -> list[NewsSectorLLMAnalysis]:
        self.calls.append(
            {
                "title": title,
                "content": content,
                "publish_time": publish_time,
            }
        )

        if self.error is not None:
            raise self.error

        return self.result


def test_news_sector_judge_service_process_once_success() -> None:
    repo = FakeNewsRepository([build_news()])
    analyzer = FakeAnalyzer()
    service = NewsSectorJudgeService(
        news_repository=repo,  # type: ignore[arg-type]
        analyzer=analyzer,
    )

    result = asyncio.run(service.process_once())

    assert result.processed is True
    assert result.success is True
    assert result.event_id == "news-1"
    assert result.sector_count == 1
    assert repo.success_updates[0][0] == "news-1"
    assert repo.success_updates[0][1][0].sector_name == "通信设备"
    assert repo.failed_updates == []
    assert analyzer.calls[0]["title"] == "光模块需求受 AI 数据中心拉动"


def test_news_sector_judge_service_process_once_failure_marks_failed() -> None:
    repo = FakeNewsRepository([build_news()])
    analyzer = FakeAnalyzer(error=RuntimeError("LLM timeout"))
    service = NewsSectorJudgeService(
        news_repository=repo,  # type: ignore[arg-type]
        analyzer=analyzer,
    )

    result = asyncio.run(service.process_once())

    assert result.processed is True
    assert result.success is False
    assert result.event_id == "news-1"
    assert result.error_message == "LLM timeout"
    assert repo.success_updates == []
    assert repo.failed_updates == [("news-1", "LLM timeout")]


def test_news_sector_judge_service_process_batch_stops_when_no_pending() -> None:
    repo = FakeNewsRepository([build_news("news-1"), build_news("news-2")])
    service = NewsSectorJudgeService(
        news_repository=repo,  # type: ignore[arg-type]
        analyzer=FakeAnalyzer(),
    )

    result = asyncio.run(service.process_batch(batch_size=5))

    assert result.total_claimed_count == 2
    assert result.success_count == 2
    assert result.failed_count == 0
    assert [item[0] for item in repo.success_updates] == ["news-1", "news-2"]
