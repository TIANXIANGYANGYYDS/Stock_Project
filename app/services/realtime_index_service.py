from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Optional

import httpx


logger = logging.getLogger(__name__)
CN_TZ = timezone(timedelta(hours=8))
LIVE_GRACE_SECONDS = 30.0
MORNING_SESSION = (dt_time(9, 30), dt_time(11, 30))
AFTERNOON_SESSION = (dt_time(13, 0), dt_time(15, 0))
_TENCENT_RE = re.compile(r'v_(sh|sz)(\d{6})="(.*?)";', re.S)
_SINA_RE = re.compile(r'var hq_str_(sh|sz)(\d{6})="(.*?)";', re.S)


def is_market_session_open(value: datetime) -> bool:
    local = value.astimezone(CN_TZ)
    return (
        MORNING_SESSION[0] <= local.time() < MORNING_SESSION[1]
        or AFTERNOON_SESSION[0] <= local.time() < AFTERNOON_SESSION[1]
    )


def quote_status(source_time: Optional[datetime], now: datetime) -> str:
    if source_time is None:
        return "unavailable"
    local_source = source_time.astimezone(CN_TZ)
    local_now = now.astimezone(CN_TZ)
    if local_source.date() != local_now.date() or not is_market_session_open(local_now):
        return "closed"
    if not is_market_session_open(local_source):
        return "stale"
    age = (local_now - local_source).total_seconds()
    return "live" if age <= LIVE_GRACE_SECONDS else "stale"


@dataclass(frozen=True)
class IndexDefinition:
    symbol: str
    code: str
    name: str
    market: str
    provider_symbol: str


INDEX_DEFINITIONS: tuple[IndexDefinition, ...] = (
    IndexDefinition("000001.SH", "000001", "上证指数", "SH", "sh000001"),
    IndexDefinition("399001.SZ", "399001", "深证成指", "SZ", "sz399001"),
    IndexDefinition("399006.SZ", "399006", "创业板指", "SZ", "sz399006"),
    IndexDefinition("000688.SH", "000688", "科创50", "SH", "sh000688"),
    IndexDefinition("000300.SH", "000300", "沪深300", "SH", "sh000300"),
)


@dataclass(frozen=True)
class IndexQuote:
    code: str
    provider: str
    price: float
    previous_close: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    open_price: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[float]
    amount: Optional[float]
    market_data_time: Optional[datetime]
    received_at: datetime


class RealtimeIndexUnavailable(RuntimeError):
    """No upstream index snapshot is available and there is no memory cache."""


def _optional_float(fields: list[str], index: int) -> Optional[float]:
    try:
        return float(fields[index])
    except (IndexError, TypeError, ValueError):
        return None


def _parse_tencent_time(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    except (TypeError, ValueError):
        return None


def _parse_sina_time(day: str, clock: str) -> Optional[datetime]:
    try:
        return datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CN_TZ
        )
    except (TypeError, ValueError):
        return None


