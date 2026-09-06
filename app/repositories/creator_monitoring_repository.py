from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from time import monotonic
from typing import Any

from pymongo import ReturnDocument, UpdateOne
from pymongo.results import BulkWriteResult, UpdateResult

from app.models.creator_monitoring import (
    CN_TZ,
    CreatorOpinionAnalysisDisplay,
    CreatorOpinionRecord,
    CreatorOpinion,
    CreatorWork,
    CreatorWorkAnalysis,
    CreatorWorkStatus,
)
from app.repositories.base import BaseMongoRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreatorWorkBatchWriteResult:
    """记录一次幂等批量写入博主作品的数量统计。"""

    # 原本不存在并已成功插入的作品键数量。
    inserted_count: int
    # 输入中已经存在于集合内的作品键数量。
    existing_count: int


def _require_aware(value: datetime, field_name: str) -> datetime:
    """要求时间值包含时区，并将其规范化为中国时区。

    仓储边界在构造 MongoDB 过滤条件前调用该函数，避免无时区的调用参数意外
    偏移作品发布时间或历史时点窗口。``field_name`` 会写入校验错误，以便定位
    具体的错误参数。
    """

    if value.tzinfo is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(CN_TZ)


def _restore_mongo_timezones(value: Any) -> Any:
    """递归将 MongoDB 时间恢复为带中国时区的值。

    根据客户端配置，Motor 可能返回不含 ``tzinfo`` 的 UTC 时间。函数会遍历
    字典和列表，使嵌套分析、观点时间与顶层时间一样满足 Pydantic 的
    ``AwareDatetime`` 字段要求。
    """

    if isinstance(value, datetime):
        utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return utc_value.astimezone(CN_TZ)
    if isinstance(value, dict):
        return {key: _restore_mongo_timezones(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_mongo_timezones(item) for item in value]
    return value


class CreatorWorkRepository(BaseMongoRepository):
    """存储博主作品，并协调带尝试次数隔离的处理状态迁移。"""

    # ``BaseMongoRepository`` 据此选择集合并完成文档转换的 Pydantic 模型。
    model_class = CreatorWork
    # 租约过期任务转为终止失败状态前允许领取的默认次数。
    max_processing_attempts = 3
    # Top 5 博主优先级在进程内缓存十五分钟，避免每领取一条作品都重复查询排行榜。
    priority_cache_seconds = 15 * 60

    def __init__(self, database: Any | None = None) -> None:
        """绑定统一作品集合，并初始化尚未加载的排行榜优先级缓存。"""

        super().__init__(database=database)
        # 当前缓存的最近一期评分 Top 5 逻辑博主 ID。
        self._priority_creator_ids: tuple[str, ...] = ()
        # 单调时钟达到该值后需要重新读取最近排行榜。
        self._priority_cache_expires_at = 0.0

    async def create_indexes(self) -> None:
        """创建作品身份、时间窗口及处理租约索引。"""

        await self.collection.create_index("work_key", unique=True, name="uk_work_key")
        await self.collection.create_index(
            [("account_id", 1), ("published_at", -1)],
            name="idx_account_published_at",
        )
        await self.collection.create_index(
            [("creator_id", 1), ("published_at", -1)],
            name="idx_creator_published_at",
        )
        await self.collection.create_index(
            [("status.status", 1), ("processing_started_at", 1)],
            name="idx_status_processing_lease",
        )
        await self.collection.create_index(
            [("status.status", 1), ("creator_id", 1), ("published_at", 1)],
            name="idx_status_creator_published_at",
        )
        await self.collection.create_index(
            [("is_a_share_relevant", 1), ("published_at", -1)],
            name="idx_a_share_relevant_published_at",
        )

    async def save_works(
        self,
        rows: Sequence[CreatorWork | Mapping[str, Any]],
    ) -> CreatorWorkBatchWriteResult:
        """插入尚未见过的作品，并统计新增与已存在的输入记录。

        通过 ``$setOnInsert`` 刻意保持已有文档不变，避免后续抓取重置内容提取
        尝试次数或覆盖作品分析结果。
        """

        if not rows:
            return CreatorWorkBatchWriteResult(0, 0)
        result = await self.upsert_works(rows)
        inserted_count = int(getattr(result, "upserted_count", 0)) if result else 0
        return CreatorWorkBatchWriteResult(
            inserted_count=inserted_count,
            existing_count=max(len(rows) - inserted_count, 0),
        )

    async def upsert_works(
        self,
        rows: Sequence[CreatorWork | Mapping[str, Any]],
    ) -> BulkWriteResult | None:
        """按 ``work_key`` 构造无序且仅插入的更新插入操作。

        操作列表为空时，基础仓储返回 ``None``；否则返回原始批量写入结果，
        供调用方统计插入数量。
        """

        operations = []
        for row in rows:
            document = self.build_document(row)
            operations.append(
                UpdateOne(
                    {"work_key": document["work_key"]},
                    {"$setOnInsert": document},
                    upsert=True,
                )
            )
        return await self.bulk_write(operations, ordered=False)

    async def get_existing_work_keys(self, work_keys: Sequence[str]) -> set[str]:
        """返回候选作品键中已经持久化的规范化子集。

        查询前会移除空白值和重复值，使重复分页时的 ``$in`` 过滤条件保持有界。
        """

        normalized = list(
            dict.fromkeys(str(item).strip() for item in work_keys if str(item).strip())
        )
        if not normalized:
            return set()
        rows = await self.find_many(
            {"work_key": {"$in": normalized}},
            projection={"_id": 0, "work_key": 1},
        )
        return {
            str(row.get("work_key") or "").strip()
            for row in rows
            if str(row.get("work_key") or "").strip()
        }

    async def get_latest_published_at(self, account_id: str) -> datetime | None:
        """返回账号最新作品时间，供平台列表新鲜度门禁对照。"""

        row = await self.find_one(
            {"account_id": account_id},
            projection={"_id": 0, "published_at": 1},
            sort=[("published_at", -1), ("work_key", 1)],
        )
        if row is None:
            return None
        restored = _restore_mongo_timezones(row["published_at"])
        return _require_aware(restored, "published_at")

    async def list_finished_works_by_keys(
        self,
        work_keys: Sequence[str],
        *,
        available_at: datetime,
    ) -> list[CreatorWork]:
        """按待验证记录中的作品键读取截止时点前已完成分析的作品。"""

        normalized = list(
            dict.fromkeys(str(item).strip() for item in work_keys if str(item).strip())
        )
        if not normalized:
            return []
        cutoff = _require_aware(available_at, "available_at")
        rows = await self.find_many(
            {
                "work_key": {"$in": normalized},
                "status.status": "finished",
                "first_seen_at": {"$lte": cutoff},
                "analysis.analyzed_at": {"$lte": cutoff},
            },
            projection={"_id": 0},
            sort=[("published_at", 1), ("work_key", 1)],
        )
        return [CreatorWork(**_restore_mongo_timezones(row)) for row in rows]


    async def claim_next_for_extraction(
        self,
        *,
        lease_timeout_seconds: int,
        max_attempts: int | None = None,
        now: datetime | None = None,
    ) -> CreatorWork | None:
        """在处理租约保护下按博主优先级和发布时间领取待内容提取作品。"""

        return await self._claim_next(
            stage="extraction",
            lease_timeout_seconds=lease_timeout_seconds,
            max_attempts=max_attempts,
            now=now,
        )

    async def claim_next_for_analysis(
        self,
        *,
        lease_timeout_seconds: int,
        max_attempts: int | None = None,
        now: datetime | None = None,
    ) -> CreatorWork | None:
        """在处理租约保护下按博主优先级和发布时间领取待观点分析作品。"""

        return await self._claim_next(
            stage="analysis",
            lease_timeout_seconds=lease_timeout_seconds,
            max_attempts=max_attempts,
            now=now,
        )

    async def _claim_next(
        self,
        *,
        stage: str,
        lease_timeout_seconds: int,
        max_attempts: int | None,
        now: datetime | None,
    ) -> CreatorWork | None:
        """为指定处理阶段领取一个待处理、可重试或租约过期的作品。

        领取前，达到尝试上限的过期租约会被终止为失败。MongoDB 原子更新会记录
        当前阶段、租约时间并递增尝试计数。返回 ``None`` 表示给定时点没有符合
        条件的作品。
        """

        if stage not in {"extraction", "analysis"}:
            raise ValueError("stage 必须是 extraction 或 analysis")
        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds 必须大于 0")
        attempt_limit = self.max_processing_attempts if max_attempts is None else max_attempts
        if attempt_limit <= 0:
            raise ValueError("max_attempts 必须大于 0")
        active_now = _require_aware(now or datetime.now(CN_TZ), "now")
        stale_before = active_now - timedelta(seconds=lease_timeout_seconds)
        pending = f"pending_{stage}"
        processing = "extracting" if stage == "extraction" else "analyzing"
        failed = f"{stage}_failed"

        await self._finalize_exhausted_stage(
            processing_status=processing,
            failed_status=failed,
            stale_before=stale_before,
            max_attempts=attempt_limit,
        )
        eligible_conditions: list[dict[str, Any]] = [
            {
                "$or": [
                    {"status.status": pending},
                    {
                        "status.status": failed,
                        "$or": [
                            {"next_retry_at": {"$exists": False}},
                            {"next_retry_at": None},
                            {"next_retry_at": {"$lte": active_now}},
                        ],
                    },
                    {
                        "status.status": processing,
                        "$or": self._stale_lease_filters(stale_before),
                    },
                ]
            },
            {
                "$or": [
                    {"processing_attempts": {"$exists": False}},
                    {"processing_attempts": {"$lt": attempt_limit}},
                ]
            },
        ]
        update = {
            "$set": {
                "status": CreatorWorkStatus(status=processing).model_dump(mode="python"),
                "processing_started_at": active_now,
            },
            "$inc": {"processing_attempts": 1},
            "$unset": {"next_retry_at": ""},
        }

        async def claim(
            priority_creator_ids: Sequence[str] = (),
        ) -> dict[str, Any] | None:
            """按可选博主优先过滤器原子领取发布时间最早的一条作品。"""

            conditions = list(eligible_conditions)
            if priority_creator_ids:
                conditions.append({"creator_id": {"$in": list(priority_creator_ids)}})
            return await self.collection.find_one_and_update(
                {"$and": conditions},
                update,
                sort=[("published_at", 1), ("work_key", 1)],
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
            )

        priority_creator_ids = await self._get_priority_creator_ids()
        document = await claim(priority_creator_ids) if priority_creator_ids else None
        if document is None:
            document = await claim()
        if document is None:
            return None
        return CreatorWork(**_restore_mongo_timezones(document))

    async def _get_priority_creator_ids(self) -> tuple[str, ...]:
        """返回最近一期评分 Top 5 博主 ID，查询失败时安全退化为普通旧作优先。

        排行榜读取只投影 ID、分数和样本数，并在当前 worker 进程内缓存十五分钟；
        数据库短暂不可用不会阻断媒体或 LLM 1 队列，下一次缓存过期后会自动重试。
        """

        now = monotonic()
        if now < self._priority_cache_expires_at:
            return self._priority_creator_ids
        try:
            repository = CreatorOpinionAnalysisRepository(database=self.database)
            self._priority_creator_ids = await repository.list_ranked_creator_ids(limit=5)
        except Exception as exc:
            logger.warning("load creator processing priority failed: %s", exc)
            self._priority_creator_ids = ()
        self._priority_cache_expires_at = now + self.priority_cache_seconds
        return self._priority_creator_ids

    async def _finalize_exhausted_stage(
        self,
        *,
        processing_status: str,
        failed_status: str,
        stale_before: datetime,
        max_attempts: int,
    ) -> None:
        """在耗尽所有尝试次数后，将过期处理租约标记为终止失败。

        只修改仍处于指定处理中状态、尝试次数达到或超过 ``max_attempts`` 且没有
        有效租约的记录，并移除租约标记，避免其被误认为仍在处理。
        """

        await self.update_many(
            {
                "status.status": processing_status,
                "processing_attempts": {"$gte": max_attempts},
                "$or": self._stale_lease_filters(stale_before),
            },
            {
                "$set": {
                    "status": CreatorWorkStatus(
                        status=failed_status,  # type: ignore[arg-type]
                        reason="处理租约过期且已达到最大重试次数。",
                    ).model_dump(mode="python")
                },
                "$unset": {"processing_started_at": ""},
            },
        )

    @staticmethod
    def _stale_lease_filters(stale_before: datetime) -> list[dict[str, Any]]:
        """返回用于识别租约缺失或过期状态的 MongoDB 备选条件。"""

        return [
            {"processing_started_at": {"$exists": False}},
            {"processing_started_at": None},
            {"processing_started_at": {"$lte": stale_before}},
        ]

    async def mark_extraction_success(
        self,
        work_key: str,
        extracted_text: str,
        *,
        expected_attempt: int,
        asr_text: str = "",
        ocr_text: str = "",
    ) -> UpdateResult:
        """隔离写入成功的内容提取结果，并推进到待观点分析状态。

        ``expected_attempt`` 必须与生成文本的领取尝试一致，因此旧工作进程无法
        覆盖更新的尝试。文本各组成部分会被规范化，处理尝试状态则为观点分析
        阶段重置。
        """

        normalized_text = extracted_text.strip()
        if not normalized_text:
            raise ValueError("extracted_text 不能为空")
        return await self.update_one(
            {
                "work_key": work_key,
                "status.status": "extracting",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "extracted_text": normalized_text,
                    "asr_text": asr_text.strip(),
                    "ocr_text": ocr_text.strip(),
                    "status": CreatorWorkStatus(status="pending_analysis").model_dump(
                        mode="python"
                    ),
                    "processing_attempts": 0,
                },
                "$unset": {"processing_started_at": "", "next_retry_at": ""},
            },
        )

    async def mark_extraction_failed(
        self,
        work_key: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int = 60,
    ) -> UpdateResult:
        """记录按尝试次数隔离的内容提取失败，并安排下次重试。"""

        return await self._mark_failed(
            work_key=work_key,
            processing_status="extracting",
            failed_status="extraction_failed",
            reason=reason,
            expected_attempt=expected_attempt,
            retry_delay_seconds=retry_delay_seconds,
        )

    async def mark_analysis_success(
        self,
        work_key: str,
        analysis: CreatorWorkAnalysis,
        *,
        expected_attempt: int,
    ) -> UpdateResult:
        """持久化已校验的单作品分析结果，并完成相匹配的领取尝试。

        状态与尝试次数条件会阻止租约已被替换的工作进程在新分析任务领取后提交
        过期结果。
        """

        return await self.update_one(
            {
                "work_key": work_key,
                "status.status": "analyzing",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "analysis": analysis.model_dump(mode="python"),
                    "a_share_opinions": [
                        opinion.model_dump(mode="python")
                        for opinion in analysis.opinions
                    ],
                    "is_a_share_relevant": bool(analysis.opinions),
                    "status": CreatorWorkStatus(status="finished").model_dump(mode="python"),
                },
                "$unset": {"processing_started_at": "", "next_retry_at": ""},
            },
        )

    async def mark_analysis_failed(
        self,
        work_key: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int = 60,
    ) -> UpdateResult:
        """记录按尝试次数隔离的单作品分析失败，并安排下次重试。"""

        return await self._mark_failed(
            work_key=work_key,
            processing_status="analyzing",
            failed_status="analysis_failed",
            reason=reason,
            expected_attempt=expected_attempt,
            retry_delay_seconds=retry_delay_seconds,
        )

    async def _mark_failed(
        self,
        *,
        work_key: str,
        processing_status: str,
        failed_status: str,
        reason: str,
        expected_attempt: int,
        retry_delay_seconds: int,
    ) -> UpdateResult:
        """执行内容提取或单作品分析共用的隔离失败状态迁移。

        失败原因经规范化和限长后存储，同时移除有效租约，并基于当前中国时区
        时间计算 ``next_retry_at``。
        """

        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能小于 0")
        next_retry_at = datetime.now(CN_TZ) + timedelta(seconds=retry_delay_seconds)
        return await self.update_one(
            {
                "work_key": work_key,
                "status.status": processing_status,
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "status": CreatorWorkStatus(
                        status=failed_status,  # type: ignore[arg-type]
                        reason=(reason or "处理失败。").strip()[:1000],
                    ).model_dump(mode="python"),
                    "next_retry_at": next_retry_at,
                },
                "$unset": {"processing_started_at": ""},
            },
        )

    async def list_opinions_by_published_window(
        self,
        *,
        creator_id: str,
        start_at: datetime,
        end_at: datetime,
        available_at: datetime | None = None,
    ) -> list[CreatorOpinion]:
        """列出发布时间窗口内已完成作品中的结构化观点。

        时间区间为左闭右开。提供 ``available_at`` 时，首次发现和分析完成时间
        均不得晚于该截止时间，防止后续补录结果泄漏到历史计算中。
        """

        start = _require_aware(start_at, "start_at")
        end = _require_aware(end_at, "end_at")
        if end <= start:
            raise ValueError("end_at 必须晚于 start_at")
        filters: dict[str, Any] = {
            "creator_id": creator_id,
            "status.status": "finished",
            "published_at": {"$gte": start, "$lt": end},
        }
        if available_at is not None:
            cutoff = _require_aware(available_at, "available_at")
            if cutoff < end:
                raise ValueError("available_at 不能早于 end_at")
            filters["first_seen_at"] = {"$lte": cutoff}
            filters["analysis.analyzed_at"] = {"$lte": cutoff}
        rows = await self.find_many(
            filters,
            projection={"_id": 0, "analysis.opinions": 1},
            sort=[("published_at", 1)],
        )
        return [
            CreatorOpinion(**_restore_mongo_timezones(opinion))
            for row in rows
            for opinion in ((row.get("analysis") or {}).get("opinions") or [])
        ]

    async def list_finished_works_by_published_window(
        self,
        *,
        creator_id: str,
        start_at: datetime,
        end_at: datetime,
        available_at: datetime | None = None,
        limit: int | None = None,
    ) -> list[CreatorWork]:
        """返回限定发布窗口内、满足历史时点安全要求的已完成作品。

        ``available_at`` 分别限制首次发现和分析完成时间，避免后续补录出现在更早
        的报告中。结果按最新优先排序，因此可选的正数 ``limit`` 始终选取最近的
        来源作品。
        """

        start = _require_aware(start_at, "start_at")
        end = _require_aware(end_at, "end_at")
        if end <= start:
            raise ValueError("end_at 必须晚于 start_at")
        filters: dict[str, Any] = {
            "creator_id": creator_id,
            "status.status": "finished",
            "published_at": {"$gte": start, "$lt": end},
        }
        if available_at is not None:
            cutoff = _require_aware(available_at, "available_at")
            filters["first_seen_at"] = {"$lte": cutoff}
            filters["analysis.analyzed_at"] = {"$lte": cutoff}
        rows = await self.find_many(
            filters,
            projection={"_id": 0},
            sort=[("published_at", -1), ("work_key", 1)],
            limit=limit,
        )
        return [CreatorWork(**_restore_mongo_timezones(row)) for row in rows]

    async def list_finished_works_for_morning_context(
        self,
        *,
        creator_id: str,
        available_after: datetime,
        available_at: datetime,
    ) -> list[CreatorWork]:
        """返回截止盘前时点新分析或仍有有效观点的作品。

        这项查询把“本报告周期内新完成分析”和“更早发布但预测尚未到期”合并，
        同时严格限制首次发现、发布时间和分析完成时间，避免历史报告穿越未来。
        """

        start = _require_aware(available_after, "available_after")
        cutoff = _require_aware(available_at, "available_at")
        if start >= cutoff:
            raise ValueError("available_after 必须早于 available_at")
        rows = await self.find_many(
            {
                "creator_id": creator_id,
                "status.status": "finished",
                "published_at": {"$lte": cutoff},
                "first_seen_at": {"$lte": cutoff},
                "analysis.analyzed_at": {"$lte": cutoff},
                "$or": [
                    {"analysis.analyzed_at": {"$gt": start}},
                    {"analysis.opinions.valid_until": {"$gte": cutoff}},
                ],
            },
            projection={"_id": 0},
            sort=[("published_at", -1), ("work_key", 1)],
        )
        return [CreatorWork(**_restore_mongo_timezones(row)) for row in rows]

    async def find_latest_finished_before(
        self,
        *,
        creator_id: str,
        end_at: datetime,
        available_at: datetime | None = None,
    ) -> CreatorWork | None:
        """查找更早且最新的已完成作品，用于诊断来源是否陈旧。

        作品发布时间必须早于 ``end_at``。提供 ``available_at`` 时，首次发现和
        分析完成时间也不得晚于该历史截止时间，确保诊断不会泄漏未来信息。
        """

        end = _require_aware(end_at, "end_at")
        filters: dict[str, Any] = {
            "creator_id": creator_id,
            "status.status": "finished",
            "published_at": {"$lt": end},
        }
        if available_at is not None:
            cutoff = _require_aware(available_at, "available_at")
            filters["first_seen_at"] = {"$lte": cutoff}
            filters["analysis.analyzed_at"] = {"$lte": cutoff}
        row = await self.find_one(
            filters,
            projection={"_id": 0},
            sort=[("published_at", -1), ("work_key", 1)],
        )
        return CreatorWork(**_restore_mongo_timezones(row)) if row else None

    async def count_by_published_window(
        self,
        *,
        creator_id: str,
        start_at: datetime,
        end_at: datetime,
        available_at: datetime | None = None,
    ) -> int:
        """统计已发现作品数量，不区分其处理状态。"""

        start = _require_aware(start_at, "start_at")
        end = _require_aware(end_at, "end_at")
        if end <= start:
            raise ValueError("end_at 必须晚于 start_at")
        filters: dict[str, Any] = {
            "creator_id": creator_id,
            "published_at": {"$gte": start, "$lt": end},
        }
        if available_at is not None:
            filters["first_seen_at"] = {
                "$lte": _require_aware(available_at, "available_at")
            }
        return await self.count_documents(filters)

    async def count_unfinished_by_published_window(
        self,
        *,
        creator_id: str,
        start_at: datetime,
        end_at: datetime,
        available_at: datetime | None = None,
    ) -> int:
        """统计尚未进入持久化分析完成状态的作品数量。"""

        start = _require_aware(start_at, "start_at")
        end = _require_aware(end_at, "end_at")
        if end <= start:
            raise ValueError("end_at 必须晚于 start_at")
        filters: dict[str, Any] = {
            "creator_id": creator_id,
            "published_at": {"$gte": start, "$lt": end},
        }
        if available_at is not None:
            cutoff = _require_aware(available_at, "available_at")
            filters["first_seen_at"] = {"$lte": cutoff}
            filters["$or"] = [
                {"status.status": {"$nin": ["finished", "excluded"]}},
                {"analysis.analyzed_at": {"$exists": False}},
                {"analysis.analyzed_at": None},
                {"analysis.analyzed_at": {"$gt": cutoff}},
            ]
        else:
            filters["status.status"] = {"$nin": ["finished", "excluded"]}
        return await self.count_documents(filters)

