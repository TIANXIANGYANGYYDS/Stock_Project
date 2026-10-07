from __future__ import annotations

import re
import asyncio
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import aggregate_page, find_page
from app.api.serializers import serialize_document
from app.repositories.realtime_snapshot_repository import decode_batch, SNAPSHOT_COLLECTION, AUCTION_COLLECTION


router = APIRouter(tags=["stocks"])


async def _list_stock_batches(code, trade_date, start_time, end_time, limit, db, *, collection_name):
    """Bound reads to fifteen minutes; source and receipt clocks remain distinct."""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(f'{trade_date}T{start_time}+08:00')
        end = datetime.fromisoformat(f'{trade_date}T{end_time}+08:00')
    except ValueError:
        raise HTTPException(422, '时间格式无效')
    if not re.fullmatch(r'\d{6}', code) or not 0 < (end-start).total_seconds() <= 900:
        raise HTTPException(422, '股票代码须为六位，查询窗口须大于零且不超过十五分钟')
    rows = []
    filters = {'trade_date': trade_date.isoformat(), 'observed_at': {'$gte': start, '$lt': end}}
    batches = db[collection_name].find(filters).sort([('observed_at', 1)]).limit(limit)
    count = 0
    next_observed_at = None
    async for batch in batches:
        count += 1
        next_observed_at = batch['observed_at'].isoformat()
        envelope = await asyncio.to_thread(decode_batch, batch)
        rows.extend(dict(q, observed_at=envelope['observed_at'])
                    for q in envelope['quotes'] if q['code'] == code)
    return {'data': rows, 'batches': count, 'possibly_truncated': count == limit,
            'collection': collection_name,
            'last_observed_at': next_observed_at,
            'sampling': 'public_quote_snapshots_not_exchange_ticks'}


@router.get('/api/v1/stocks/{code}/snapshots')
async def list_stock_snapshots(
    code: str,
    trade_date: date,
    start_time: str = Query(default='09:30:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    end_time: str = Query(default='09:45:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Read intraday quote batches; auction batches have their own endpoint."""
    return await _list_stock_batches(code, trade_date, start_time, end_time, limit, db,
                                     collection_name=SNAPSHOT_COLLECTION)


@router.get('/api/v1/stocks/{code}/auctions')
async def list_stock_auctions(
    code: str,
    trade_date: date,
    start_time: str = Query(default='09:15:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    end_time: str = Query(default='09:30:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Read opening/closing auction observations from their separate collection."""
    return await _list_stock_batches(code, trade_date, start_time, end_time, limit, db,
                                     collection_name=AUCTION_COLLECTION)


@router.get('/api/v1/market/realtime-quality/{trade_date}')
async def realtime_quality(trade_date: date, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    """Return bounded collection diagnostics without decompressing quote data."""
    pipeline = [
        {'$match': {'trade_date': trade_date.isoformat()}},
        {'$group': {'_id': '$session', 'batches': {'$sum': 1},
                    'first_observed_at': {'$min': '$observed_at'}, 'last_observed_at': {'$max': '$observed_at'},
                    'requested_observations': {'$sum': '$requested'}, 'returned_observations': {'$sum': '$returned'},
                    'usable_observations': {'$sum': '$usable'}, 'quality_issue_count': {'$sum': '$quality_issue_count'},
                    'compressed_bytes': {'$sum': '$compressed_bytes'}}},
    ]
    data = []
    for kind, collection_name in (('snapshot', SNAPSHOT_COLLECTION), ('auction', AUCTION_COLLECTION)):
        rows = await db[collection_name].aggregate(pipeline).to_list(length=10)
        data.extend(serialize_document(row) | {'session': row['_id'], 'data_kind': kind,
                                              'collection': collection_name} for row in rows)
    minute_quality = await db['stock_realtime_minute_quality'].find_one({'trade_date': trade_date.isoformat()})
    return {'data': data,
            'minute_quality': serialize_document(minute_quality) if minute_quality else None,
            'complete': False, 'coverage_basis': 'observed_samples_only_no_tick_completeness_claim'}


@router.get("/api/v1/market/latest-trade-date")
async def get_latest_trade_date(
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    stock_row = await db["stock_daily_detail"].find_one(
        {"adjust": "qfq"},
        projection={"_id": 0, "trade_date": 1},
        sort=[("trade_date", -1)],
    )
    analysis_row = await db["daily_market_analysis"].find_one(
        {},
        projection={"_id": 0, "analysis_date": 1},
        sort=[("analysis_date", -1)],
    )
    return {
        "data": {
            "latest_trade_date": (stock_row or {}).get("trade_date"),
            "latest_analysis_date": (analysis_row or {}).get("analysis_date"),
        }
    }


@router.get("/api/v1/stocks")
async def list_stocks(
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
    keyword: str | None = Query(default=None, min_length=1),
    adjust: str = Query(default="qfq"),
) -> dict[str, Any]:
    # Keep the indexed latest-per-code selection ahead of name filtering.
    # A historical name match must not turn an old bar into the latest quote.
    pipeline: list[dict[str, Any]] = [
        {"$match": {"adjust": adjust}},
        {"$sort": {"code": 1, "trade_date_int": -1}},
        {"$group": {"_id": "$code", "latest": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$latest"}},
    ]
    if keyword:
        escaped = re.escape(keyword)
        pipeline.append({"$match": {"$or": [
            {"code": {"$regex": escaped, "$options": "i"}},
            {"name": {"$regex": escaped, "$options": "i"}},
        ]}})
    pipeline.extend([
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
    ])
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