class _IndexProvider:
    name: str
    endpoint: str
    referer: str

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/plain,*/*",
                "Referer": self.referer,
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, definitions: tuple[IndexDefinition, ...]) -> tuple[IndexQuote, ...]:
        raise NotImplementedError


class TencentIndexProvider(_IndexProvider):
    name = "TENCENT"
    endpoint = "https://qt.gtimg.cn/q="
    referer = "https://finance.qq.com/"

    async def fetch(self, definitions: tuple[IndexDefinition, ...]) -> tuple[IndexQuote, ...]:
        response = await self.client.get(
            self.endpoint + ",".join(item.provider_symbol for item in definitions)
        )
        response.raise_for_status()
        received_at = datetime.now(CN_TZ)
        requested = {item.provider_symbol: item for item in definitions}
        quotes: dict[str, IndexQuote] = {}
        for match in _TENCENT_RE.finditer(response.text):
            prefix, code, payload = match.groups()
            provider_symbol = f"{prefix}{code}"
            definition = requested.get(provider_symbol)
            fields = payload.split("~")
            if definition is None or len(fields) < 38:
                continue
            price = _optional_float(fields, 3)
            if price is None:
                continue
            amount: Optional[float] = None
            try:
                amount = float(fields[35].split("/")[2])
            except (IndexError, TypeError, ValueError):
                pass
            quotes[provider_symbol] = IndexQuote(
                code=definition.code,
                provider=self.name,
                price=price,
                previous_close=_optional_float(fields, 4),
                change=_optional_float(fields, 31),
                change_pct=_optional_float(fields, 32),
                open_price=_optional_float(fields, 5),
                high=_optional_float(fields, 33),
                low=_optional_float(fields, 34),
                volume=_optional_float(fields, 6),
                amount=amount,
                market_data_time=_parse_tencent_time(fields[30]),
                received_at=received_at,
            )
        if len(quotes) != len(definitions):
            raise RealtimeIndexUnavailable(
                f"{self.name} returned {len(quotes)}/{len(definitions)} index quotes"
            )
        return tuple(quotes[item.provider_symbol] for item in definitions)


class SinaIndexProvider(_IndexProvider):
    name = "SINA"
    endpoint = "https://hq.sinajs.cn/list="
    referer = "https://finance.sina.com.cn/"

    async def fetch(self, definitions: tuple[IndexDefinition, ...]) -> tuple[IndexQuote, ...]:
        response = await self.client.get(
            self.endpoint + ",".join(item.provider_symbol for item in definitions)
        )
        response.raise_for_status()
        received_at = datetime.now(CN_TZ)
        requested = {item.provider_symbol: item for item in definitions}
        quotes: dict[str, IndexQuote] = {}
        for match in _SINA_RE.finditer(response.text):
            prefix, code, payload = match.groups()
            provider_symbol = f"{prefix}{code}"
            definition = requested.get(provider_symbol)
            fields = payload.split(",")
            if definition is None or len(fields) < 32 or not fields[0]:
                continue
            price = _optional_float(fields, 3)
            if price is None:
                continue
            previous_close = _optional_float(fields, 2)
            change = price - previous_close if previous_close is not None else None
            quotes[provider_symbol] = IndexQuote(
                code=definition.code,
                provider=self.name,
                price=price,
                previous_close=previous_close,
                change=change,
                change_pct=(change / previous_close * 100)
                if change is not None and previous_close
                else None,
                open_price=_optional_float(fields, 1),
                high=_optional_float(fields, 4),
                low=_optional_float(fields, 5),
                volume=_optional_float(fields, 8),
                amount=_optional_float(fields, 9),
                market_data_time=_parse_sina_time(fields[30], fields[31]),
                received_at=received_at,
            )
        if len(quotes) != len(definitions):
            raise RealtimeIndexUnavailable(
                f"{self.name} returned {len(quotes)}/{len(definitions)} index quotes"
            )
        return tuple(quotes[item.provider_symbol] for item in definitions)


class RealtimeIndexService:
    """Fetch indices on every open-session request and freeze the close snapshot."""

    def __init__(
        self,
        *,
        primary: Any | None = None,
        backup: Any | None = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.primary = primary or TencentIndexProvider()
        self.backup = backup or SinaIndexProvider()
        self._wall_clock = wall_clock or (lambda: datetime.now(CN_TZ))
        self._refresh_lock = asyncio.Lock()
        self._cached_quotes: tuple[IndexQuote, ...] | None = None
        self._cached_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    async def close(self) -> None:
        await asyncio.gather(self.primary.close(), self.backup.close())

    async def _fetch_quotes(self) -> tuple[IndexQuote, ...]:
        errors: list[str] = []
        for provider in (self.primary, self.backup):
            try:
                return await provider.fetch(INDEX_DEFINITIONS)
            except Exception as exc:
                provider_name = getattr(provider, "name", type(provider).__name__)
                errors.append(f"{provider_name}:{type(exc).__name__}")
                logger.warning(
                    "realtime_index_provider_failed provider=%s error=%s",
                    provider_name,
                    exc,
                )
        raise RealtimeIndexUnavailable("; ".join(errors) or "no quote provider configured")

    @staticmethod
    def _as_number(value: Optional[float]) -> Optional[float]:
        return round(value, 6) if value is not None else None

    def _snapshot(self, now: datetime) -> dict[str, Any]:
        if self._cached_quotes is None or self._cached_at is None:
            raise RealtimeIndexUnavailable("no realtime index snapshot available")
        items: list[dict[str, Any]] = []
        statuses: set[str] = set()
        source_dates: list[str] = []
        for definition, quote in zip(INDEX_DEFINITIONS, self._cached_quotes):
            status = quote_status(quote.market_data_time, now)
            statuses.add(status)
            if quote.market_data_time:
                source_dates.append(quote.market_data_time.astimezone(CN_TZ).date().isoformat())
            items.append(
                {
                    "symbol": definition.symbol,
                    "name": definition.name,
                    "market": definition.market,
                    "price": self._as_number(quote.price),
                    "previous_close": self._as_number(quote.previous_close),
                    "change": self._as_number(quote.change),
                    "change_pct": self._as_number(quote.change_pct),
                    "open": self._as_number(quote.open_price),
                    "high": self._as_number(quote.high),
                    "low": self._as_number(quote.low),
                    "volume": self._as_number(quote.volume),
                    "amount": self._as_number(quote.amount),
                    "source_time": quote.market_data_time.isoformat()
                    if quote.market_data_time
                    else None,
                    "received_at": quote.received_at.isoformat(),
                    "status": status,
                    "provider": quote.provider.lower(),
                }
            )
        if not is_market_session_open(now):
            market_status = "closed"
        elif "live" in statuses:
            market_status = "open"
        else:
            market_status = "stale"
        data: dict[str, Any] = {
            "trading_date": max(source_dates) if source_dates else None,
            "market_status": market_status,
            "updated_at": self._cached_at.isoformat(),
            "cache_age_ms": round(
                max(0.0, (now - self._cached_at).total_seconds()) * 1000,
                3,
            ),
            "items": items,
        }
        if self._last_error:
            data["warning"] = "upstream_unavailable_using_memory_cache"
        return data

    def _has_current_day_cache(self, now: datetime) -> bool:
        return (
            self._cached_quotes is not None
            and self._cached_at is not None
            and self._cached_at.astimezone(CN_TZ).date() == now.date()
        )

    async def fetch_latest(self) -> dict[str, Any]:
        now = self._wall_clock().astimezone(CN_TZ)
        if self._has_current_day_cache(now) and not is_market_session_open(now):
            return self._snapshot(now)

        async with self._refresh_lock:
            now = self._wall_clock().astimezone(CN_TZ)
            if self._has_current_day_cache(now) and not is_market_session_open(now):
                return self._snapshot(now)
            try:
                quotes = await self._fetch_quotes()
            except RealtimeIndexUnavailable:
                self._last_error = "upstream_unavailable"
                if self._cached_quotes is None:
                    raise
                return self._snapshot(now)
            self._cached_quotes = quotes
            self._cached_at = self._wall_clock().astimezone(CN_TZ)
            self._last_error = None
            return self._snapshot(self._cached_at)
