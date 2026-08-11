from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import (
    get_db,
    get_realtime_index_service,
    get_realtime_stock_crawler,
)
from app.api.serializers import serialize_document
from app.crawlers.realtime_market_crawler import RealtimeMarketCrawler, RealtimeQuote
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


def _quote_item(quote: RealtimeQuote) -> dict[str, Any]:
    return {
        "code": quote.code,
        "name": quote.name,
        "market": quote.market,
        "price": quote.price,
        "volume": quote.volume,
        "amount": quote.amount,
        "source_time": quote.market_data_time.isoformat()
        if quote.market_data_time
        else None,
        "received_at": quote.received_at.isoformat(),
        "provider": quote.provider.lower(),
    }


def _quote_response(
    quotes: list[RealtimeQuote],
    requested_codes: list[str],
) -> dict[str, Any]:
    quotes_by_code = {quote.code: quote for quote in quotes}
    ordered_quotes = [quotes_by_code[code] for code in requested_codes if code in quotes_by_code]
    source_dates = [
        quote.market_data_time.astimezone(CN_TZ).date().isoformat()
        for quote in ordered_quotes
        if quote.market_data_time
    ]
    now = datetime.now(CN_TZ)
    trading_date = max(source_dates) if source_dates else None
    return {
        "trading_date": trading_date,
        "market_status": (
            "open"
            if trading_date == now.date().isoformat() and is_market_session_open(now)
            else "closed"
        ),
        "items": [_quote_item(quote) for quote in ordered_quotes],
        "missing_codes": [code for code in requested_codes if code not in quotes_by_code],
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
    crawler: RealtimeMarketCrawler = Depends(get_realtime_stock_crawler),
) -> dict[str, Any]:
    try:
        normalized = _normalize_codes(codes.split(","))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    quotes, _ = await crawler.fetch_quotes(normalized)
    if not quotes:
        raise HTTPException(status_code=503, detail="实时股票行情暂不可用")
    return {"data": _quote_response(quotes, normalized)}


@router.get("/api/v1/stocks/{code}/intraday")
async def get_stock_intraday(
    code: str,
    trade_date: date | None = Query(default=None),
    interval: RealtimeInterval = Query(default="1m"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    try:
        normalized = _normalize_codes([code])[0]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    target_date = (trade_date or datetime.now(CN_TZ).date()).isoformat()
    rows = await db["stock_realtime_minute_bars"].find(
        {
            "code": normalized,
            "trade_date": target_date,
            "interval": interval,
        }
    ).sort([("timestamp", 1)]).to_list(length=None)
    items = [serialize_document(row) for row in rows]
    return {
        "data": {
            "code": normalized,
            "name": items[0].get("name") if items else None,
            "trade_date": target_date,
            "interval": interval,
            "count": len(items),
            "items": items,
        }
    }


@router.get("/api/v1/stocks/{code}/realtime")
async def get_realtime_stock(
    code: str,
    crawler: RealtimeMarketCrawler = Depends(get_realtime_stock_crawler),
) -> dict[str, Any]:
    try:
        normalized = _normalize_codes([code])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    quotes, _ = await crawler.fetch_quotes(normalized)
    if not quotes:
        raise HTTPException(status_code=503, detail="实时股票行情暂不可用")
    return {"data": _quote_response(quotes, normalized)}
