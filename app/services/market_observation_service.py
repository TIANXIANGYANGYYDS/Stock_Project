"""Scheduled collection with explicit receipt times and bounded retries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import time

from app.crawlers.market_observation_crawler import MarketObservationCrawler
from app.crawlers.realtime_market_crawler import CN_TZ, RealtimeMarketCrawler
from app.repositories.market_observation_repository import MarketObservationRepository
from app.services.stock_daily_detail_service import resolve_a_stock_target_trade_date

logger = logging.getLogger(__name__)
_offset = None
_checked = 0.0
_checked_wall = None
_verification = None


async def _verify_clock():
    """同一轮校时只采一次，两路校时源均通过后才发布新偏移。"""
    global _offset, _checked, _checked_wall
    clock = RealtimeMarketCrawler()
    try:
        value = await clock.clock_offset()
    finally:
        await clock.close()
    if value is None:
        raise RuntimeError("market_observation_clock_unverified")
    _offset, _checked = value, time.monotonic()
    _checked_wall = datetime.now(CN_TZ)


async def observation_now():
    """复用双来源校时结果，缓存过期或系统时间跳变时重新验证。"""
    global _offset, _verification
    wall = datetime.now(CN_TZ)
    elapsed = time.monotonic() - _checked
    clock_changed = (_checked_wall is not None
                     and abs((wall - _checked_wall).total_seconds() - elapsed) > 1)
    if _offset is None or elapsed >= 300 or clock_changed:
        # 系统时间跳变后旧偏移立即失效，重新校验失败时保留未知状态。
        _offset = None
        if _verification is None or _verification.done():
            _verification = asyncio.create_task(_verify_clock())
        await asyncio.shield(_verification)
        # 等待期间另一个任务可能检测到新的系统校时跳变。
        if _offset is None:
            raise RuntimeError("market_observation_clock_unverified")
    return datetime.now(CN_TZ) + timedelta(seconds=_offset), _offset


def hot_slot(now):
    clock = now.astimezone(CN_TZ).strftime("%H:%M")
    if not ("09:30" <= clock <= "11:30" or "13:00" <= clock <= "15:00"):
        return None
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


class MarketObservationService:
    def __init__(self, repository=None, crawler=None):
        self.repo = repository or MarketObservationRepository()
        self.crawler = crawler or MarketObservationCrawler()

    async def close(self):
        await self.crawler.close()

    async def collect(self, kind, *, now, clock_offset=0, manual=False, trade_date=None):
        day = trade_date or now.astimezone(CN_TZ).date().isoformat()
        decision = await resolve_a_stock_target_trade_date(day.replace("-", ""))
        if not decision.is_reference_trade_day:
            return {"status": "skipped", "reason": "non_trading_day"}
        if kind == "hot":
            if day != now.astimezone(CN_TZ).date().isoformat():
                raise ValueError("hot_rank_has_no_historical_query")
            slot = hot_slot(now)
            if slot is None and not manual:
                return {"status": "skipped", "reason": "outside_session"}
            identity = f"{day}:{slot.strftime('%H:%M')}" if not manual else f"manual:{now.isoformat()}"
        elif kind == "billboard":
            if day > now.date().isoformat() or day == now.date().isoformat() and now.hour < 16:
                return {"status": "skipped", "reason": "before_1600"}
            identity = day
        else:
            raise ValueError("unknown observation kind")
        await self.repo.create_indexes()
        if await self.repo.exists_snapshot(kind, identity):
            return {"status": "skipped", "reason": "already_collected", "id": identity}
        for attempt in range(3):
            try:
                if kind == "hot":
                    data = await self.crawler.fetch_hot_rank()
                else:
                    data = await self.crawler.fetch_billboard(day)
                observed = data["fetched_at"] + timedelta(seconds=clock_offset)
                if kind == "hot":
                    if observed.astimezone(CN_TZ).date().isoformat() != day:
                        raise ValueError("observation_fetch_crossed_day")
                    if not manual and hot_slot(observed) != slot:
                        raise ValueError("hot_rank_fetch_crossed_slot")
                data.update(trade_date=day, observed_at=observed,
                            clock_offset_seconds=clock_offset, run_mode="manual" if manual else "scheduled")
                if kind == "hot":
                    data["scheduled_slot"] = None if manual else slot
                await self.repo.save(kind, identity, data)
                status = data.get("collection_status", "success")
                await self.repo.record_run(kind, identity, status=status)
                return {"status": status, "id": identity,
                        "count": len(data["stocks"] if kind == "hot" else data["records"])}
            except Exception as exc:
                await self.repo.record_run(kind, identity, status="failed", error=str(exc)[:300])
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
