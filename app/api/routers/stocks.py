from __future__ import annotations

import re
import asyncio
from datetime import date
from typing import Any, Literal

from bson.codec_options import CodecOptions
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies import Pagination, get_db, get_pagination
from app.api.query import aggregate_page, find_page
from app.api.serializers import serialize_document
from app.repositories.quote_chart_repository import read_chart_window
from app.repositories.realtime_snapshot_repository import (
    decode_symbol_batches, symbol_projection, SNAPSHOT_COLLECTION, AUCTION_COLLECTION, PARTITIONED_CODEC,
    QUALITY_INDEX_NAME,
)


router = APIRouter(tags=["stocks"])


@router.get('/api/v1/stocks/{code}/fund-flows')
async def list_stock_fund_flows(
    code: str,
    trade_date: date,
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    """Bounded pages of compressed seconds observations; five sessions retained."""
    from app.repositories.market_observation_repository import FUND_FLOW_COLLECTION
    from app.repositories.fund_flow_repository import fund_symbol_projection, decode_fund_snapshot
    if not re.fullmatch(r'\d{6}', code):
        raise HTTPException(422, '股票代码须为六位')
    collection = db[FUND_FLOW_COLLECTION].with_options(codec_options=CodecOptions(tz_aware=True))
    filters = {'trade_date': trade_date.isoformat()}
    total = await collection.count_documents(filters)
    documents = await collection.find(filters, fund_symbol_projection(code)).sort(
        [('observed_at', -1), ('_id', -1)]).skip(pagination.skip).limit(pagination.page_size).to_list(
            length=pagination.page_size)
    items = await asyncio.to_thread(lambda: [serialize_document(decode_fund_snapshot(d, code)) for d in documents])
    return {'items': items, 'total': total, 'page': pagination.page, 'page_size': pagination.page_size,
            'total_basis': 'observation_batches_empty_records_mean_symbol_missing',
            'sampling': 'fixed_5s_cumulative_flow_observations', 'retention_trading_days': 5}


@router.get('/api/v1/stocks/{code}/fund-flows/daily')
async def list_stock_daily_fund_flows(
    code: str,
    start_date: date | None = None,
    end_date: date | None = None,
    pagination: Pagination = Depends(get_pagination),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict[str, Any]:
    from app.repositories.fund_flow_repository import DAILY_COLLECTION
    if not re.fullmatch(r'\d{6}', code) or start_date and end_date and start_date > end_date:
        raise HTTPException(422, '股票代码或日期范围无效')
    filters = {'code': code}
    if start_date or end_date:
        filters['trade_date'] = {}
        if start_date:
            filters['trade_date']['$gte'] = start_date.isoformat()
        if end_date:
            filters['trade_date']['$lte'] = end_date.isoformat()
    collection = db[DAILY_COLLECTION].with_options(codec_options=CodecOptions(tz_aware=True))
    return await find_page(collection, filters, pagination, sort=[('trade_date', -1), ('code', 1)])


@router.get('/api/v1/market/fund-flow-quality/{trade_date}')
async def fund_flow_quality(trade_date: date, db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, Any]:
    from app.repositories.market_observation_repository import FUND_FLOW_COLLECTION, RUN_COLLECTION
    from app.repositories.fund_flow_repository import DAILY_COLLECTION, DAILY_RUN_KIND
    collection = db[FUND_FLOW_COLLECTION].with_options(codec_options=CodecOptions(tz_aware=True))
    runs = db[RUN_COLLECTION].with_options(codec_options=CodecOptions(tz_aware=True))
    latest = await collection.find_one(
        {'trade_date': trade_date.isoformat()}, {'records': 0, 'payload': 0, 'quote_chunks': 0,
                                              'chunk_sha256': 0}, sort=[('observed_at', -1)])
    run = await runs.find_one({'_id': f'fund_flow:{trade_date.isoformat()}'})
    daily_run = await runs.find_one({'_id': f'{DAILY_RUN_KIND}:{trade_date.isoformat()}'})
    return {'latest_snapshot': serialize_document(latest) if latest else None,
            'latest_run': serialize_document(run) if run else None,
            'daily_run': serialize_document(daily_run) if daily_run else None,
            'daily_count': await db[DAILY_COLLECTION].count_documents({'trade_date': trade_date.isoformat()}),
            'raw_retention_trading_days': 5, 'daily_retention': 'permanent'}


async def _list_stock_batches(code, trade_date, start_time, end_time, limit, db, *, collection_name, view="full"):
    """按视图限制批次数和时间范围，保留行情时间与采集时间。"""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(f'{trade_date}T{start_time}+08:00')
        end = datetime.fromisoformat(f'{trade_date}T{end_time}+08:00')
    except ValueError:
        raise HTTPException(422, '时间格式无效')
    max_seconds, max_batches = (3600, 4000) if view == 'series' else (900, 1000)
    if not re.fullmatch(r'\d{6}', code) or not 0 < (end-start).total_seconds() <= max_seconds or limit > max_batches:
        raise HTTPException(422, f'股票代码须为六位，窗口须在 1 至 {max_seconds} 秒之间，批次数不超过 {max_batches}')
    if view != 'full':
        return await read_chart_window(db, collection_name, trade_date.isoformat(), start, end, limit, code, view)
    rows = []
    filters = {'trade_date': trade_date.isoformat(), 'observed_at': {'$gte': start, '$lt': end}}
    batches = db[collection_name].find(filters, symbol_projection(code)).sort([('observed_at', 1)]).limit(limit)
    count = 0
    next_observed_at = None
    pending = []
    async for batch in batches:
        count += 1
        next_observed_at = batch['observed_at'].isoformat()
        pending.append(batch)
        # Amortize thread dispatch for small projections. Legacy full-market
        # batches are still decoded immediately to bound memory during rollout.
        if len(pending) >= 32 or batch.get('codec') != PARTITIONED_CODEC:
            rows.extend(await asyncio.to_thread(decode_symbol_batches, pending, code))
            pending = []
    if pending:
        rows.extend(await asyncio.to_thread(decode_symbol_batches, pending, code))
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
    limit: int = Query(default=200, ge=1, le=4000),
    db: AsyncIOMotorDatabase = Depends(get_db),
    view: Literal['full', 'chart', 'series'] = 'full',
) -> dict[str, Any]:
    """Read intraday quote batches; auction batches have their own endpoint."""
    return await _list_stock_batches(code, trade_date, start_time, end_time, limit, db,
                                     collection_name=SNAPSHOT_COLLECTION, view=view)


@router.get('/api/v1/stocks/{code}/auctions')
async def list_stock_auctions(
    code: str,
    trade_date: date,
    start_time: str = Query(default='09:15:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    end_time: str = Query(default='09:30:00', pattern=r'^\d{2}:\d{2}:\d{2}$'),
    limit: int = Query(default=1000, ge=1, le=4000),
    db: AsyncIOMotorDatabase = Depends(get_db),
    view: Literal['full', 'chart', 'series'] = 'full',
) -> dict[str, Any]:
    """Read opening/closing auction observations from their separate collection."""
    return await _list_stock_batches(code, trade_date, start_time, end_time, limit, db,
                                     collection_name=AUCTION_COLLECTION, view=view)


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
        rows = await db[collection_name].aggregate(
            pipeline, hint=QUALITY_INDEX_NAME, maxTimeMS=10000).to_list(length=10)
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
