from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import aggregate_page, find_page
from app.api.serializers import serialize_document


router = APIRouter(prefix="/api/v1/news", tags=["news"])


def _append_filter(filters: dict[str, Any], condition: dict[str, Any]) -> None:
    filters.setdefault("$and", []).append(condition)


EMPTY_SECTORS = ["", "不涉及板块", "不涉及版块", "暂无", "无", "未涉及"]


def _strongest_score() -> dict[str, Any]:
    # 与前端一致：取有实际板块的评分，绝对值相同时保留先出现的板块。
    candidate = {"$convert": {
        "input": "$$this.sector_llm_analysis.score", "to": "double",
        "onError": None, "onNull": None,
    }}
    sector = {"$ifNull": ["$$this.sector_name", ""]}
    stronger = {"$or": [
        {"$eq": ["$$value", None]},
        {"$gt": [{"$abs": "$$score"}, {"$abs": "$$value"}]},
    ]}
    use_candidate = {"$and": [
        {"$not": [{"$in": [sector, EMPTY_SECTORS]}]},
        {"$ne": ["$$score", None]}, stronger,
    ]}
    return {"$reduce": {
        "input": {"$ifNull": ["$sector_llm_analysis", []]}, "initialValue": None,
        "in": {"$let": {"vars": {"score": candidate},
                        "in": {"$cond": [use_candidate, "$$score", "$$value"]}}},
    }}


@router.get("")
async def list_news(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    start_ts: int | None = Query(default=None, ge=0),
    end_ts: int | None = Query(default=None, ge=0),
    sector_name: str | None = Query(default=None),
    company: str | None = Query(default=None),
    keyword: str | None = Query(default=None, min_length=1),
    view: Literal["raw", "feed", "updates"] = "raw",
    a_share_only: bool = False,
    sentiment: Literal["positive", "neutral", "negative"] | None = None,
    sort_by: Literal["time", "score"] = "time",
    sort_order: Literal["desc", "asc"] = "desc",
    snapshot: int | None = Query(default=None, ge=0, le=4294967295),
) -> dict[str, Any]:
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise HTTPException(status_code=422, detail="start_ts 不能晚于 end_ts")

    filters: dict[str, Any] = {}
    if source:
        filters["source"] = source
    if status:
        filters["status.status"] = status
    if start_ts is not None or end_ts is not None:
        filters["publish_ts"] = {
            **({"$gte": start_ts} if start_ts is not None else {}),
            **({"$lte": end_ts} if end_ts is not None else {}),
        }
    if sector_name:
        _append_filter(
            filters,
            {"sector_llm_analysis": {"$elemMatch": {"sector_name": sector_name}}},
        )
    if a_share_only:
        _append_filter(filters, {"sector_llm_analysis": {"$elemMatch": {
            "sector_name": {"$type": "string", "$nin": EMPTY_SECTORS},
        }}})
    if company:
        _append_filter(
            filters,
            {
                "sector_llm_analysis": {
                    "$elemMatch": {"sector_llm_analysis.companies": company}
                }
            },
        )
    if keyword:
        escaped = re.escape(keyword)
        _append_filter(
            filters,
            {
                "$or": [
                    {"title": {"$regex": escaped, "$options": "i"}},
                    {"content": {"$regex": escaped, "$options": "i"}},
                    {"sector_llm_analysis.sector_name": {"$regex": escaped, "$options": "i"}},
                    {"sector_llm_analysis.sector_llm_analysis.companies": {"$regex": escaped, "$options": "i"}},
                ]
            },
        )

    now = int(time.time())
    cutoff = snapshot if snapshot is not None else now
    if cutoff > now or (view == "updates" and snapshot is None):
        raise HTTPException(status_code=422, detail="无效的资讯快照时间")
    if view in ("feed", "updates"):
        # 以入库时间分隔，迟到或同一发布时间的快讯也能提醒，翻页时不会被新稿挤动。
        boundary = ObjectId.from_datetime(datetime.fromtimestamp(cutoff, timezone.utc))
        filters["_id"] = {"$lt": boundary} if view == "feed" else {
            "$gte": boundary, "$lt": ObjectId.from_datetime(datetime.fromtimestamp(now, timezone.utc)),
        }
    collection = db["news_data"]
    direction = -1 if sort_order == "desc" else 1
    sort = [("publish_ts", direction), ("event_id", 1)]
    if sentiment or (sort_by == "score" and view != "updates"):
        pipeline = [{"$match": filters}, {"$set": {"_score": _strongest_score()}}]
        if sentiment:
            bounds = {"positive": {"$gt": 20}, "negative": {"$lt": -20},
                      "neutral": {"$gte": -20, "$lte": 20}}
            pipeline.append({"$match": {"_score": {"$type": "number", **bounds[sentiment]}}})
        if view == "updates":
            counts = await collection.aggregate([*pipeline, {"$count": "total"}]).to_list(1)
            return {"total": counts[0]["total"] if counts else 0}
        if sort_by == "score":
            pipeline.append({"$set": {"_scored": {"$ne": ["$_score", None]}}})
            sort = [("_scored", -1), ("_score", direction), ("publish_ts", -1), ("event_id", 1)]
        pipeline.append({"$facet": {"meta": [{"$count": "total"}], "items": [
            {"$sort": dict(sort)}, {"$skip": pagination.skip}, {"$limit": pagination.page_size},
            {"$unset": ["_score", "_scored"]},
        ]}})
        result = await aggregate_page(collection, pipeline, pagination)
    elif view == "updates":
        return {"total": await collection.count_documents(filters)}
    else:
        result = await find_page(collection, filters, pagination, sort=sort)
    if view == "feed":
        result["snapshot"] = cutoff
    return result


@router.get("/{event_id}")
async def get_news(
    event_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["news_data"].find_one({"event_id": event_id})
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应新闻")
    return {"data": serialize_document(row)}
