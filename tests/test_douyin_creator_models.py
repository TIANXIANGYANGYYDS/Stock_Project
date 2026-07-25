from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.douyin_creator_work import (
    DouyinCreatorWork,
    DouyinSectorOpinion,
    DouyinSectorOpinionDraft,
    DouyinTranscript,
    DouyinTranscriptSegment,
    DouyinWorkAnalysis,
    DouyinWorkAnalysisDraft,
    DouyinWorkStatus,
    FetchedDouyinWork,
)


NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def build_fetched_work(**overrides) -> FetchedDouyinWork:
    values = {
        "work_id": "7530000000000000001",
        "creator_sec_uid": "MS4wLjABAAAA-test",
        "creator_name": "测试博主",
        "creator_short_id": "douyin-test",
        "description": "盘前关注算力基础设施。",
        "published_at": NOW,
        "publish_ts": int(NOW.timestamp()),
        "canonical_url": "https://www.douyin.com/video/7530000000000000001",
        "duration_ms": 60_000,
        "first_seen_at": NOW,
        "fetched_at": NOW,
    }
    values.update(overrides)
    return FetchedDouyinWork(**values)


def build_transcript() -> DouyinTranscript:
    return DouyinTranscript(
        text="算力基础设施仍有增量需求。",
        segments=[
            DouyinTranscriptSegment(
                start_ms=0,
                end_ms=2_000,
                text="算力基础设施仍有增量需求。",
            )
        ],
        language="zh-CN",
        provider="local_whisper",
        model="large-v3",
        transcribed_at=NOW,
    )


def build_analysis() -> DouyinWorkAnalysis:
    return DouyinWorkAnalysis(
        summary="博主认为算力基础设施需求延续。",
        sector_opinions=[
            DouyinSectorOpinion(
                opinion_id="7530000000000000001:1",
                sector_name="通信设备",
                stance_score=65,
                reason="光模块需求可能继续增长。",
            )
        ],
        analysis_version="douyin_creator_analysis_v1",
        analysis_model="test-model",
        analyzed_at=NOW,
    )


def test_fetched_work_and_creator_work_defaults() -> None:
    fetched = build_fetched_work()
    work = DouyinCreatorWork(**fetched.model_dump(mode="python"))

    assert fetched.work_id == "7530000000000000001"
    assert fetched.published_at_cn == "2026-07-24T16:30:00.000+08:00"
    assert fetched.first_seen_at_cn == "2026-07-24T16:30:00.000+08:00"
    assert fetched.fetched_at_cn == "2026-07-24T16:30:00.000+08:00"
    assert work.status.status == "pending_transcription"
    assert work.transcript is None
    assert work.analysis is None


def test_nested_results_include_china_time_fields() -> None:
    transcript = build_transcript()
    analysis = build_analysis()

    assert transcript.transcribed_at_cn == "2026-07-24T16:30:00.000+08:00"
    assert analysis.analyzed_at_cn == "2026-07-24T16:30:00.000+08:00"


def test_processing_times_include_china_time_fields() -> None:
    work = DouyinCreatorWork(
        **build_fetched_work().model_dump(mode="python"),
        processing_started_at=NOW,
        next_retry_at=NOW,
    )

    assert work.processing_started_at_cn == "2026-07-24T16:30:00.000+08:00"
    assert work.next_retry_at_cn == "2026-07-24T16:30:00.000+08:00"


def test_transcript_segment_rejects_reversed_range() -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        DouyinTranscriptSegment(start_ms=2_000, end_ms=1_000, text="无效分段")


def test_analysis_draft_allows_no_sector_but_rejects_duplicates() -> None:
    draft = DouyinWorkAnalysisDraft(summary="只讨论整体市场。")
    assert draft.sector_opinions == []

    with pytest.raises(ValidationError, match="重复板块"):
        DouyinWorkAnalysisDraft(
            summary="重复板块",
            sector_opinions=[
                DouyinSectorOpinionDraft(
                    sector_name="通信设备",
                    stance_score=60,
                    reason="第一条",
                ),
                DouyinSectorOpinionDraft(
                    sector_name="通信设备",
                    stance_score=40,
                    reason="第二条",
                ),
            ],
        )


def test_persisted_analysis_keeps_three_sector_limit() -> None:
    with pytest.raises(ValidationError, match="at most 3"):
        DouyinWorkAnalysis(
            summary="板块过多",
            sector_opinions=[
                DouyinSectorOpinion(
                    opinion_id=f"work:{index}",
                    sector_name=f"行业{index}",
                    stance_score=10,
                    reason="测试",
                )
                for index in range(4)
            ],
            analysis_version="v1",
            analysis_model="test",
            analyzed_at=NOW,
        )


def test_finished_work_requires_transcript_and_analysis() -> None:
    raw = build_fetched_work().model_dump(mode="python")

    with pytest.raises(ValidationError, match="finished"):
        DouyinCreatorWork(
            **raw,
            status=DouyinWorkStatus(status="finished"),
        )

    work = DouyinCreatorWork(
        **raw,
        status=DouyinWorkStatus(status="finished"),
        transcript=build_transcript(),
        analysis=build_analysis(),
    )
    assert work.analysis is not None
    assert work.analysis.sector_opinions[0].opinion_id.endswith(":1")


def test_analysis_stage_requires_transcript() -> None:
    with pytest.raises(ValidationError, match="transcript"):
        DouyinCreatorWork(
            **build_fetched_work().model_dump(mode="python"),
            status=DouyinWorkStatus(status="analyzing"),
        )


def test_fetched_work_requires_aware_datetimes() -> None:
    with pytest.raises(ValidationError):
        build_fetched_work(fetched_at=datetime(2026, 7, 24, 8, 30))
