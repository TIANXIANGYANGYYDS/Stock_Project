from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional

from app.crawlers.realtime_market_crawler import RealtimeMarketCrawler, RealtimeQuote
from app.crawlers.stock_daily_detail_crawler import StockDailyDetailCrawler
from app.models.realtime_minute_bar import RealtimeMinuteBar, now_cn
from app.repositories.realtime_minute_bar_repository import RealtimeMinuteBarRepository
from app.services.trading_calendar_service import resolve_morning_trade_dates


logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
MORNING = (dt_time(9, 30), dt_time(11, 30))
AFTERNOON = (dt_time(13, 0), dt_time(15, 0))
POLL_INTERVAL_SECONDS = 5.0
REALTIME_BATCH_SIZE = 200
AGGREGATE_INTERVAL_MINUTES = (5, 15, 30, 60, 120)
SESSION_CLOCK_SKEW_GRACE_SECONDS = 5 * 60
SESSION_CLOSE_STABILITY_SECONDS = 5 * 60
SESSION_HARD_STOP_GRACE_SECONDS = 10 * 60
SOURCE_CLOCK_SAMPLE_LIMIT = 31
MIN_UNIVERSE_SYMBOLS = 1000
SOURCE_CLOCK_REFERENCE_CODES = frozenset(
    {"600519", "000001", "300750", "601318", "600036"}
)


@dataclass
class _MutableBar:
    code: str
    name: Optional[str]
    market: str
    trade_date: str
    interval: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    provider: str
    first_seen_at: datetime
    last_seen_at: datetime
    revision_count: int = 0

    def to_model(self, *, now: Optional[datetime] = None) -> RealtimeMinuteBar:
        updated_at = now or now_cn()
        return RealtimeMinuteBar(
            code=self.code,
            name=self.name,
            market=self.market,
            trade_date=self.trade_date,
            interval=self.interval,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=max(0.0, self.volume),
            amount=max(0.0, self.amount),
            provider=self.provider,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            revision_count=self.revision_count,
            created_at=self.first_seen_at,
            updated_at=updated_at,
        )


