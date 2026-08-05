from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.crawlers.creator_platforms.base import PlatformAccount
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorMarketEvidence,
    CreatorOpinion,
    CreatorOpinionAnalysisDisplay,
    CreatorOpinionRecord,
    CreatorOpinionVerification,
    CreatorWork,
    CreatorWorkAnalysis,
)
from app.services.creator_daily_verification_service import (
    CreatorDailyVerificationService,
)


AS_OF = datetime(2026, 7, 24, 15, 40, tzinfo=CN_TZ)
PUBLISHED_AT = datetime(2026, 7, 24, 12, 0, tzinfo=CN_TZ)


def account(creator_id: str, rank: int) -> PlatformAccount:
    return PlatformAccount(
        rank=rank,
        creator_id=creator_id,
        display_name=f"测试博主{rank}",
        platform="weibo",
        platform_account_id=f"account-{rank}",
        platform_id_type="weibo_uid",
        homepage_url=f"https://example.com/account-{rank}",
    )


def work(creator_id: str, number: int) -> CreatorWork:
    work_key = f"weibo:work-{number}"
    opinion = CreatorOpinion(
        opinion_id=f"{work_key}:1",
        work_key=work_key,
        target_type="sector",
        target_name="半导体",
        direction="bullish",
        stance_score=80,
        claim="半导体次日上涨",
        horizon="当天",
        valid_from=PUBLISHED_AT,
        valid_until=datetime(2026, 7, 24, 16, 0, tzinfo=CN_TZ),
        metric="板块涨跌幅",
        source_quote="明天看好半导体。",
    )
    return CreatorWork(
        creator_id=creator_id,
        creator_name=f"测试博主{number}",
        account_id=f"weibo:account-{number}",
        platform="weibo",
        platform_work_id=f"work-{number}",
        content_type="short_post",
        canonical_url=f"https://example.com/work-{number}",
        published_at=PUBLISHED_AT,
        first_seen_at=PUBLISHED_AT,
        fetched_at=PUBLISHED_AT,
        source_text="明天看好半导体。",
        extracted_text="明天看好半导体。",
        status={"status": "finished"},
        analysis=CreatorWorkAnalysis(
            summary="看好半导体。",
            opinions=[opinion],
            analysis_version="v1",
            analysis_model="content-test",
            analyzed_at=PUBLISHED_AT + timedelta(minutes=5),
        ),
    )


class FakeWorkRepository:
    def __init__(self):
        self.works = {"creator-1": [work("creator-1", 1)], "creator-2": [work("creator-2", 2)]}
        self.list_calls = []

    async def create_indexes(self):
        pass

    async def list_finished_works_by_keys(self, work_keys, **kwargs):
        self.list_calls.append(kwargs)
        return [
            item
            for rows in self.works.values()
            for item in rows
            if item.work_key in work_keys
        ]


class FakeOpinionRepository:
    def __init__(self):
        self.docs = {}
        self.settlements = []

    async def create_indexes(self):
        pass

    async def sync_work_opinions(self, work):
        document = self.docs.setdefault(
            work.creator_id,
            CreatorOpinionAnalysisDisplay(creator_name=work.creator_name),
        )
        document.pending_opinions.extend(
            [
                CreatorOpinionRecord(
                    opinion_id=item.opinion_id,
                    work_key=work.work_key,
                    platform=work.platform,
                    published_at_beijing=work.published_at_beijing,
                    target_type=item.target_type,
                    target_name=item.target_name,
                    direction=item.direction,
                    opinion=item.claim,
                    verification_date=item.verification_date,
                )
                for item in work.a_share_opinions
                if item.verification_date is not None
            ]
        )

    async def get_creator(self, *, creator_id, creator_name):
        return self.docs.setdefault(
            creator_id,
            CreatorOpinionAnalysisDisplay(creator_name=creator_name),
        )

    async def settle_opinions(self, *, creator_id, records, accuracy_score):
        self.settlements.append((creator_id, list(records), accuracy_score))
        current = self.docs[creator_id]
        ids = {record.opinion_id for record in records}
        self.docs[creator_id] = current.model_copy(
            update={
                "pending_opinions": [
                    item for item in current.pending_opinions if item.opinion_id not in ids
                ],
                "verified_opinions": [*current.verified_opinions, *records],
                "accuracy_score": accuracy_score,
            }
        )


