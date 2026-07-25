from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument, UpdateOne
from pymongo.results import BulkWriteResult, UpdateResult

from app.models.douyin_creator_work import (
    CN_TZ,
    DouyinCreatorWork,
    DouyinTranscript,
    DouyinWorkAnalysis,
    DouyinWorkStatus,
    FetchedDouyinWork,
    format_cn_datetime,
)
from app.repositories.base import BaseMongoRepository


@dataclass(frozen=True)
class DouyinWorkBatchWriteResult:
    """记录抖音作品批量写入时新增和已存在的数量。"""

    # 本次 upsert 实际插入的新作品数量。
    inserted_count: int
    # 已存在于 MongoDB、因此没有新建文档的作品数量。
    existing_count: int


class DouyinCreatorWorkRepository(BaseMongoRepository):
    """
    抖音作品、转写结果和博主观点分析结果的 MongoDB 仓储。

    作品的 `publish_ts` 表示内容发布时间，`first_seen_at` 表示系统首次发现
    作品，`analysis.analyzed_at` 表示 LLM 分析完成时间。盘前查询必须分别使用
    这三类时间：例如 7 月 24 日 09:00 的报告只取 7 月 23 日发布、且在 7 月
    24 日 09:00 前发现并分析完成的作品。
    """

    # BaseMongoRepository 使用的 Pydantic 文档类型。
    model_class = DouyinCreatorWork

    async def create_indexes(self) -> None:
        """创建作品唯一键、发布时间和处理租约相关索引，保证查询与抢占高效。"""
        await self.collection.create_index(
            "work_id",
            unique=True,
            name="uk_work_id",
        )
        await self.collection.create_index(
            [("status.status", 1), ("publish_ts", -1)],
            name="idx_status_publish_ts",
        )
        await self.collection.create_index(
            [("creator_sec_uid", 1), ("publish_ts", -1)],
            name="idx_creator_publish_ts",
        )
        await self.collection.create_index(
            [("status.status", 1), ("processing_started_at", 1)],
            name="idx_status_processing_lease",
        )

    def _build_document(
        self,
        row: DouyinCreatorWork | FetchedDouyinWork | dict[str, Any],
    ) -> dict[str, Any]:
        """
        将抓取结果、领域模型或字典统一转换成 MongoDB 文档。

        `FetchedDouyinWork` 先转换为完整的 `DouyinCreatorWork`，再交由基类
        处理时间字段和 BSON 序列化，确保不同入口写入结构一致。
        """
        if isinstance(row, FetchedDouyinWork) and not isinstance(
            row, DouyinCreatorWork
        ):
            row = DouyinCreatorWork(**row.model_dump(mode="python"))
        return self.build_document(row)

    async def save_rows(
        self,
        rows: Sequence[DouyinCreatorWork | FetchedDouyinWork | dict[str, Any]],
    ) -> DouyinWorkBatchWriteResult:
        """
        批量保存抓取到的作品，并返回新增与已存在数量。

        空输入直接返回零计数；非空输入使用作品 `work_id` 做幂等 upsert，
        不会覆盖已经保存的转写、分析或处理状态。
        """
        if not rows:
            return DouyinWorkBatchWriteResult(0, 0)

        write_result = await self.upsert_many(rows)
        inserted_count = (
            0
            if write_result is None
            else int(getattr(write_result, "upserted_count", 0))
        )
        total_count = len(rows)
        return DouyinWorkBatchWriteResult(
            inserted_count=inserted_count,
            existing_count=max(total_count - inserted_count, 0),
        )

    async def upsert_many(
        self,
        rows: Sequence[DouyinCreatorWork | FetchedDouyinWork | dict[str, Any]],
    ) -> BulkWriteResult | None:
        """
        以 `work_id` 为唯一键批量插入作品，已有文档只保留原内容。

        使用 `$setOnInsert` 是为了让周期性抓取不会覆盖 worker 正在更新的
        转写、分析、租约和失败重试字段。
        """
        operations = []
        for row in rows:
            document = self._build_document(row)
            operations.append(
                UpdateOne(
                    {"work_id": document["work_id"]},
                    {"$setOnInsert": document},
                    upsert=True,
                )
            )
        return await self.bulk_write(operations, ordered=False)

    async def get_existing_work_ids(self, work_ids: Sequence[str]) -> set[str]:
        """
        查询一组作品中已存在的 `work_id`。

        输入会先去空白、去重；空输入不访问数据库，返回空集合。
        """
        normalized_ids = list(
            dict.fromkeys(str(item).strip() for item in work_ids if str(item).strip())
        )
        if not normalized_ids:
            return set()

        rows = await self.find_many(
            {"work_id": {"$in": normalized_ids}},
            projection={"_id": 0, "work_id": 1},
        )
        return {
            str(row.get("work_id") or "").strip()
            for row in rows
            if str(row.get("work_id") or "").strip()
        }

    async def claim_next_for_processing(
        self,
        *,
        lease_timeout_seconds: int,
        now: datetime | None = None,
    ) -> DouyinCreatorWork | None:
        """
        原子领取一个待处理作品，并通过 attempt 与租约隔离迟到写入。

        待处理、可重试失败或租约过期的作品会按发布时间倒序抢占；同一作品
        的 `processing_attempts` 限制为三次。`now` 仅用于测试或重放，必须带
        时区。该方法与盘前日期筛选无关，只负责 worker 的可靠领取。
        """
        if lease_timeout_seconds <= 0:
            raise ValueError("lease_timeout_seconds 必须大于 0")
        active_now = now or datetime.now(CN_TZ)
        if active_now.tzinfo is None:
            raise ValueError("now 必须包含时区")
        active_now = active_now.astimezone(CN_TZ)
        stale_before = active_now - timedelta(seconds=lease_timeout_seconds)

        await self._finalize_exhausted_stale_processing(stale_before=stale_before)
        document = await self.collection.find_one_and_update(
            {
                "$and": [
                    {
                        "$or": [
                            {
                                "status.status": "pending_transcription",
                            },
                            {
                                "status.status": {
                                    "$in": ["transcription_failed", "analysis_failed"]
                                },
                                "$or": [
                                    {"next_retry_at": {"$exists": False}},
                                    {"next_retry_at": None},
                                    {"next_retry_at": {"$lte": active_now}},
                                ],
                            },
                            {
                                "status.status": {"$in": ["transcribing", "analyzing"]},
                                "$or": self._stale_lease_filters(stale_before),
                            },
                        ]
                    },
                    {
                        "$or": [
                            {"processing_attempts": {"$exists": False}},
                            {"processing_attempts": {"$lt": 3}},
                        ]
                    },
                ],
            },
            {
                "$set": {
                    "status": DouyinWorkStatus(status="transcribing").model_dump(
                        mode="python"
                    ),
                    "processing_started_at": active_now,
                    "processing_started_at_cn": format_cn_datetime(active_now),
                },
                "$inc": {"processing_attempts": 1},
                "$unset": {"next_retry_at": "", "next_retry_at_cn": ""},
            },
            sort=[("publish_ts", -1)],
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        return (
            DouyinCreatorWork(**self._restore_mongo_timezones(document))
            if document is not None
            else None
        )

    async def _finalize_exhausted_stale_processing(
        self,
        *,
        stale_before: datetime,
    ) -> None:
        """
        将已达到最大重试次数且租约过期的任务标记为最终失败。

        转写和内容分析分别写入对应失败状态，清理处理租约，避免后续 worker
        无限重复领取同一任务。
        """
        transitions = (
            (
                "transcribing",
                DouyinWorkStatus(
                    status="transcription_failed",
                    reason="转写任务中断且已达到最大重试次数。",
                ),
            ),
            (
                "analyzing",
                DouyinWorkStatus(
                    status="analysis_failed",
                    reason="内容分析任务中断且已达到最大重试次数。",
                ),
            ),
        )
        for source_status, target_status in transitions:
            await self.update_many(
                {
                    "status.status": source_status,
                    "processing_attempts": {"$gte": 3},
                    "$or": self._stale_lease_filters(stale_before),
                },
                {
                    "$set": {"status": target_status.model_dump(mode="python")},
                    "$unset": {
                        "processing_started_at": "",
                        "processing_started_at_cn": "",
                    },
                },
            )

    @staticmethod
    def _stale_lease_filters(stale_before: datetime) -> list[dict[str, Any]]:
        """
        构造“没有租约或租约早于阈值”的 MongoDB 查询条件。

        该条件供领取和最终失败回收共用，保证两条路径对过期租约的定义一致。
        """
        return [
            {"processing_started_at": {"$exists": False}},
            {"processing_started_at": None},
            {"processing_started_at": {"$lte": stale_before}},
        ]

    async def mark_transcription_success(
        self,
        work_id: str,
        transcript: DouyinTranscript,
        *,
        expected_attempt: int,
    ) -> UpdateResult:
        """
        在 attempt 匹配时保存转写结果，并把作品推进到分析中状态。

        查询同时校验 `status=transcribing` 和 `processing_attempts`，因此旧 worker
        即使迟到也不能覆盖新一轮处理；返回的修改数由调用方用于检测租约丢失。
        """
        processing_started_at = datetime.now(CN_TZ)
        return await self.update_one(
            {
                "work_id": work_id,
                "status.status": "transcribing",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "transcript": transcript.model_dump(mode="python"),
                    "status": DouyinWorkStatus(status="analyzing").model_dump(
                        mode="python"
                    ),
                    "processing_started_at": processing_started_at,
                    "processing_started_at_cn": format_cn_datetime(
                        processing_started_at
                    ),
                }
            },
        )

    async def mark_transcription_failed(
        self,
        work_id: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int = 60,
    ) -> UpdateResult:
        """
        记录转写失败原因并安排下一次重试。

        失败原因会截断到 1000 字符；清理处理租约后由 `next_retry_at` 控制再次
        领取，`expected_attempt` 继续防止旧 worker 改写新 attempt。
        """
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds 必须大于 0")
        error_reason = (reason or "作品语音转写失败。").strip()[:1000]
        next_retry_at = datetime.now(CN_TZ) + timedelta(
            seconds=retry_delay_seconds
        )
        return await self.update_one(
            {
                "work_id": work_id,
                "status.status": "transcribing",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "status": DouyinWorkStatus(
                        status="transcription_failed",
                        reason=error_reason,
                    ).model_dump(mode="python"),
                    "next_retry_at": next_retry_at,
                    "next_retry_at_cn": format_cn_datetime(next_retry_at),
                },
                "$unset": {
                    "processing_started_at": "",
                    "processing_started_at_cn": "",
                },
            },
        )

    async def mark_analysis_success(
        self,
        work_id: str,
        analysis: DouyinWorkAnalysis,
        *,
        expected_attempt: int,
    ) -> UpdateResult:
        """
        在 attempt 匹配时保存结构化博主分析，并将作品标记为已完成。

        清除处理租约后，盘前服务才能把该作品视为可用来源；其 `analyzed_at`
        仍会在盘前查询中与 09:00 截止时间单独比较。
        """
        return await self.update_one(
            {
                "work_id": work_id,
                "status.status": "analyzing",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "analysis": analysis.model_dump(mode="python"),
                    "status": DouyinWorkStatus(status="finished").model_dump(
                        mode="python"
                    ),
                },
                "$unset": {
                    "processing_started_at": "",
                    "processing_started_at_cn": "",
                },
            },
        )

    async def mark_analysis_failed(
        self,
        work_id: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int = 60,
    ) -> UpdateResult:
        """
        记录 LLM 分析失败并安排延迟重试，同时清除当前处理租约。

        只允许仍处于 `analyzing` 且 attempt 未变化的任务写入，防止旧 worker
        在新 worker 接管后覆盖状态。
        """
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds 必须大于 0")
        error_reason = (reason or "作品内容分析失败。").strip()[:1000]
        next_retry_at = datetime.now(CN_TZ) + timedelta(
            seconds=retry_delay_seconds
        )
        return await self.update_one(
            {
                "work_id": work_id,
                "status.status": "analyzing",
                "processing_attempts": expected_attempt,
            },
            {
                "$set": {
                    "status": DouyinWorkStatus(
                        status="analysis_failed",
                        reason=error_reason,
                    ).model_dump(mode="python"),
                    "next_retry_at": next_retry_at,
                    "next_retry_at_cn": format_cn_datetime(next_retry_at),
                },
                "$unset": {
                    "processing_started_at": "",
                    "processing_started_at_cn": "",
                },
            },
        )

    async def list_finished_for_morning(
        self,
        *,
        creator_sec_uid: str,
        start_ts: int,
        end_ts: int,
        available_at_ts: int | None = None,
        limit: int,
    ) -> list[DouyinCreatorWork]:
        """
        查询可用于指定盘前报告的已完成作品。

        `start_ts`/`end_ts` 只限制发布时间，代表来源自然日；例如 7 月 24 日
        盘前应传入 7 月 23 日 00:00:00 到 23:59:59。`available_at_ts` 独立
        限制 `first_seen_at` 和 `analysis.analyzed_at`，例如固定为 7 月 24 日
        09:00，从而排除盘后才补录或分析完成的 7 月 23 日视频。
        """
        if end_ts < start_ts:
            raise ValueError("end_ts 不能小于 start_ts")
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        availability_ts = end_ts if available_at_ts is None else available_at_ts
        if availability_ts < end_ts:
            raise ValueError("available_at_ts 不能早于 end_ts")

        rows = await self.find_many(
            {
                "creator_sec_uid": creator_sec_uid,
                "status.status": "finished",
                "publish_ts": {"$gte": start_ts, "$lte": end_ts},
                "first_seen_at": {
                    "$lte": datetime.fromtimestamp(availability_ts, tz=CN_TZ)
                },
                "analysis.analyzed_at": {
                    "$lte": datetime.fromtimestamp(availability_ts, tz=CN_TZ)
                },
            },
            projection={"_id": 0},
            sort=[("publish_ts", -1)],
            limit=limit,
        )
        return [DouyinCreatorWork(**self._restore_mongo_timezones(row)) for row in rows]

    async def find_latest_finished_before(
        self,
        *,
        creator_sec_uid: str,
        end_ts: int,
        available_at_ts: int | None = None,
    ) -> DouyinCreatorWork | None:
        """
        查找指定发布时间上限且在可用截止时点前完成处理的最新作品。

        这是来源日没有合格作品时的诊断回退查询；调用方会将不在指定自然日
        的结果标记为 stale，而不会把它伪装成当日盘前来源。
        """
        availability_ts = end_ts if available_at_ts is None else available_at_ts
        if availability_ts < end_ts:
            raise ValueError("available_at_ts 不能早于 end_ts")
        row = await self.find_one(
            {
                "creator_sec_uid": creator_sec_uid,
                "status.status": "finished",
                "publish_ts": {"$lte": end_ts},
                "first_seen_at": {
                    "$lte": datetime.fromtimestamp(availability_ts, tz=CN_TZ)
                },
                "analysis.analyzed_at": {
                    "$lte": datetime.fromtimestamp(availability_ts, tz=CN_TZ)
                },
            },
            projection={"_id": 0},
            sort=[("publish_ts", -1)],
        )
        return (
            DouyinCreatorWork(**self._restore_mongo_timezones(row))
            if row is not None
            else None
        )

    @classmethod
    def _restore_mongo_timezones(cls, value: Any) -> Any:
        """
        递归恢复 MongoDB 返回值的中国时区信息。

        MongoDB 通常返回无时区的 UTC datetime；这里递归处理嵌套字典和列表，
        让领域模型中的发布时间、首次发现时间和分析完成时间都能安全参与
        7 月 24 日 09:00 这类带时区的盘前截止比较。
        """
        if isinstance(value, datetime):
            utc_value = (
                value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            )
            return utc_value.astimezone(CN_TZ)
        if isinstance(value, dict):
            return {
                key: cls._restore_mongo_timezones(item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._restore_mongo_timezones(item) for item in value]
        return value
