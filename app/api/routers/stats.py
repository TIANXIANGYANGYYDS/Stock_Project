from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import get_db


router = APIRouter(prefix="/api/v1", tags=["stats"])


async def _status_counts(collection: Any, field: str) -> dict[str, int]:
    rows = await collection.aggregate(
        [{"$group": {"_id": f"${field}", "count": {"$sum": 1}}}]
    ).to_list(length=None)
    return {
        str(row.get("_id") or "unknown"): int(row.get("count", 0))
        for row in rows
    }


@router.get("/stats")
async def stats(db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    news = db["news_data"]
    stocks = db["stock_daily_detail"]
    works = db["creator_works"]
    reports = db["daily_market_analysis"]

    latest_stock = await stocks.find_one(
        {}, sort=[("trade_date_int", -1)], projection={"_id": 0, "trade_date": 1}
    )
    stock_groups = await stocks.aggregate(
        [{"$group": {"_id": "$code"}}, {"$count": "count"}]
    ).to_list(length=1)
    stock_count = int(stock_groups[0]["count"]) if stock_groups else 0

    return {
        "news": {
            "total": await news.count_documents({}),
            "status_counts": await _status_counts(news, "status.status"),
        },
        "stocks": {
            "document_count": await stocks.count_documents({}),
            "stock_count": stock_count,
            "latest_trade_date": (latest_stock or {}).get("trade_date"),
        },
        "creator_works": {
            "total": await works.count_documents({}),
            "status_counts": await _status_counts(works, "status.status"),
            "a_share_relevant_count": await works.count_documents(
                {"is_a_share_relevant": True}
            ),
        },
        "morning_analyses": {
            "total": await reports.count_documents({}),
            "latest_analysis_date": (
                await reports.find_one({}, sort=[("analysis_date", -1)], projection={"_id": 0, "analysis_date": 1})
                or {}
            ).get("analysis_date"),
        },
    }