class FakeEvidenceBuilder:
    def __init__(self):
        self.build_calls = []

    async def build_evidence(self, **kwargs):
        self.build_calls.append(kwargs)
        return SimpleNamespace(
            evidence=CreatorMarketEvidence(
                evidence_id="temporary-evidence",
                market_date="2026-07-24",
                as_of=AS_OF,
                facts={"market_mainline_targets": ["半导体"]},
                source="test",
                evidence_version="v1",
                generated_at=AS_OF,
            )
        )

    async def enrich_evidence(self, **kwargs):
        return kwargs["evidence"]


class FakeVerifier:
    def __init__(self):
        self.calls = []

    async def verify_work(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["work"].creator_id == "creator-2":
            raise RuntimeError("联网验证暂时不可用")
        return [
            CreatorOpinionVerification(
                opinion_id=opinion.opinion_id,
                verdict="corroborated",
                reason="收盘上涨，与观点一致。",
                evidence_refs=["facts.market_mainline_targets"],
            )
            for opinion in kwargs["work"].analysis.opinions
        ]


def test_run_moves_due_opinions_and_isolates_creator_failure() -> None:
    works = FakeWorkRepository()
    opinions = FakeOpinionRepository()
    evidence = FakeEvidenceBuilder()
    verifier = FakeVerifier()
    for rows in works.works.values():
        for item in rows:
            asyncio.run(opinions.sync_work_opinions(item))
    service = CreatorDailyVerificationService(
        work_repository=works,
        opinion_repository=opinions,
        evidence_builder=evidence,
        verifier=verifier,
        accounts=(account("creator-1", 1), account("creator-2", 2)),
    )

    result = asyncio.run(service.run(score_date="2026-07-24", as_of=AS_OF, concurrency=1))

    assert [item.status for item in result.results] == ["completed", "failed"]
    assert result.results[0].daily_score == 100
    assert result.results[0].score == 100
    assert len(evidence.build_calls) == 1
    assert opinions.settlements[0][0] == "creator-1"
    assert len(opinions.settlements[0][1]) == 1
    assert opinions.docs["creator-1"].pending_opinions == []
    assert len(opinions.docs["creator-1"].verified_opinions) == 1


def test_run_skips_market_evidence_when_no_opinion_is_due() -> None:
    class NoDueWorkRepository(FakeWorkRepository):
        async def list_finished_works_by_keys(self, work_keys, **kwargs):
            return []

    evidence = FakeEvidenceBuilder()
    service = CreatorDailyVerificationService(
        work_repository=NoDueWorkRepository(),
        opinion_repository=FakeOpinionRepository(),
        evidence_builder=evidence,
        verifier=FakeVerifier(),
        accounts=(account("creator-1", 1),),
    )
    result = asyncio.run(service.run(score_date="2026-07-24", as_of=AS_OF))

    assert result.evidence_id == "none:2026-07-24"
    assert evidence.build_calls == []
    assert result.results[0].reason == "今天没有到期观点。"


def test_run_keeps_pending_when_due_source_work_is_not_ready() -> None:
    class MissingWorkRepository(FakeWorkRepository):
        async def list_finished_works_by_keys(self, work_keys, **kwargs):
            return []

    works = MissingWorkRepository()
    opinions = FakeOpinionRepository()
    source_work = works.works["creator-1"][0]
    asyncio.run(opinions.sync_work_opinions(source_work))
    service = CreatorDailyVerificationService(
        work_repository=works,
        opinion_repository=opinions,
        evidence_builder=FakeEvidenceBuilder(),
        verifier=FakeVerifier(),
        accounts=(account("creator-1", 1),),
    )

    result = asyncio.run(service.run(score_date="2026-07-24", as_of=AS_OF))

    assert result.results[0].status == "failed"
    assert "保留待验证状态" in result.results[0].reason
    assert len(opinions.docs["creator-1"].pending_opinions) == 1
    assert opinions.settlements == []


def test_run_keeps_pending_when_verifier_omits_due_opinion() -> None:
    class EmptyVerifier:
        async def verify_work(self, **kwargs):
            return []

    works = FakeWorkRepository()
    opinions = FakeOpinionRepository()
    source_work = works.works["creator-1"][0]
    asyncio.run(opinions.sync_work_opinions(source_work))
    service = CreatorDailyVerificationService(
        work_repository=works,
        opinion_repository=opinions,
        evidence_builder=FakeEvidenceBuilder(),
        verifier=EmptyVerifier(),
        accounts=(account("creator-1", 1),),
    )

    result = asyncio.run(service.run(score_date="2026-07-24", as_of=AS_OF))

    assert result.results[0].status == "failed"
    assert "验证结果与提交观点集合不一致" in result.results[0].reason
    assert len(opinions.docs["creator-1"].pending_opinions) == 1
    assert opinions.settlements == []