class CreatorOpinionAnalysisRepository(BaseMongoRepository):
    """持久化每位博主一条的待验证、已验证观点和累计准确率。"""

    model_class = CreatorOpinionAnalysisDisplay

    async def create_indexes(self) -> None:
        """创建累计准确率排行榜索引；博主唯一性由 ``_id`` 保证。"""

        await self.collection.create_index(
            [("accuracy_score", -1), ("creator_name", 1)],
            name="idx_accuracy_score",
        )

    @staticmethod
    def pending_from_work(work: CreatorWork) -> list[CreatorOpinionRecord]:
        """把作品中带验证日期的 A 股观点转换为待验证记录。"""

        return [
            CreatorOpinionRecord(
                opinion_id=item.opinion_id,
                event_id=item.event_id,
                work_key=work.work_key,
                platform=work.platform,
                published_at_beijing=work.published_at_beijing,
                target_type=item.target_type,
                target_name=item.target_name,
                direction=item.direction,
                opinion=item.claim,
                statement_type=item.statement_type,
                verification_date=item.verification_date,
            )
            for item in work.a_share_opinions
            if item.verification_date is not None
        ]

    async def get_creator(
        self,
        *,
        creator_id: str,
        creator_name: str,
    ) -> CreatorOpinionAnalysisDisplay:
        """返回一位博主的观点文档；首次出现时返回空模型。"""

        row = await self.collection.find_one({"_id": creator_id})
        if row is None:
            return CreatorOpinionAnalysisDisplay(creator_name=creator_name)
        return CreatorOpinionAnalysisDisplay(
            **_restore_mongo_timezones(
                {key: value for key, value in row.items() if key != "_id"}
            )
        )

    async def sync_work_opinions(self, work: CreatorWork) -> int:
        """把新完成作品的观点幂等加入该博主待验证列表。"""

        await self.collection.update_one(
            {"_id": work.creator_id},
            {
                "$set": {"creator_name": work.creator_name or work.creator_id},
                "$setOnInsert": {
                    "verified_opinions": [],
                    "accuracy_score": None,
                    "pending_opinions": [],
                },
            },
            upsert=True,
        )
        inserted = 0
        for record in self.pending_from_work(work):
            result = await self.collection.update_one(
                {
                    "_id": work.creator_id,
                    "verified_opinions.opinion_id": {"$ne": record.opinion_id},
                    "pending_opinions.opinion_id": {"$ne": record.opinion_id},
                },
                {"$push": {"pending_opinions": record.model_dump(mode="python")}},
            )
            inserted += int(getattr(result, "modified_count", 0))
        return inserted

    async def replace_creator(
        self,
        *,
        creator_id: str,
        document: CreatorOpinionAnalysisDisplay,
    ) -> UpdateResult:
        """按博主 ID 严格替换整条观点文档。"""

        return await self.collection.replace_one(
            {"_id": creator_id},
            {"_id": creator_id, **document.model_dump(mode="python")},
            upsert=True,
        )

    async def settle_opinions(
        self,
        *,
        creator_id: str,
        records: Sequence[CreatorOpinionRecord],
        accuracy_score: float | None,
    ) -> UpdateResult | None:
        """原子地把到期观点从 pending 移入 verified 并更新累计分。"""

        if not records:
            return None
        opinion_ids = [item.opinion_id for item in records]
        return await self.collection.update_one(
            {"_id": creator_id},
            {
                "$pull": {"pending_opinions": {"opinion_id": {"$in": opinion_ids}}},
                "$addToSet": {
                    "verified_opinions": {
                        "$each": [item.model_dump(mode="python") for item in records]
                    }
                },
                "$set": {"accuracy_score": accuracy_score},
            },
        )

    async def list_all(
        self,
    ) -> list[tuple[str, CreatorOpinionAnalysisDisplay]]:
        """按博主 ID 返回全部观点文档。"""

        rows = await self.find_many({}, sort=[("_id", 1)])
        return [
            (
                str(row["_id"]),
                CreatorOpinionAnalysisDisplay(
                    **_restore_mongo_timezones(
                        {key: value for key, value in row.items() if key != "_id"}
                    )
                ),
            )
            for row in rows
        ]

    async def list_ranked(
        self,
        *,
        limit: int = 20,
    ) -> list[CreatorOpinionAnalysisDisplay]:
        """按累计准确率和博主名称返回当前排名。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        rows = await self.find_many(
            {
                "accuracy_score": {"$ne": None},
            },
            projection={"_id": 0},
            sort=[
                ("accuracy_score", -1),
                ("creator_name", 1),
            ],
            limit=limit,
        )
        return [
            CreatorOpinionAnalysisDisplay(**_restore_mongo_timezones(row))
            for row in rows
        ]

    async def list_ranked_with_ids(
        self,
        *,
        limit: int = 20,
        creator_ids: Sequence[str] | None = None,
    ) -> list[tuple[str, CreatorOpinionAnalysisDisplay]]:
        """按累计准确率返回博主 ID 和对应的唯一汇总文档。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        filters: dict[str, Any] = {"accuracy_score": {"$ne": None}}
        if creator_ids is not None:
            active_ids = list(dict.fromkeys(str(item).strip() for item in creator_ids))
            if not active_ids or any(not item for item in active_ids):
                return []
            filters["_id"] = {"$in": active_ids}
        rows = await self.find_many(
            filters,
            projection={"_id": 1, "creator_name": 1, "verified_opinions": 1,
                        "accuracy_score": 1, "pending_opinions": 1},
            sort=[("accuracy_score", -1), ("creator_name", 1), ("_id", 1)],
            limit=limit,
        )
        result: list[tuple[str, CreatorOpinionAnalysisDisplay]] = []
        for row in rows:
            creator_id = str(row.pop("_id"))
            result.append(
                (
                    creator_id,
                    CreatorOpinionAnalysisDisplay(
                        **_restore_mongo_timezones(row)
                    ),
                )
            )
        return result

    async def list_ranked_creator_ids(self, *, limit: int = 5) -> tuple[str, ...]:
        """返回累计准确率最高且已有已验证观点的博主 ID。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        rows = await self.find_many(
            {
                "accuracy_score": {"$ne": None},
                "verified_opinions.0": {"$exists": True},
            },
            projection={"_id": 1},
            sort=[("accuracy_score", -1), ("creator_name", 1), ("_id", 1)],
            limit=limit,
        )
        return tuple(str(row["_id"]) for row in rows)
