from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from typing import Any, Protocol

from app.crawlers.creator_platforms import CREATOR_ACCOUNTS, PlatformAccount
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorMarketEvidence,
    CreatorOpinion,
    CreatorOpinionAnalysisDisplay,
    CreatorOpinionRecord,
    CreatorOpinionVerification,
    CreatorWork,
    VERDICT_SCORES,
    beijing_time_text,
)
from app.repositories.creator_monitoring_repository import (
    CreatorOpinionAnalysisRepository,
    CreatorWorkRepository,
)
from app.services.creator_market_evidence_service import CreatorMarketEvidenceService
from app.services.creator_opinion_verification_service import (
    CreatorOpinionVerificationService,
)
from app.services.trading_calendar_service import resolve_morning_trade_dates


logger = logging.getLogger(__name__)
DEFAULT_VERIFICATION_CONCURRENCY = 1
FORMULA_VERSION = "creator_cumulative_accuracy_v1"


def current_source_window_bounds(evaluation_date: date | str) -> tuple[datetime, datetime]:
    """返回评价交易日前一次交易日至评价日零点的来源日期窗口。"""

    target = (
        evaluation_date
        if isinstance(evaluation_date, date) and not isinstance(evaluation_date, datetime)
        else date.fromisoformat(str(evaluation_date).strip())
    )
    decision = resolve_morning_trade_dates(target)
    if not decision.is_current_trade_day:
        raise ValueError("evaluation_date 必须是 A 股交易日")
    start = datetime.combine(
        date.fromisoformat(decision.prev_trade_date),
        time.min,
        tzinfo=CN_TZ,
    )
    end = datetime.combine(target, time.min, tzinfo=CN_TZ)
    return start, end


class FinishedWorkReader(Protocol):
    async def create_indexes(self) -> None: ...

    async def list_finished_works_by_keys(
        self,
        work_keys: Sequence[str],
        *,
        available_at: datetime,
    ) -> list[CreatorWork]: ...


class OpinionStore(Protocol):
    async def create_indexes(self) -> None: ...

    async def get_creator(
        self,
        *,
        creator_id: str,
        creator_name: str,
    ) -> CreatorOpinionAnalysisDisplay: ...

    async def settle_opinions(
        self,
        *,
        creator_id: str,
        records: Sequence[CreatorOpinionRecord],
        accuracy_score: float | None,
    ) -> Any: ...


class EvidenceBuildResult(Protocol):
    evidence: CreatorMarketEvidence


class TransientMarketEvidenceBuilder(Protocol):
    async def build_evidence(
        self,
        *,
        market_date: date | str,
        as_of: datetime | None = None,
    ) -> EvidenceBuildResult: ...

    async def enrich_evidence(
        self,
        *,
        evidence: CreatorMarketEvidence,
        target_names: Collection[str],
        condition_names: Collection[str] = (),
        as_of: datetime,
    ) -> CreatorMarketEvidence: ...


class WorkOpinionVerifier(Protocol):
    async def verify_work(
        self,
        *,
        work: CreatorWork,
        evidence: CreatorMarketEvidence,
        evaluation_date: date | str,
        source_window_start: date | str | None = None,
        market_mainline_targets: Collection[str] = (),
    ) -> list[CreatorOpinionVerification]: ...


@dataclass(frozen=True)
class CreatorDailyVerificationRunResult:
    creator_id: str
    status: str
    evaluated_opinion_count: int
    daily_score: float | None
    score: float | None
    reason: str


@dataclass(frozen=True)
class CreatorDailyVerificationBatchResult:
    score_date: str
    evidence_id: str
    results: tuple[CreatorDailyVerificationRunResult, ...]


