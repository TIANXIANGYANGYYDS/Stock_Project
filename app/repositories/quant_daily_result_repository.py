from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from app.quant.runtime.daily_flow import (
    DAILY_RESULTS_COLLECTION,
    DailyFlow,
    daily_flow_document,
)
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID


class QuantDailyResultRepository:
    """保存并读取前端所需的每日量化流程快照。"""

    def __init__(self, database: AsyncIOMotorDatabase, *, collection_name: str = DAILY_RESULTS_COLLECTION) -> None:
        self.collection = database[collection_name]

    async def create_indexes(self) -> None:
        await self.collection.update_many(
            {"strategy_id": {"$exists": False}},
            {"$set": {"strategy_id": STRATEGY_ID}},
        )
        existing_indexes = await self.collection.index_information()
        if "uniq_quant_daily_result_trade_date" in existing_indexes:
            await self.collection.drop_index(
                "uniq_quant_daily_result_trade_date"
            )
        await self.collection.create_index(
            [("strategy_id", ASCENDING), ("trade_date", ASCENDING)],
            unique=True,
            name="uniq_quant_daily_result_strategy_date",
        )
        await self.collection.create_index(
            [("updated_at", DESCENDING)],
            name="idx_quant_daily_result_updated_at",
        )

    async def save(self, flow: DailyFlow) -> dict[str, object]:
        """按交易日覆盖快照，使一次查询始终得到同一时点的完整状态。"""

        document = daily_flow_document(flow)
        await self.save_document(document)
        return document

    async def save_document(
        self, document: dict[str, Any]
    ) -> dict[str, Any]:
        """保存包含盘中运行字段的完整每日快照。"""

        trade_date = str(document.get("trade_date") or "")
        if not trade_date:
            raise ValueError("量化结果必须包含trade_date")
        nested_strategy_id = str(document.get("strategy", {}).get("id") or "")
        strategy_id = str(document.get("strategy_id") or nested_strategy_id)
        if not strategy_id:
            raise ValueError("量化结果必须包含strategy_id")
        if nested_strategy_id and nested_strategy_id != strategy_id:
            raise ValueError("strategy_id与strategy.id不一致")
        document["strategy_id"] = strategy_id
        await self.collection.replace_one(
            {"strategy_id": strategy_id, "trade_date": trade_date},
            document,
            upsert=True,
        )
        return document

    async def get(
        self,
        trade_date: str,
        projection: dict[str, Any] | None = None,
        *,
        strategy_id: str = STRATEGY_ID,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {"strategy_id": strategy_id, "trade_date": trade_date},
            projection,
        )

    async def latest(
        self,
        projection: dict[str, Any] | None = None,
        *,
        strategy_id: str = STRATEGY_ID,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {"strategy_id": strategy_id},
            projection,
            sort=[("trade_date", DESCENDING)],
        )

    async def latest_live(
        self,
        projection: dict[str, Any] | None = None,
        *,
        strategy_id: str = STRATEGY_ID,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "strategy_id": strategy_id,
                "runtime": {"$exists": True},
            },
            projection,
            sort=[("trade_date", DESCENDING)],
        )

    async def latest_before(
        self,
        trade_date: str,
        *,
        strategy_id: str = STRATEGY_ID,
    ) -> dict[str, Any] | None:
        return await self.collection.find_one(
            {
                "strategy_id": strategy_id,
                "trade_date": {"$lt": trade_date},
            },
            sort=[("trade_date", DESCENDING)],
        )

    async def page(
        self, *, strategy_id: str, start_date: str | None, end_date: str | None,
        skip: int, limit: int, projection: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """按策略和交易日期读取收益曲线；在数据库侧分页。"""
        query: dict[str, Any] = {"strategy_id": strategy_id}
        dates = {}
        if start_date is not None:
            dates["$gte"] = start_date
        if end_date is not None:
            dates["$lte"] = end_date
        if dates:
            query["trade_date"] = dates
        total = await self.collection.count_documents(query)
        rows = await self.collection.find(query, projection).sort(
            [("trade_date", ASCENDING)]
        ).skip(skip).limit(limit).to_list(length=limit)
        return rows, total

    async def execution_history_page(
        self, *, strategy_id: str, start_date: str, end_date: str,
        code: str | None, action: str | None, skip: int, limit: int,
    ) -> dict[str, Any]:
        """单次聚合返回区间源版本、成交总数和一页成交，不读取整份恢复账本。"""
        conditions = [{"$eq": ["$$execution.status", "filled"]}]
        for field, value in (("code", code), ("action", action)):
            if value is not None:
                conditions.append({"$eq": [f"$$execution.{field}", value]})
        metadata = {
            "_id": 0, "schema_version": 1, "strategy_id": 1, "trade_date": 1,
            "status": 1, "updated_at": 1, "recording": 1,
            "strategy.name": 1, "strategy.version": 1,
            "runtime.version": 1, "runtime.evaluated_at": 1,
            "runtime.last_valuation_at": 1, "runtime.data_status": 1,
            "runtime.last_error_at": 1,
        }
        pipeline = [
            {"$match": {"strategy_id": strategy_id,
                        "trade_date": {"$gte": start_date, "$lte": end_date}}},
            {"$sort": {"trade_date": 1}},
            {"$project": {**metadata, "executions": {"$filter": {
                "input": {"$ifNull": ["$intraday_trading.items", []]},
                "as": "execution", "cond": {"$and": conditions},
            }}}},
            {"$facet": {
                # 无成交日期也进入版本计算；重算后新增/删除成交可使旧分页失效。
                "sources": [{"$project": metadata}],
                "items": [
                    {"$unwind": "$executions"},
                    {"$sort": {"executions.execution_at": -1, "executions.code": 1,
                               "trade_date": -1, "executions.event_id": 1}},
                    {"$skip": skip}, {"$limit": limit},
                    {"$project": {"_id": 0, "trade_date": 1, "execution": "$executions"}},
                ],
                "counts": [{"$unwind": "$executions"}, {"$count": "total"}],
            }},
        ]
        batches = await self.collection.aggregate(pipeline).to_list(length=1)
        result = batches[0] if batches else {"sources": [], "items": [], "counts": []}
        counts = result.pop("counts")
        result["total"] = counts[0]["total"] if counts else 0
        return result

    async def record_runtime_error(
        self,
        *,
        trade_date: str,
        evaluated_at: str,
        error: str,
        strategy_id: str = STRATEGY_ID,
    ) -> None:
        """保留上一份快照，同时把本轮失败暴露给健康检查和前端。"""

        await self.collection.update_one(
            {"strategy_id": strategy_id, "trade_date": trade_date},
            {
                "$set": {
                    "runtime.data_status": "error",
                    "runtime.last_error": error[:300],
                    "runtime.last_error_at": evaluated_at,
                    "runtime.mode": "shadow",
                    "strategy_id": strategy_id,
                },
                "$setOnInsert": {
                    "trade_date": trade_date,
                    "schema_version": "2.0",
                    "status": "error",
                    "strategy": {"id": strategy_id},
                },
            },
            upsert=True,
        )