class RealtimeMinuteService:
    """Poll public snapshots during A-share sessions and persist local minute bars."""

    def __init__(
        self,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        batch_size: int = REALTIME_BATCH_SIZE,
    ) -> None:
        self.poll_interval = max(2.0, poll_interval)
        self.crawler = RealtimeMarketCrawler(batch_size=batch_size)
        self.universe_crawler = StockDailyDetailCrawler()
        self.repository = RealtimeMinuteBarRepository()
        self._bars: dict[tuple[str, str], _MutableBar] = {}
        self._aggregate_bars: dict[tuple[str, str, str], _MutableBar] = {}
        self._totals: dict[str, tuple[float, float]] = {}
        self._last_quote_signatures: dict[
            str, tuple[datetime, float, float, float]
        ] = {}
        self._source_clock_offsets: deque[float] = deque(maxlen=SOURCE_CLOCK_SAMPLE_LIMIT)

    async def close(self) -> None:
        await self.crawler.close()
        await self.universe_crawler.close()

    async def _load_universe(self, target_trade_date: str) -> list[dict[str, str]]:
        """Merge the latest stored universe with today's publicly traded list."""

        daily_collection = self.repository.database["stock_daily_detail"]
        latest = await daily_collection.find_one(
            {},
            projection={"_id": 0, "trade_date": 1},
            sort=[("trade_date_int", -1)],
        )
        by_code: dict[str, dict[str, str]] = {}
        latest_trade_date = latest.get("trade_date") if latest else None
        if latest_trade_date:
            documents = await daily_collection.find(
                {"trade_date": latest_trade_date},
                {"_id": 0, "code": 1, "name": 1},
            ).to_list(length=None)
            for item in documents:
                code = str(item.get("code") or "").strip().zfill(6)
                if len(code) == 6 and code.isdigit():
                    by_code[code] = {
                        "code": code,
                        "name": str(item.get("name") or "").strip(),
                    }
        mongo_symbol_count = len(by_code)
        fresh_by_code: dict[str, dict[str, str]] = {}
        try:
            dataframe = await self.universe_crawler.fetch_stock_list(
                target_trade_date=target_trade_date,
            )
            for item in dataframe.to_dict("records"):
                code = str(item.get("代码") or "").strip().zfill(6)
                name = str(item.get("名称") or "").strip()
                if len(code) == 6 and code.isdigit() and name:
                    fresh_by_code[code] = {"code": code, "name": name}
        except Exception as exc:
            logger.warning(
                "realtime_minute_universe_public_failed trade_date=%s error=%s",
                target_trade_date,
                type(exc).__name__,
            )

        if len(fresh_by_code) >= MIN_UNIVERSE_SYMBOLS:
            by_code.update(fresh_by_code)
        elif fresh_by_code:
            logger.warning(
                "realtime_minute_universe_public_rejected trade_date=%s symbols=%s",
                target_trade_date,
                len(fresh_by_code),
            )

        if len(by_code) < MIN_UNIVERSE_SYMBOLS:
            raise RuntimeError(
                "realtime stock universe is too small: "
                f"mongo={len(by_code)} public={len(fresh_by_code)}"
            )
        rows = [by_code[code] for code in sorted(by_code)]
        logger.info(
            "realtime_minute_universe_loaded target_trade_date=%s "
            "mongo_trade_date=%s mongo_symbols=%s public_symbols=%s merged_symbols=%s",
            target_trade_date,
            latest_trade_date,
            mongo_symbol_count,
            len(fresh_by_code),
            len(rows),
        )
        return rows

    @staticmethod
    def _session_window(session: str) -> tuple[dt_time, dt_time]:
        if session == "morning":
            return MORNING
        if session == "afternoon":
            return AFTERNOON
        raise ValueError(f"unsupported realtime session: {session}")

    @staticmethod
    def _minute_key(value: datetime) -> str:
        local = value.astimezone(CN_TZ).replace(second=0, microsecond=0)
        return local.isoformat()

    @staticmethod
    def _period_bucket_start(value: datetime, minutes: int) -> str:
        """Return a session-aligned bucket start without crossing lunch."""

        if minutes not in AGGREGATE_INTERVAL_MINUTES:
            raise ValueError(f"unsupported realtime aggregate interval: {minutes}")
        local = value.astimezone(CN_TZ).replace(second=0, microsecond=0)
        if MORNING[0] <= local.time() < MORNING[1]:
            session_start = local.replace(hour=9, minute=30)
        elif AFTERNOON[0] <= local.time() < AFTERNOON[1]:
            session_start = local.replace(hour=13, minute=0)
        else:
            raise ValueError(f"bar timestamp outside trading session: {local.isoformat()}")
        elapsed_minutes = int((local - session_start).total_seconds() // 60)
        bucket_offset = elapsed_minutes // minutes * minutes
        return (session_start + timedelta(minutes=bucket_offset)).isoformat()

    @staticmethod
    def _bar_time_in_session(
        quote: RealtimeQuote,
        now: datetime,
        start: dt_time,
        end: dt_time,
    ) -> Optional[datetime]:
        market_time = quote.market_data_time
        if market_time is None:
            return None
        local = market_time.astimezone(CN_TZ)
        if local.date() != now.astimezone(CN_TZ).date():
            return None
        session_start = datetime.combine(local.date(), start, tzinfo=CN_TZ)
        session_end = datetime.combine(local.date(), end, tzinfo=CN_TZ)
        if session_start <= local < session_end:
            return local
        if session_end <= local < session_end + timedelta(minutes=1):
            return session_end - timedelta(microseconds=1)
        return None

    def _observe_source_clock(self, quote: RealtimeQuote) -> None:
        """Estimate source-time minus local-server-time during open sessions."""

        if quote.market_data_time is None:
            return
        if quote.code not in SOURCE_CLOCK_REFERENCE_CODES:
            return
        source_time = quote.market_data_time.astimezone(CN_TZ)
        received_time = quote.received_at.astimezone(CN_TZ)
        if source_time.date() != received_time.date():
            return
        source_clock = source_time.time()
        in_live_clock_window = (
            dt_time(9, 25) <= source_clock < MORNING[1]
            or dt_time(12, 55) <= source_clock < AFTERNOON[1]
        )
        if not in_live_clock_window:
            return
        offset = (source_time - received_time).total_seconds()
        if abs(offset) <= SESSION_CLOCK_SKEW_GRACE_SECONDS:
            self._source_clock_offsets.append(offset)

    def _clock_offset_seconds(self) -> float:
        if not self._source_clock_offsets:
            return 0.0
        return float(statistics.median(self._source_clock_offsets))

    def _exchange_now(self, value: datetime) -> datetime:
        return value + timedelta(seconds=self._clock_offset_seconds())

    def _ingest_quote(
        self,
        quote: RealtimeQuote,
        *,
        bar_time: Optional[datetime] = None,
    ) -> None:
        if quote.price <= 0 or quote.volume < 0 or quote.amount < 0 or quote.market_data_time is None:
            return
        signature = (
            quote.market_data_time,
            quote.price,
            quote.volume,
            quote.amount,
        )
        if self._last_quote_signatures.get(quote.code) == signature:
            return
        self._last_quote_signatures[quote.code] = signature
        effective_time = bar_time or quote.market_data_time
        minute = self._minute_key(effective_time)
        key = (quote.code, minute)
        first_seen = self._exchange_now(quote.received_at)
        previous = self._totals.get(quote.code)
        self._totals[quote.code] = (quote.volume, quote.amount)
        delta_volume = 0.0 if previous is None else max(0.0, quote.volume - previous[0])
        delta_amount = 0.0 if previous is None else max(0.0, quote.amount - previous[1])
        current = self._bars.get(key)
        if current is None:
            self._bars[key] = _MutableBar(
                code=quote.code,
                name=quote.name,
                market=quote.market,
                trade_date=effective_time.astimezone(CN_TZ).date().isoformat(),
                interval="1m",
                timestamp=minute,
                open=quote.price,
                high=quote.price,
                low=quote.price,
                close=quote.price,
                volume=delta_volume,
                amount=delta_amount,
                provider=quote.provider,
                first_seen_at=first_seen,
                last_seen_at=first_seen,
            )
            return
        changed = (
            current.close != quote.price
            or current.high != max(current.high, quote.price)
            or current.low != min(current.low, quote.price)
            or delta_volume != 0
            or delta_amount != 0
        )
        current.high = max(current.high, quote.price)
        current.low = min(current.low, quote.price)
        current.close = quote.price
        current.volume += delta_volume
        current.amount += delta_amount
        current.provider = quote.provider
        current.last_seen_at = first_seen
        if changed:
            current.revision_count += 1

    def _update_aggregate_bars(self, minute_bar: _MutableBar) -> list[_MutableBar]:
        updated: list[_MutableBar] = []
        minute_time = datetime.fromisoformat(minute_bar.timestamp)
        for minutes in AGGREGATE_INTERVAL_MINUTES:
            interval = f"{minutes}m"
            timestamp = self._period_bucket_start(minute_time, minutes)
            key = (minute_bar.code, interval, timestamp)
            current = self._aggregate_bars.get(key)
            if current is None:
                current = _MutableBar(
                    code=minute_bar.code,
                    name=minute_bar.name,
                    market=minute_bar.market,
                    trade_date=minute_bar.trade_date,
                    interval=interval,
                    timestamp=timestamp,
                    open=minute_bar.open,
                    high=minute_bar.high,
                    low=minute_bar.low,
                    close=minute_bar.close,
                    volume=minute_bar.volume,
                    amount=minute_bar.amount,
                    provider=minute_bar.provider,
                    first_seen_at=minute_bar.first_seen_at,
                    last_seen_at=minute_bar.last_seen_at,
                    revision_count=minute_bar.revision_count,
                )
                self._aggregate_bars[key] = current
            else:
                current.high = max(current.high, minute_bar.high)
                current.low = min(current.low, minute_bar.low)
                current.close = minute_bar.close
                current.volume += minute_bar.volume
                current.amount += minute_bar.amount
                current.provider = minute_bar.provider
                current.last_seen_at = max(current.last_seen_at, minute_bar.last_seen_at)
                current.revision_count += minute_bar.revision_count + 1
            updated.append(current)
        return updated

    async def _flush_before(self, cutoff: Optional[str], *, force: bool = False) -> int:
        if force:
            selected = list(self._bars.values())
            self._bars.clear()
        else:
            selected = [bar for key, bar in self._bars.items() if cutoff and key[1] < cutoff]
            for bar in selected:
                self._bars.pop((bar.code, bar.timestamp), None)
        if not selected:
            return 0
        selected.sort(key=lambda bar: (bar.timestamp, bar.code))
        aggregate_updates: dict[tuple[str, str, str], _MutableBar] = {}
        for minute_bar in selected:
            for aggregate_bar in self._update_aggregate_bars(minute_bar):
                key = (aggregate_bar.code, aggregate_bar.interval, aggregate_bar.timestamp)
                aggregate_updates[key] = aggregate_bar
        corrected_now = self._exchange_now(datetime.now(CN_TZ))
        documents = [bar.to_model(now=corrected_now) for bar in selected]
        documents.extend(
            bar.to_model(now=corrected_now) for bar in aggregate_updates.values()
        )
        written = await self.repository.upsert_bars(documents)
        logger.info(
            "realtime_minute_bars_flushed minute_bars=%s aggregate_bars=%s written=%s",
            len(selected),
            len(aggregate_updates),
            written,
        )
        return written

    async def run_session(self, session: str) -> dict[str, Any]:
        start_time, end_time = self._session_window(session)
        now = datetime.now(CN_TZ)
        session_start = datetime.combine(now.date(), start_time, tzinfo=CN_TZ)
        session_end = datetime.combine(now.date(), end_time, tzinfo=CN_TZ)
        wall_deadline = session_end + timedelta(seconds=SESSION_HARD_STOP_GRACE_SECONDS)
        trade_date = resolve_morning_trade_dates(now.date())
        if not trade_date.is_current_trade_day:
            logger.info(
                "realtime_minute_session_skipped session=%s reason=non_trading_day",
                session,
            )
            return {"session": session, "status": "skipped", "reason": "non_trading_day"}
        if not (
            session_start - timedelta(seconds=SESSION_CLOCK_SKEW_GRACE_SECONDS)
            <= now
            < wall_deadline
        ):
            logger.info("realtime_minute_session_skipped session=%s reason=outside_trading_window", session)
            return {"session": session, "status": "skipped", "reason": "outside_trading_window"}

        await self.repository.create_indexes()
        rows = await self._load_universe(trade_date.reference_date)
        codes = [row["code"] for row in rows]
        name_by_code = {row["code"]: row["name"] for row in rows}
        started = time.perf_counter()
        cycles = 0
        fetched = 0
        written = 0
        try:
            close_target = session_end + timedelta(seconds=SESSION_CLOSE_STABILITY_SECONDS)
            while True:
                local_now = datetime.now(CN_TZ)
                if self._exchange_now(local_now) >= close_target:
                    break
                if local_now >= wall_deadline:
                    logger.warning(
                        "realtime_minute_session_hard_stop session=%s "
                        "clock_offset_s=%.3f",
                        session,
                        self._clock_offset_seconds(),
                    )
                    break
                cycle_started = time.perf_counter()
                cycle_now = local_now
                quotes, metrics = await self.crawler.fetch_quotes(codes)
                cycles += 1
                latest_market_time: Optional[datetime] = None
                for quote in quotes:
                    if quote.code not in name_by_code:
                        continue
                    self._observe_source_clock(quote)
                    bar_time = self._bar_time_in_session(
                        quote,
                        cycle_now,
                        start_time,
                        end_time,
                    )
                    if bar_time is None:
                        continue
                    self._ingest_quote(quote, bar_time=bar_time)
                    quote_time = quote.market_data_time.astimezone(CN_TZ)
                    if latest_market_time is None or quote_time > latest_market_time:
                        latest_market_time = quote_time
                fetched += len(quotes)
                if latest_market_time is not None:
                    written += await self._flush_before(
                        self._minute_key(latest_market_time)
                    )
                logger.info(
                    "realtime_minute_cycle session=%s cycle=%s requested=%s returned=%s "
                    "requests=%s fallback_batches=%s failed_batches=%s elapsed_ms=%s "
                    "clock_offset_s=%.3f",
                    session,
                    cycles,
                    metrics["requested"],
                    metrics["returned"],
                    metrics["requests"],
                    metrics["fallback_batches"],
                    metrics["failed_batches"],
                    metrics["elapsed_ms"],
                    self._clock_offset_seconds(),
                )
                delay = self.poll_interval - (time.perf_counter() - cycle_started)
                if delay > 0:
                    await asyncio.sleep(delay)
        finally:
            written += await self._flush_before(None, force=True)
        result = {
            "session": session,
            "status": "completed",
            "symbols": len(rows),
            "cycles": cycles,
            "quotes": fetched,
            "bars_written": written,
            "clock_offset_s": round(self._clock_offset_seconds(), 3),
            "elapsed_s": round(time.perf_counter() - started, 3),
        }
        logger.info("realtime_minute_session_finished %s", result)
        return result
