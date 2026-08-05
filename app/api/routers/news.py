from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import find_page
from app.api.serializers import serialize_document


router = APIRouter(prefix="/api/v1/news", tags=["news"])


def _append_filter(filters: dict[str, Any], condition: dict[str, Any]) -> None:
    filters.setdefault("$and", []).append(condition)


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
                ]
            },
        )

    return await find_page(
        db["news_data"],
        filters,
        pagination,
        sort=[("publish_ts", -1), ("event_id", 1)],
    )


@router.get("/{event_id}")
async def get_news(
    event_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["news_data"].find_one({"event_id": event_id})
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应新闻")
    return {"data": serialize_document(row)}
