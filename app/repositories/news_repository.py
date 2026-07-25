from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pymongo import ReturnDocument, UpdateOne
from pymongo.results import BulkWriteResult, UpdateResult

from app.models import FetchedNews, News, NewsSectorLLMAnalysis, NewsStatus
from app.repositories.base import BaseMongoRepository


@dataclass
class NewsBatchWriteResult:
    """
    新闻批量入库的统计结果。

    这个对象只描述一次 save_rows 的写入结果，不代表数据库里总共有多少新闻。
    """

    # 本次传入 save_rows 的新闻总数，包含已存在和新插入的数据。
    total_count: int

    # 本次真正新插入 MongoDB 的新闻数量。
    inserted_count: int

    # 本次发现已经存在、因此没有重复插入的新闻数量。
    existing_count: int


class NewsRepository(BaseMongoRepository):
    """
    新闻集合的 MongoDB repository。

    这里统一封装 news_data 集合的读写细节。上层 service 不直接拼 MongoDB
    查询条件，而是调用这些方法完成入库、去重、状态流转和 LLM 结果落库。
    """

    # 绑定 Pydantic 模型。BaseMongoRepository 会根据 News.__tablename__
    # 自动推导集合名，也会用 News 做入库数据校验和规范化。
    model_class = News

    async def create_indexes(self) -> None:
        """
        创建新闻集合需要的索引。

        uk_event_id：
            保证同一条新闻只入库一次，是爬虫去重的最后防线。

        idx_source_publish_ts：
            支持按来源和发布时间倒序查询新闻。

        idx_status_publish_ts：
            支持 worker 按状态领取待处理新闻，并优先处理最新新闻。

        idx_publish_ts：
            支持榜单定时任务统计时间窗口内各处理状态，避免周期性全表扫描。
        """

        await self.collection.create_index("event_id", unique=True, name="uk_event_id")
        await self.collection.create_index(
            [("source", 1), ("publish_ts", -1)],
            name="idx_source_publish_ts",
        )
        await self.collection.create_index(
            [("status.status", 1), ("publish_ts", -1)],
            name="idx_status_publish_ts",
        )
        await self.collection.create_index(
            "publish_ts",
            name="idx_publish_ts",
        )
    def _build_document(
        self,
        row: News | FetchedNews | dict[str, Any],
    ) -> dict[str, Any]:
        """
        把外部传入的数据转换成可写入 MongoDB 的标准新闻文档。

        row 可以是：
        1. News：已经是完整入库模型；
        2. FetchedNews：crawler 抓取后的模型；
        3. dict：测试或补偿脚本传入的原始字典。

        build_document 会再经过 News 模型校验，保证默认 status 等字段被补齐。
        """

        return self.build_document(row)

    async def save_rows(
        self,
        rows: Sequence[News | FetchedNews | dict[str, Any]],
    ) -> NewsBatchWriteResult:
        """
        批量保存新闻，并返回本轮入库统计。

        内部调用 upsert_many，按 event_id 做幂等写入：
        - event_id 不存在时插入整条新闻；
        - event_id 已存在时不覆盖原文、状态和已有 LLM 分析结果。

        这个方法主要给 NewsIngestionService 使用。
        """

        if not rows:
            return NewsBatchWriteResult(
                total_count=0,
                inserted_count=0,
                existing_count=0,
            )

        rows_to_write = await self._exclude_existing_jin10_identities(rows)
        write_result = await self.upsert_many(rows_to_write)
        inserted_count = 0 if write_result is None else int(getattr(write_result, "upserted_count", 0))
        total_count = len(rows)

        return NewsBatchWriteResult(
            total_count=total_count,
            inserted_count=inserted_count,
            existing_count=max(total_count - inserted_count, 0),
        )

    async def _exclude_existing_jin10_identities(
        self,
        rows: Sequence[News | FetchedNews | dict[str, Any]],
    ) -> list[News | FetchedNews | dict[str, Any]]:
        """过滤数据库中已按旧事件 ID 入库的同一金十快讯。

        新版金十事件 ID 使用稳定快讯 ID，但历史文档可能仍是内容哈希。方法用
        ``publish_ts + title`` 查询并排除这些历史重复项，其他来源和新新闻原样保留。
        """
        identities = {
            (int(document.get("publish_ts") or 0), str(document.get("title") or "").strip())
            for row in rows
            if (document := self._row_dict(row)).get("source") == "jin10"
        }
        identities.discard((0, ""))
        if not identities:
            return list(rows)

        existing_rows = await self.find_many(
            {
                "source": "jin10",
                "publish_ts": {"$in": sorted({item[0] for item in identities})},
            },
            projection={"_id": 0, "publish_ts": 1, "title": 1},
        )
        existing_identities = {
            (int(row.get("publish_ts") or 0), str(row.get("title") or "").strip())
            for row in existing_rows
        }
        filtered_rows = []
        for row in rows:
            document = self._row_dict(row)
            identity = (
                int(document.get("publish_ts") or 0),
                str(document.get("title") or "").strip(),
            )
            if document.get("source") == "jin10" and identity in existing_identities:
                continue
            filtered_rows.append(row)
        return filtered_rows

    @staticmethod
    def _row_dict(row: News | FetchedNews | dict[str, Any]) -> dict[str, Any]:
        """把新闻模型或字典转换为普通 Python 字典，供身份比较复用。

        Pydantic 模型使用 python 模式导出以保留 datetime 等原生值；原始字典会
        浅拷贝，避免过滤流程意外修改调用方持有的数据。
        """
        if isinstance(row, (News, FetchedNews)):
            return row.model_dump(mode="python")
        return dict(row)

    async def upsert_one(
        self,
        row: News | FetchedNews | dict[str, Any],
    ) -> UpdateResult:
        """
        幂等写入单条新闻。

        只使用 $setOnInsert，表示重复抓取同一 event_id 时不会覆盖已有数据。
        这样可以避免 crawler 后续重复抓取时，把已经进入 LLM 流程的状态重置回
        crawled，也避免覆盖已经写入的 sector_llm_analysis。
        """

        document = self._build_document(row)

        return await self.update_one(
            {"event_id": document["event_id"]},
            {
                # 重复抓取时保留已有状态和后续 LLM 分析结果。
                "$setOnInsert": document,
            },
            upsert=True,
        )

    async def upsert_many(
        self,
        rows: Sequence[News | FetchedNews | dict[str, Any]],
    ) -> BulkWriteResult | None:
        """
        幂等批量写入新闻。

        ordered=False 表示批量操作里某条数据失败时，不阻塞后续数据继续写入。
        返回 None 表示本次没有任何待写入数据。
        """

        if not rows:
            return None

        operations = []
        for row in rows:
            document = self._build_document(row)
            operations.append(
                UpdateOne(
                    {"event_id": document["event_id"]},
                    {
                        # 重复抓取时保留已有状态和后续 LLM 分析结果。
                        "$setOnInsert": document,
                    },
                    upsert=True,
                )
            )

        return await self.bulk_write(operations, ordered=False)

    async def get_existing_event_ids(self, event_ids: Sequence[str]) -> set[str]:
        """
        查询一批 event_id 中哪些已经存在。

        这个方法适合 crawler 或补偿脚本在写入前做轻量预检查，返回 set 是为了
        让调用方可以 O(1) 判断某条新闻是否已经入库。
        """

        if not event_ids:
            return set()

        cursor = self.collection.find(
            {"event_id": {"$in": list(event_ids)}},
            projection={"event_id": 1, "_id": 0},
        )

        existing_event_ids: set[str] = set()
        async for doc in cursor:
            event_id = doc.get("event_id")
            if event_id:
                existing_event_ids.add(event_id)

        return existing_event_ids

    async def find_by_event_id(self, event_id: str) -> dict[str, Any] | None:
        """
        按 event_id 查询一条新闻。

        返回原始 dict，并去掉 MongoDB 的 _id 字段，方便 API、脚本或测试直接
        序列化查看。event_id 为空时直接返回 None。
        """

        if not event_id:
            return None

        return await self.find_one({"event_id": event_id}, projection={"_id": 0})

    async def list_news_for_ranking_window(
        self,
        *,
        start_ts: int,
        end_ts: int,
    ) -> list[dict[str, Any]]:
        """Read one internally consistent window for ranking input and stats."""
        return await self.find_many(
            {
                "publish_ts": {"$gte": start_ts, "$lte": end_ts},
            },
            projection={
                "_id": 0,
                "event_id": 1,
                "source": 1,
                "title": 1,
                "publish_time": 1,
                "publish_ts": 1,
                "status.status": 1,
                "sector_llm_analysis": 1,
            },
            sort=[("publish_ts", -1)],
        )

    async def claim_next_sector_judge_news(self) -> News | None:
        """
        原子领取一条待板块判断的新闻。

        多 worker 并发时，find_one_and_update 会保证同一条 crawled 新闻
        只会被一个 worker 改成 sector_judging 并返回。

        领取逻辑：
        1. 只匹配 status.status == crawled 的新闻；
        2. 按 publish_ts 倒序，优先处理最新新闻；
        3. 匹配到后立刻把状态改成 sector_judging；
        4. 返回更新后的 News 模型。

        如果没有待处理新闻，返回 None，worker 会进入空闲 sleep。
        """

        doc = await self.collection.find_one_and_update(
            {"status.status": "crawled"},
            {
                "$set": {
                    "status": NewsStatus(
                        status="sector_judging",
                        reason="板块判断 worker 已领取，正在调用 LLM。",
                    ).model_dump(mode="python"),
                },
            },
            sort=[("publish_ts", -1)],
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

        if doc is None:
            return None

        return News(**doc)

    async def mark_sector_judge_success(
        self,
        event_id: str,
        analysis: Sequence[NewsSectorLLMAnalysis],
    ) -> UpdateResult:
        """
        写入板块判断结果，并把状态推进到 sector_judged。

        filter 里额外要求 status.status == sector_judging，是为了保证只有被
        worker 成功领取中的新闻才会被写入结果。如果状态已被其他流程改变，这次
        更新不会误覆盖。
        """

        return await self.update_one(
            {
                "event_id": event_id,
                "status.status": "sector_judging",
            },
            {
                "$set": {
                    "sector_llm_analysis": [
                        item.model_dump(mode="python")
                        for item in analysis
                    ],
                    "status": NewsStatus(status="sector_judged").model_dump(mode="python"),
                },
            },
        )

    async def mark_sector_judge_failed(
        self,
        event_id: str,
        reason: str,
    ) -> UpdateResult:
        """
        标记板块判断失败，并记录失败原因。

        失败原因最多保留 1000 个字符，避免异常堆栈或接口返回体过长导致单条
        文档膨胀。失败后状态进入 sector_judge_failed，后续可以单独做重试或人工
        排查。
        """

        error_reason = (reason or "板块判断分析失败。").strip()

        return await self.update_one(
            {
                "event_id": event_id,
                "status.status": "sector_judging",
            },
            {
                "$set": {
                    "status": NewsStatus(
                        status="sector_judge_failed",
                        reason=error_reason[:1000],
                    ).model_dump(mode="python"),
                },
            },
        )

    async def claim_next_sector_detail_news(self) -> News | None:
        """
        原子领取一条待板块详情分析的新闻。

        详情分析是第二阶段，只处理第一阶段已经完成的新闻：
        1. status.status 必须是 sector_judged；
        2. 领取后立即改成 sector_detail_analyzing；
        3. 返回更新后的 News 模型。

        多 worker 并发时，同一条 sector_judged 新闻只会被一个 worker 领取。
        """

        doc = await self.collection.find_one_and_update(
            {"status.status": "sector_judged"},
            {
                "$set": {
                    "status": NewsStatus(
                        status="sector_detail_analyzing",
                        reason="板块详情 worker 已领取，正在调用 LLM。",
                    ).model_dump(mode="python"),
                },
            },
            sort=[("publish_ts", -1)],
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

        if doc is None:
            return None

        return News(**doc)

    async def mark_sector_detail_success(
        self,
        event_id: str,
        analysis: Sequence[NewsSectorLLMAnalysis],
    ) -> UpdateResult:
        """
        写入板块详情分析结果，并把新闻状态推进到 finished。

        analysis 会覆盖 sector_llm_analysis：第一阶段只有 sector_name，
        第二阶段会把每个 sector_name 对应的 sector_llm_analysis 补齐。
        """

        return await self.update_one(
            {
                "event_id": event_id,
                "status.status": "sector_detail_analyzing",
            },
            {
                "$set": {
                    "sector_llm_analysis": [
                        item.model_dump(mode="python")
                        for item in analysis
                    ],
                    "status": NewsStatus(status="finished").model_dump(mode="python"),
                },
            },
        )

    async def mark_sector_detail_failed(
        self,
        event_id: str,
        reason: str,
    ) -> UpdateResult:
        """
        标记板块详情分析失败，并记录失败原因。

        失败状态与第一阶段失败状态分开，方便后续只重试详情分析，不重复做板块判断。
        """

        error_reason = (reason or "板块详情分析失败。").strip()

        return await self.update_one(
            {
                "event_id": event_id,
                "status.status": "sector_detail_analyzing",
            },
            {
                "$set": {
                    "status": NewsStatus(
                        status="sector_detail_failed",
                        reason=error_reason[:1000],
                    ).model_dump(mode="python"),
                },
            },
        )
