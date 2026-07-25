from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinTranscript,
    DouyinWorkAnalysis,
    DouyinWorkStatus,
)
from app.services.douyin_creator_analysis_service import (
    DouyinCreatorAnalysisService,
)


def build_work(work_id: str = "work-1") -> DouyinCreatorWork:
    now = datetime.now(timezone.utc)
    return DouyinCreatorWork(
        work_id=work_id,
        creator_sec_uid="sec",
        creator_name="全能的野人",
        creator_short_id="203775400",
        description="盘后观点",
        published_at=now,
        publish_ts=int(now.timestamp()),
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        duration_ms=60_000,
        first_seen_at=now,
        fetched_at=now,
        status=DouyinWorkStatus(status="transcribing"),
        processing_attempts=1,
    )


class FakeRepository:
    def __init__(self, rows: list[DouyinCreatorWork]) -> None:
        self.rows = rows
        self.transcripts = []
        self.analyses = []
        self.transcription_failures = []
        self.analysis_failures = []
        self.index_calls = 0
        self.transition_modified_count = 1

    async def create_indexes(self):
        self.index_calls += 1

    async def claim_next_for_processing(self, **kwargs):
        return self.rows.pop(0) if self.rows else None

    async def mark_transcription_success(
        self, work_id, transcript, *, expected_attempt
    ):
        self.transcripts.append((work_id, transcript))
        return SimpleNamespace(modified_count=self.transition_modified_count)

    async def mark_transcription_failed(
        self, work_id, reason, *, expected_attempt, retry_delay_seconds
    ):
        self.transcription_failures.append((work_id, reason))
        return SimpleNamespace(modified_count=self.transition_modified_count)

    async def mark_analysis_success(self, work_id, analysis, *, expected_attempt):
        self.analyses.append((work_id, analysis))
        return SimpleNamespace(modified_count=self.transition_modified_count)

    async def mark_analysis_failed(
        self, work_id, reason, *, expected_attempt, retry_delay_seconds
    ):
        self.analysis_failures.append((work_id, reason))
        return SimpleNamespace(modified_count=self.transition_modified_count)


class FakeMediaProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    async def download_media(self, work_id: str) -> Path:
        self.calls.append(work_id)
        return self.path


class FakeTranscriber:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    def transcribe(self, media_path):
        if self.error:
            raise self.error
        return DouyinTranscript(
            text="博主认为市场会走独立行情。",
            provider="test",
            model="test",
            transcribed_at=datetime.now(timezone.utc),
        )


class FakeAnalyzer:
    model = "test-llm"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def analyze(self, **kwargs):
        if self.error:
            raise self.error
        return DouyinWorkAnalysis(
            summary="看好独立行情",
            sector_opinions=[],
            analysis_version="v1",
            analysis_model=self.model,
            analyzed_at=datetime.now(timezone.utc),
        )


def test_analysis_service_completes_transcription_and_llm(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    repo = FakeRepository([build_work()])
    service = DouyinCreatorAnalysisService(
        repository=repo,  # type: ignore[arg-type]
        media_provider=FakeMediaProvider(media),
        transcriber=FakeTranscriber(),
        analyzer=FakeAnalyzer(),
    )

    result = asyncio.run(service.process_once())

    assert result.success is True
    assert result.stage == "finished"
    assert repo.transcripts[0][1].text.startswith("博主认为")
    assert repo.analyses[0][1].summary == "看好独立行情"
    assert media.exists() is False


def test_analysis_service_distinguishes_asr_and_llm_failures(tmp_path: Path) -> None:
    first_media = tmp_path / "first.mp4"
    first_media.write_bytes(b"video")
    first_repo = FakeRepository([build_work("asr")])
    asr_result = asyncio.run(
        DouyinCreatorAnalysisService(
            repository=first_repo,  # type: ignore[arg-type]
            media_provider=FakeMediaProvider(first_media),
            transcriber=FakeTranscriber(error=RuntimeError("asr failed")),
            analyzer=FakeAnalyzer(),
        ).process_once()
    )
    assert asr_result.stage == "transcription"
    assert first_repo.transcription_failures == [("asr", "asr failed")]

    second_media = tmp_path / "second.mp4"
    second_media.write_bytes(b"video")
    second_repo = FakeRepository([build_work("llm")])
    llm_result = asyncio.run(
        DouyinCreatorAnalysisService(
            repository=second_repo,  # type: ignore[arg-type]
            media_provider=FakeMediaProvider(second_media),
            transcriber=FakeTranscriber(),
            analyzer=FakeAnalyzer(error=RuntimeError("llm failed")),
        ).process_once()
    )
    assert llm_result.stage == "analysis"
    assert second_repo.analysis_failures == [("llm", "llm failed")]


def test_analysis_service_batch_stops_when_empty(tmp_path: Path) -> None:
    repo = FakeRepository([])
    service = DouyinCreatorAnalysisService(
        repository=repo,  # type: ignore[arg-type]
        media_provider=SimpleNamespace(),  # type: ignore[arg-type]
        transcriber=SimpleNamespace(),  # type: ignore[arg-type]
        analyzer=SimpleNamespace(),  # type: ignore[arg-type]
    )
    result = asyncio.run(service.process_batch(batch_size=3))
    assert result.total_claimed_count == 0


def test_analysis_service_creates_indexes_on_startup(tmp_path: Path) -> None:
    repo = FakeRepository([])
    service = DouyinCreatorAnalysisService(
        repository=repo,  # type: ignore[arg-type]
        media_provider=FakeMediaProvider(tmp_path / "unused.mp4"),
        transcriber=FakeTranscriber(),
        analyzer=FakeAnalyzer(),
    )

    asyncio.run(service.ensure_indexes())

    assert repo.index_calls == 1


def test_analysis_retry_reuses_existing_transcript(tmp_path: Path) -> None:
    work = build_work("analysis-retry")
    work.transcript = DouyinTranscript(
        text="已有的完整转写",
        provider="test",
        model="test",
        transcribed_at=datetime.now(timezone.utc),
    )
    repo = FakeRepository([work])
    media_provider = FakeMediaProvider(tmp_path / "must-not-download.mp4")
    service = DouyinCreatorAnalysisService(
        repository=repo,  # type: ignore[arg-type]
        media_provider=media_provider,
        transcriber=FakeTranscriber(error=AssertionError("不应重复转写")),
        analyzer=FakeAnalyzer(),
    )

    result = asyncio.run(service.process_once())

    assert result.success is True
    assert media_provider.calls == []
    assert repo.transcripts[0][1].text == "已有的完整转写"


def test_analysis_service_stops_after_losing_processing_lease(tmp_path: Path) -> None:
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    repo = FakeRepository([build_work("lease-lost")])
    repo.transition_modified_count = 0
    service = DouyinCreatorAnalysisService(
        repository=repo,  # type: ignore[arg-type]
        media_provider=FakeMediaProvider(media),
        transcriber=FakeTranscriber(),
        analyzer=FakeAnalyzer(),
    )

    result = asyncio.run(service.process_once())

    assert result.success is False
    assert result.stage == "lease_lost"
    assert repo.analyses == []