class CreatorDailyVerificationService:
    """在收盘后把到期观点从 pending 原地移动到 verified。"""

    def __init__(
        self,
        *,
        work_repository: FinishedWorkReader | None = None,
        opinion_repository: OpinionStore | None = None,
        evidence_builder: TransientMarketEvidenceBuilder | None = None,
        verifier: WorkOpinionVerifier | None = None,
        accounts: tuple[PlatformAccount, ...] = CREATOR_ACCOUNTS,
    ) -> None:
        self.work_repository = work_repository or CreatorWorkRepository()
        self.opinion_repository = opinion_repository or CreatorOpinionAnalysisRepository()
        self.evidence_builder = evidence_builder or CreatorMarketEvidenceService()
        self.verifier = verifier or CreatorOpinionVerificationService()
        creators: dict[str, str] = {}
        for account in accounts:
            if account.enabled:
                creators.setdefault(account.creator_id, account.display_name)
        self.creators = tuple(creators.items())

    async def ensure_indexes(self) -> None:
        """只创建作品表和博主观点表索引。"""

        await asyncio.gather(
            self.work_repository.create_indexes(),
            self.opinion_repository.create_indexes(),
        )

    async def run(
        self,
        *,
        score_date: date | str,
        as_of: datetime,
        concurrency: int = DEFAULT_VERIFICATION_CONCURRENCY,
    ) -> CreatorDailyVerificationBatchResult:
        """串行或按显式小并发验证目标交易日到期观点。"""

        if as_of.tzinfo is None:
            raise ValueError("as_of 必须包含时区")
        if concurrency <= 0:
            raise ValueError("concurrency 必须大于 0")
        score_day = self._parse_date(score_date)
        decision = resolve_morning_trade_dates(score_day)
        if not decision.is_current_trade_day:
            raise ValueError("评分日期必须是 A 股交易日")
        active_as_of = as_of.astimezone(CN_TZ)
        if active_as_of < datetime.combine(score_day, time(15), tzinfo=CN_TZ):
            raise ValueError("收盘验证只能在评价日 15:00 后执行")

        current_by_creator: dict[str, CreatorOpinionAnalysisDisplay] = {}
        due_ids_by_creator: dict[str, set[str]] = {}
        due_work_keys: list[str] = []
        for creator_id, creator_name in self.creators:
            current = await self.opinion_repository.get_creator(
                creator_id=creator_id,
                creator_name=creator_name,
            )
            current_by_creator[creator_id] = current
            due = [
                item
                for item in current.pending_opinions
                if item.verification_date == score_day.isoformat()
            ]
            due_ids_by_creator[creator_id] = {item.opinion_id for item in due}
            due_work_keys.extend(item.work_key for item in due)

        if not due_work_keys:
            empty_results: list[CreatorDailyVerificationRunResult] = []
            for creator_id, creator_name in self.creators:
                current = current_by_creator[creator_id]
                empty_results.append(
                    CreatorDailyVerificationRunResult(
                        creator_id=creator_id,
                        status="completed",
                        evaluated_opinion_count=0,
                        daily_score=None,
                        score=current.accuracy_score,
                        reason="今天没有到期观点。",
                    )
                )
            return CreatorDailyVerificationBatchResult(
                score_date=score_day.isoformat(),
                evidence_id=f"none:{score_day.isoformat()}",
                results=tuple(empty_results),
            )

        works = await self.work_repository.list_finished_works_by_keys(
            due_work_keys,
            available_at=active_as_of,
        )
        due_groups: dict[str, list[tuple[CreatorWork, list[CreatorOpinion]]]] = {
            creator_id: [] for creator_id, _ in self.creators
        }
        for work in works:
            due_ids = due_ids_by_creator.get(work.creator_id, set())
            due = [
                opinion
                for opinion in work.a_share_opinions
                if opinion.opinion_id in due_ids
                and opinion.verification_date == score_day.isoformat()
            ]
            if due:
                due_groups[work.creator_id].append((work, due))

        loaded_work_keys = {work.work_key for work in works}
        missing_due_by_creator = {
            creator_id: {
                item.opinion_id
                for item in current_by_creator[creator_id].pending_opinions
                if item.verification_date == score_day.isoformat()
                and item.work_key not in loaded_work_keys
            }
            for creator_id, _ in self.creators
        }

        build_result = await self.evidence_builder.build_evidence(
            market_date=score_day,
            as_of=active_as_of,
        )
        evidence = build_result.evidence
        targets, conditions = self._collect_evidence_names(due_groups)
        if targets or conditions:
            evidence = await self.evidence_builder.enrich_evidence(
                evidence=evidence,
                target_names=targets,
                condition_names=conditions,
                as_of=active_as_of,
            )

        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(
            creator_id: str,
            creator_name: str,
        ) -> CreatorDailyVerificationRunResult:
            async with semaphore:
                return await self._run_creator(
                    creator_id=creator_id,
                    creator_name=creator_name,
                    current=current_by_creator[creator_id],
                    due_works=due_groups[creator_id],
                    missing_due_opinion_ids=missing_due_by_creator[creator_id],
                    score_day=score_day,
                    source_window_start=decision.prev_trade_date,
                    as_of=active_as_of,
                    evidence=evidence,
                )

        results = await asyncio.gather(
            *(run_one(creator_id, creator_name) for creator_id, creator_name in self.creators)
        )
        return CreatorDailyVerificationBatchResult(
            score_date=score_day.isoformat(),
            evidence_id=evidence.evidence_id,
            results=tuple(results),
        )

    async def _run_creator(
        self,
        *,
        creator_id: str,
        creator_name: str,
        current: CreatorOpinionAnalysisDisplay,
        due_works: Sequence[tuple[CreatorWork, list[CreatorOpinion]]],
        missing_due_opinion_ids: Collection[str],
        score_day: date,
        source_window_start: date | str,
        as_of: datetime,
        evidence: CreatorMarketEvidence,
    ) -> CreatorDailyVerificationRunResult:
        if missing_due_opinion_ids:
            return CreatorDailyVerificationRunResult(
                creator_id=creator_id,
                status="failed",
                evaluated_opinion_count=0,
                daily_score=None,
                score=current.accuracy_score,
                reason=(
                    "到期观点的来源作品尚未完成内容分析，保留待验证状态，"
                    f"缺失观点数={len(missing_due_opinion_ids)}。"
                ),
            )
        verified_ids = {item.opinion_id for item in current.verified_opinions}
        records: list[CreatorOpinionRecord] = []
        try:
            for work, opinions in due_works:
                pending = [item for item in opinions if item.opinion_id not in verified_ids]
                if not pending:
                    continue
                selected_work = self._work_with_opinions(work, pending)
                evaluations = await self.verifier.verify_work(
                    work=selected_work,
                    evidence=evidence,
                    evaluation_date=score_day,
                    source_window_start=source_window_start,
                    market_mainline_targets=self._market_mainline_targets(evidence),
                )
                records.extend(
                    self._verified_records(
                        work=selected_work,
                        evaluations=evaluations,
                        verified_at=as_of,
                    )
                )
            all_scores = [
                item.score
                for item in [*current.verified_opinions, *records]
                if item.score is not None
            ]
            accuracy = self._score_values(all_scores)
            await self.opinion_repository.settle_opinions(
                creator_id=creator_id,
                records=records,
                accuracy_score=accuracy,
            )
            return CreatorDailyVerificationRunResult(
                creator_id=creator_id,
                status="completed",
                evaluated_opinion_count=len(records),
                daily_score=self._score_values(
                    [item.score for item in records if item.score is not None]
                ),
                score=accuracy,
                reason="到期观点验证完成。" if records else "今天没有到期观点。",
            )
        except Exception as exc:
            reason = (str(exc) or type(exc).__name__)[:1000]
            logger.exception(
                "creator opinion settlement failed creator=%s date=%s",
                creator_id,
                score_day.isoformat(),
            )
            return CreatorDailyVerificationRunResult(
                creator_id=creator_id,
                status="failed",
                evaluated_opinion_count=0,
                daily_score=None,
                score=current.accuracy_score,
                reason=reason,
            )

    @staticmethod
    def _opinions_due_for_close(
        opinions: Collection[CreatorOpinion],
        score_day: date,
    ) -> list[CreatorOpinion]:
        return [
            item
            for item in opinions
            if item.market_scope == "a_share"
            and item.verifiable
            and item.verification_date == score_day.isoformat()
        ]

    @staticmethod
    def _work_with_opinions(
        work: CreatorWork,
        opinions: Sequence[CreatorOpinion],
    ) -> CreatorWork:
        if work.analysis is None:
            raise ValueError("作品尚未完成观点分析")
        return work.model_copy(
            update={"analysis": work.analysis.model_copy(update={"opinions": list(opinions)})}
        )

    @staticmethod
    def _verified_records(
        *,
        work: CreatorWork,
        evaluations: Sequence[CreatorOpinionVerification],
        verified_at: datetime,
    ) -> list[CreatorOpinionRecord]:
        if work.analysis is None:
            raise ValueError("作品尚未完成观点分析")
        by_id = {item.opinion_id: item for item in evaluations}
        if set(by_id) != {item.opinion_id for item in work.analysis.opinions}:
            raise ValueError("验证结果与提交观点集合不一致")
        return [
            CreatorOpinionRecord(
                opinion_id=opinion.opinion_id,
                work_key=work.work_key,
                platform=work.platform,
                published_at_beijing=work.published_at_beijing,
                target_type=opinion.target_type,
                target_name=opinion.target_name,
                direction=opinion.direction,
                opinion=opinion.claim,
                verification_date=opinion.verification_date,
                verified_at_beijing=beijing_time_text(verified_at),
                verdict=by_id[opinion.opinion_id].verdict,
                score=VERDICT_SCORES[by_id[opinion.opinion_id].verdict],
                reason=by_id[opinion.opinion_id].reason,
            )
            for opinion in work.analysis.opinions
            if opinion.verification_date is not None
        ]

    @classmethod
    def _collect_evidence_names(
        cls,
        due_groups: dict[str, list[tuple[CreatorWork, list[CreatorOpinion]]]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        targets: list[str] = []
        conditions: list[str] = []
        for rows in due_groups.values():
            for _, opinions in rows:
                for opinion in opinions:
                    if opinion.target_type in {"sector", "theme"}:
                        targets.append(opinion.target_name)
                    conditions.extend(opinion.conditions)
        return (
            tuple(dict.fromkeys(item.strip() for item in targets if item.strip())),
            tuple(dict.fromkeys(item.strip() for item in conditions if item.strip())),
        )

    @staticmethod
    def _score_values(values: Collection[float]) -> float | None:
        if not values:
            return None
        return round((sum(values) / len(values) + 1.0) * 50.0, 2)

    @staticmethod
    def _market_mainline_targets(evidence: CreatorMarketEvidence) -> tuple[str, ...]:
        return tuple(
            str(item).strip()
            for item in evidence.facts.get("market_mainline_targets", [])
            if str(item).strip()
        )

    @staticmethod
    def _parse_date(value: date | str) -> date:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("datetime 日期参数必须包含时区")
            return value.astimezone(CN_TZ).date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip())


__all__ = [
    "FORMULA_VERSION",
    "current_source_window_bounds",
    "CreatorDailyVerificationBatchResult",
    "CreatorDailyVerificationRunResult",
    "CreatorDailyVerificationService",
]
