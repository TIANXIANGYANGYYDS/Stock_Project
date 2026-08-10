from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import get_db, get_realtime_index_service
from app.api.serializers import serialize_document
from app.services.realtime_index_service import (
    CN_TZ,
    RealtimeIndexService,
    RealtimeIndexUnavailable,
    is_market_session_open,
)


router = APIRouter(tags=["market"])
MAX_REALTIME_CODES = 200
RealtimeInterval = Literal["1m", "5m", "15m", "30m", "60m", "120m"]


def _normalize_codes(codes: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(str(code).strip().zfill(6) for code in codes))
    if not normalized or any(len(code) != 6 or not code.isdigit() for code in normalized):
        raise ValueError("codes 必须是六位数字股票代码")
    if len(normalized) > MAX_REALTIME_CODES:
        raise ValueError(f"codes 一次最多请求 {MAX_REALTIME_CODES} 只股票")
    return normalized


async def _latest_stock_bars(
    db: AsyncIOMotorDatabase,
    codes: list[str],
    interval: RealtimeInterval,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"code": {"$in": codes}, "interval": interval}},
        {"$sort": {"timestamp": -1}},
        {"$group": {"_id": "$code", "latest": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$latest"}},
    ]
    rows = await db["stock_realtime_minute_bars"].aggregate(pipeline).to_list(
        length=None
    )
    rows_by_code = {str(row.get("code")): row for row in rows}
    return [rows_by_code[code] for code in codes if code in rows_by_code]


def _stock_response(
    rows: list[dict[str, Any]],
    requested_codes: list[str],
    interval: RealtimeInterval,
) -> dict[str, Any]:
    items = [serialize_document(row) for row in rows]
    trading_dates = [str(item.get("trade_date")) for item in items if item.get("trade_date")]
    trading_date = max(trading_dates) if trading_dates else None
    now = datetime.now(CN_TZ)
    market_status = (
        "open"
        if trading_date == now.date().isoformat() and is_market_session_open(now)
        else "closed"
    )
    returned_codes = {str(item.get("code")) for item in items}
    return {
        "trading_date": trading_date,
        "market_status": market_status,
        "interval": interval,
        "items": items,
        "missing_codes": [code for code in requested_codes if code not in returned_codes],
    }


@router.get("/api/v1/market/indices/realtime")
async def get_realtime_indices(
    service: RealtimeIndexService = Depends(get_realtime_index_service),
) -> dict[str, Any]:
    try:
        return {"data": await service.fetch_latest()}
    except RealtimeIndexUnavailable as exc:
        raise HTTPException(status_code=503, detail="实时指数行情暂不可用") from exc


@router.get("/api/v1/stocks/realtime")
async def get_realtime_stocks(
    codes: str = Query(..., description="逗号分隔的六位股票代码，最多 200 只"),
    interval: RealtimeInterval = Query(default="1m"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    try:
        normalized = _normalize_codes(codes.split(","))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await _latest_stock_bars(db, normalized, interval)
    return {"data": _stock_response(rows, normalized, interval)}


@router.get("/api/v1/stocks/{code}/realtime")
async def get_realtime_stock(
    code: str,
    interval: RealtimeInterval = Query(default="1m"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    try:
        normalized = _normalize_codes([code])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = await _latest_stock_bars(db, normalized, interval)
    if not rows:
        raise HTTPException(status_code=404, detail="没有找到对应股票实时行情")
    return {"data": _stock_response(rows, normalized, interval)}
