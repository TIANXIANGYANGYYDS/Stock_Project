from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import find_page
from app.api.serializers import serialize_document


router = APIRouter(prefix="/api/v1/news-rankings", tags=["news-rankings"])


@router.get("")
async def list_rankings(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    biz_date: str | None = Query(default=None),
) -> dict[str, Any]:
    filters = {"biz_date": biz_date} if biz_date else {}
    return await find_page(
        db["news_ranking_snapshots"],
        filters,
        pagination,
        sort=[("generated_at", -1), ("snapshot_id", -1)],
    )


async def _latest_snapshot(db: AsyncIOMotorDatabase) -> dict[str, Any] | None:
    return await db["news_ranking_snapshots"].find_one(
        {"status": "completed"},
        sort=[("generated_at", -1), ("snapshot_id", -1)],
    )


@router.get("/latest")
async def latest_ranking(db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    row = await _latest_snapshot(db)
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到新闻排行榜快照")
    return {"data": serialize_document(row)}


@router.get("/{snapshot_id}")
async def get_ranking(
    snapshot_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["news_ranking_snapshots"].find_one({"snapshot_id": snapshot_id})
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应新闻排行榜快照")
    return {"data": serialize_document(row)}
