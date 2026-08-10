from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import aggregate_page, find_page
from app.api.serializers import serialize_document


router = APIRouter(tags=["stocks"])


@router.get("/api/v1/market/latest-trade-date")
async def get_latest_trade_date(
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    row = await db["stock_daily_detail"].find_one(
        {"adjust": "qfq"},
        projection={"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    return {"data": {"latest_trade_date": (row or {}).get("trade_date")}}


@router.get("/api/v1/stocks")
async def list_stocks(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    keyword: str | None = Query(default=None, min_length=1),
    adjust: str = Query(default="qfq"),
) -> dict[str, Any]:
    match: dict[str, Any] = {"adjust": adjust}
    if keyword:
        escaped = re.escape(keyword)
        match["$or"] = [
            {"code": {"$regex": escaped, "$options": "i"}},
            {"name": {"$regex": escaped, "$options": "i"}},
        ]
    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {"$sort": {"code": 1, "trade_date_int": -1}},
        {"$group": {"_id": "$code", "latest": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$latest"}},
        {"$sort": {"trade_date_int": -1, "code": 1}},
        {
            "$facet": {
                "items": [
                    {"$skip": pagination.skip},
                    {"$limit": pagination.page_size},
                    {
                        "$project": {
                            "_id": 0,
                            "code": 1,
                            "name": 1,
                            "latest_trade_date": "$trade_date",
                            "latest_close": "$close",
                        }
                    },
                ],
                "meta": [{"$count": "total"}],
            }
        },
    ]
    return await aggregate_page(db["stock_daily_detail"], pipeline, pagination)


@router.get("/api/v1/stocks/{code}/daily")
async def list_stock_daily(
    code: str,
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    adjust: str = Query(default="qfq"),
) -> dict[str, Any]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    filters: dict[str, Any] = {"code": code, "adjust": adjust}
    if start_date or end_date:
        filters["trade_date"] = {
            **({"$gte": start_date.isoformat()} if start_date else {}),
            **({"$lte": end_date.isoformat()} if end_date else {}),
        }
    return await find_page(
        db["stock_daily_detail"],
        filters,
        pagination,
        sort=[("trade_date_int", -1)],
    )


@router.get("/api/v1/stocks/{code}/daily/{trade_date}")
async def get_stock_daily(
    code: str,
    trade_date: date,
    db: AsyncIOMotorDatabase = Depends(get_db),
    adjust: str = Query(default="qfq"),
) -> dict[str, Any]:
    row = await db["stock_daily_detail"].find_one(
        {"code": code, "trade_date": trade_date.isoformat(), "adjust": adjust}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="没有找到对应股票日线")
    return {"data": serialize_document(row)}


@router.get("/api/v1/stock-daily/{trade_date}")
async def list_market_daily(
    trade_date: date,
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    adjust: str = Query(default="qfq"),
    sort_by: Literal["code", "close", "pct_chg", "volume", "amount", "turnover_pct"] = Query(default="code"),
    sort_order: Literal["asc", "desc"] = Query(default="asc"),
) -> dict[str, Any]:
    direction = 1 if sort_order == "asc" else -1
    return await find_page(
        db["stock_daily_detail"],
        {"trade_date": trade_date.isoformat(), "adjust": adjust},
        pagination,
        sort=[(sort_by, direction), ("code", 1)],
    )
