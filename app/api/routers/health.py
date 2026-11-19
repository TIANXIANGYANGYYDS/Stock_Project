from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi.responses import JSONResponse

from app.api.dependencies import get_db
from app.scheduler.heartbeat import heartbeat_status
from app.services.trading_calendar_service import get_a_share_calendar
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID


logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
async def health(db: AsyncIOMotorDatabase = Depends(get_db)) -> dict[str, str]:
    try:
        await db.command("ping")
    except Exception as exc:
        logger.exception("MongoDB health check failed")
        raise HTTPException(
            status_code=503,
            detail="MongoDB 不可用",
        ) from exc
    return {"status": "ok", "mongodb": "ok"}


def expected_completed_date():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    cutoff = now.date() if now.hour >= 19 else now.date() - timedelta(days=1)
    sessions = get_a_share_calendar().sessions_in_range(cutoff - timedelta(days=40), cutoff)
    return sessions[-1].strftime('%Y-%m-%d')


@router.get('/api/v1/health/ready')
async def readiness(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Business readiness: an alive API does not imply a progressing scheduler."""
    await health(db)
    expected = expected_completed_date()
    scheduler = heartbeat_status()
    daily = await db['stock_daily_detail'].find_one({'trade_date': expected}, {'_id': 0, 'trade_date': 1},
                                                   sort=[('trade_date', -1), ('code', -1)])
    daily_run = await db['stock_daily_detail_sync_runs'].find_one(
        {'target_trade_date': expected, 'adjust': 'qfq', 'scope_key': 'all'},
        {'_id': 0, 'status': 1, 'expected_count': 1, 'success_count': 1, 'failed_count': 1},
        sort=[('started_at', -1)])
    quant = await db['quant_daily_results'].find_one(
        {'strategy_id': STRATEGY_ID, 'trade_date': expected},
        {'_id': 0, 'trade_date': 1, 'status': 1, 'runtime.data_status': 1,
         'runtime.incomplete_code_count': 1, 'runtime.incomplete_codes': 1}, sort=[('trade_date', -1)])
    sentiment = await db['short_sentiment_snapshots'].find_one(
        {'scope': 'all_a', 'trade_date': expected}, {'_id': 0, 'trade_date': 1, 'meta.is_final': 1}, sort=[('trade_date', -1)])
    checks = {
        'daily': {'latest': (daily or {}).get('trade_date'), 'sync': daily_run or {}},
        'quant': {'latest': (quant or {}).get('trade_date'), 'status': (quant or {}).get('status'),
                  'data_status': (quant or {}).get('runtime', {}).get('data_status'),
                  'incomplete_code_count': (quant or {}).get('runtime', {}).get('incomplete_code_count')},
        'sentiment': {'latest': (sentiment or {}).get('trade_date'),
                      'is_final': (sentiment or {}).get('meta', {}).get('is_final', False)},
    }
    for value in checks.values():
        value['current'] = bool(value['latest'] and value['latest'] >= expected)
    incomplete = (quant or {}).get('runtime', {}).get('incomplete_codes', [])
    suspended = []
    # A verified suspension has no candles. Preserve the ledger's partial flag,
    # but distinguish it from missing data when deciding business readiness.
    # Keep this endpoint bounded during a broad outage.
    if checks['quant']['data_status'] == 'closed_partial' and 0 < len(incomplete) <= 20:
        for code in incomplete:
            evidence = await db['market_sentiment_raw_daily'].find_one({
                'scope': 'hs_mainboard', 'trade_date': expected,
                'market.stocks': {'$elemMatch': {
                    'code': code, 'is_suspended': True,
                    'status_source': 'baostock.daily_observation',
                    'volume_shares': {'$in': [None, 0]}, 'amount_yuan': {'$in': [None, 0]},
                }},
            }, {'_id': 0, 'trade_date': 1})
            if evidence:
                suspended.append(code)
    checks['quant']['verified_suspended_codes'] = suspended
    quant_complete = checks['quant']['data_status'] == 'closed' or (
        bool(incomplete) and set(incomplete) == set(suspended)
        and checks['quant']['incomplete_code_count'] == len(incomplete))
    # Verify the required completed session even when a newer intraday snapshot exists.
    ready = scheduler['status'] == 'ok' and all(v['current'] for v in checks.values())
    ready = ready and (daily_run or {}).get('status') == 'success'
    ready = ready and checks['quant']['status'] == 'closed' and quant_complete
    ready = ready and checks['sentiment']['is_final']
    return JSONResponse(status_code=200 if ready else 503, content={
        'status': 'ok' if ready else 'degraded', 'mongodb': 'ok',
        'expected_completed_trade_date': expected, 'scheduler': scheduler, 'data': checks})
