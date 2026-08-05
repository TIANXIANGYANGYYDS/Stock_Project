from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import find_page
from app.api.serializers import serialize_document


router = APIRouter(prefix="/api/v1/morning-analyses", tags=["morning-analysis"])

LIST_PROJECTION = {
    "_id": 0,
    "analysis_date": 1,
    "trade_date": 1,
    "prev_trade_date": 1,
    "status": 1,
    "data_quality": 1,
    "prompt_version": 1,
    "analysis_model": 1,
    "thinking_enabled": 1,
    "news_window": 1,
    "ranking_snapshot_meta": 1,
    "analysis": 1,
    "created_at": 1,
    "updated_at": 1,
}


@router.get("")
async def list_morning_analyses(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    data_quality: str | None = Query(default=None),
) -> dict[str, Any]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    filters: dict[str, Any] = {}
    if start_date or end_date:
        filters["analysis_date"] = {
            **({"$gte": start_date.isoformat()} if start_date else {}),
            **({"$lte": end_date.isoformat()} if end_date else {}),
        }
    if data_quality:
        filters["data_quality"] = data_quality
    return await find_page(
        db["daily_market_analysis"],
        filters,
        pagination,
        projection=LIST_PROJECTION,
        sort=[("analysis_date", -1)],
    )


@router.get("/latest")
async def latest_morning_analysis(
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["daily_market_analysis"].find_one(
        {}, sort=[("analysis_date", -1)]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到盘前分析")
    return {"data": serialize_document(row)}


@router.get("/{analysis_date}")
async def get_morning_analysis(
    analysis_date: date,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["daily_market_analysis"].find_one(
        {"analysis_date": analysis_date.isoformat()}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应日期的盘前分析")
    return {"data": serialize_document(row)}
