from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx


CN_TZ = timezone(timedelta(hours=8))
_TENCENT_RE = re.compile(r'v_(sh|sz|bj)(\d{6})="(.*?)";', re.S)
_SINA_RE = re.compile(r'var hq_str_(sh|sz|bj)(\d{6})="(.*?)";', re.S)


def market_prefix(code: str) -> str:
    """Map a six-digit A-share code to the public quote prefix."""

    normalized = str(code).strip().zfill(6)
    if normalized.startswith("6"):
        return "sh"
    if normalized.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return "sz"


def quote_volume_multiplier(code: str) -> float:
    """Return the Tencent volume unit multiplier for an A-share code.

    Tencent reports Shanghai STAR-market volume in shares while the other
    public A-share quote rows used here are reported in lots.
    """

    normalized = str(code).strip().zfill(6)
    return 1.0 if normalized.startswith(("688", "689")) else 100.0


@dataclass(frozen=True)
class RealtimeQuote:
    code: str
    name: Optional[str]
    market: str
    provider: str
    price: float
    volume: float
    amount: float
    market_data_time: Optional[datetime]
    received_at: datetime
    previous_close: Optional[float] = None
    open_price: Optional[float] = None
    bids: tuple[tuple[float, float], ...] = ()
    asks: tuple[tuple[float, float], ...] = ()
    # Keep the provider fields as evidence; auction fields are not trade ticks.
    raw_fields: tuple[str, ...] = ()
    server_time: Optional[datetime] = None
    calibrated_received_at: Optional[datetime] = None
    amount_basis: str = 'provider_cumulative'


def quote_phase(value: datetime) -> str:
    clock = value.astimezone(CN_TZ).strftime('%H:%M:%S')
    if '09:15:00' <= clock < '09:20:00':
        return 'opening_auction_cancelable'
    if '09:20:00' <= clock < '09:25:00':
        return 'opening_auction_locked'
    if '09:25:00' <= clock < '09:30:00':
        return 'opening_result'
    if '09:30:00' <= clock < '11:30:00' or '13:00:00' <= clock < '14:57:00':
        return 'continuous'
    if '14:57:00' <= clock < '15:00:00':
        return 'closing_auction'
    if clock in ('11:30:00', '15:00:00'):
        return 'session_close'
    return 'outside_session'


def quote_issue(quote: RealtimeQuote) -> Optional[str]:
    if not all(math.isfinite(v) and v >= 0 for v in (quote.price, quote.volume, quote.amount)):
        return 'invalid_values'
    if quote.market_data_time is None:
        return 'missing_source_time'
    reference = quote.calibrated_received_at or quote.server_time or quote.received_at
    age = (reference - quote.market_data_time).total_seconds()
    if age < -5:
        return 'future_source_time'
    stable_opening_result = quote_phase(reference) == quote_phase(quote.market_data_time) == 'opening_result'
    if age > 30 and quote_phase(reference) != 'outside_session' and not stable_opening_result:
        return 'stale_source_time'
    if quote.market_data_time.date() != reference.astimezone(CN_TZ).date():
        return 'previous_trade_date'
    if quote.price <= 0 and not quote_phase(reference).startswith('opening_auction'):
        return 'no_trade_price'
    return None


def _book(fields, pairs, multiplier=1.0):
    levels = []
    for price_index, volume_index in pairs:
        try:
            price, volume = float(fields[price_index]), float(fields[volume_index]) * multiplier
        except (ValueError, IndexError):
            return ()
        if not all(math.isfinite(v) and v >= 0 for v in (price, volume)):
            return ()
        levels.append((price, volume))
    return tuple(levels)


def _optional_price(fields, index):
    try:
        value = float(fields[index])
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True)
class QuoteBatchResult:
    provider: str
    requested: int
    returned: int
    status_code: Optional[int]
    elapsed_ms: float
    response_bytes: int
    quotes: tuple[RealtimeQuote, ...]
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.status_code == 200 and self.returned == self.requested


class _PublicQuoteProvider:
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
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, codes: Iterable[str]) -> str:
        raise NotImplementedError

    def _parse(
        self,
        text: str,
        received_at: datetime,
        requested_codes: set[str],
    ) -> list[RealtimeQuote]:
        raise NotImplementedError

    async def fetch_batch(self, codes: list[str]) -> QuoteBatchResult:
        started = time.perf_counter()
        status_code: Optional[int] = None
        response_bytes = 0
        error: Optional[str] = None
        quotes: list[RealtimeQuote] = []
        try:
            response = await self.client.get(self._url(codes))
            received_at = datetime.now(CN_TZ)
            status_code = response.status_code
            response_bytes = len(response.content)
            if status_code != 200:
                error = f"http_{status_code}"
            else:
                quotes = self._parse(
                    response.text,
                    received_at,
                    {str(code).zfill(6) for code in codes},
                )
                try:
                    server_time = parsedate_to_datetime(response.headers['date'])
                    if abs((server_time - received_at).total_seconds()) <= 600:
                        quotes = [replace(q, server_time=server_time) for q in quotes]
                except (KeyError, ValueError, TypeError):
                    pass
                if len(quotes) != len(codes):
                    error = "incomplete_batch"
        except Exception as exc:  # network failures are isolated per batch
            error = type(exc).__name__
        return QuoteBatchResult(
            provider=self.name,
            requested=len(codes),
            returned=len(quotes),
            status_code=status_code,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            response_bytes=response_bytes,
            quotes=tuple(quotes),
            error=error,
        )


class TencentQuoteProvider(_PublicQuoteProvider):
    name = "TENCENT"
    endpoint = "https://qt.gtimg.cn/q="
    referer = "https://finance.qq.com/"

    def _url(self, codes: Iterable[str]) -> str:
        return self.endpoint + ",".join(market_prefix(code) + str(code).zfill(6) for code in codes)

    def _parse(
        self,
        text: str,
        received_at: datetime,
        requested_codes: set[str],
    ) -> list[RealtimeQuote]:
        quotes: list[RealtimeQuote] = []
        for match in _TENCENT_RE.finditer(text):
            market_prefix_value, code, payload = match.groups()
            fields = payload.split("~")
            if code not in requested_codes or len(fields) < 38:
                continue
            try:
                price = float(fields[3] or 0)
                previous_close = float(fields[4])
                volume = float(fields[6] or 0) * quote_volume_multiplier(code)
                detail = fields[35].split('/')
                amount = float(detail[2]) if len(detail) >= 3 and detail[2] else float(fields[37] or 0) * 10000
                amount_basis = 'provider_cumulative' if len(detail) >= 3 and detail[2] else 'rounded_ten_thousand_cny'
            except (ValueError, IndexError):
                continue
            market_data_time = _parse_tencent_time(fields[30])
            quotes.append(
                RealtimeQuote(
                    code=code,
                    name=fields[1] or None,
                    market=market_prefix_value.upper(),
                    provider=self.name,
                    price=price,
                    volume=volume,
                    amount=amount,
                    market_data_time=market_data_time,
                    received_at=received_at,
                    previous_close=(
                        previous_close if previous_close > 0 else None
                    ),
                    open_price=_optional_price(fields, 5),
                    # Book quantities are lots, including STAR securities;
                    # Tencent's cumulative volume has a separate unit rule.
                    bids=_book(fields, [(i, i+1) for i in range(9, 19, 2)], 100),
                    asks=_book(fields, [(i, i+1) for i in range(19, 29, 2)], 100),
                    raw_fields=tuple(fields),
                    amount_basis=amount_basis,
                )
            )
        return quotes


class SinaQuoteProvider(_PublicQuoteProvider):
    name = "SINA"
    endpoint = "https://hq.sinajs.cn/list="
    referer = "https://finance.sina.com.cn/"

    def _url(self, codes: Iterable[str]) -> str:
        return self.endpoint + ",".join(market_prefix(code) + str(code).zfill(6) for code in codes)

    def _parse(
        self,
        text: str,
        received_at: datetime,
        requested_codes: set[str],
    ) -> list[RealtimeQuote]:
        quotes: list[RealtimeQuote] = []
        for match in _SINA_RE.finditer(text):
            market_prefix_value, code, payload = match.groups()
            fields = payload.split(",")
            if code not in requested_codes or len(fields) < 32 or not fields[0]:
                continue
            try:
                price = float(fields[3])
                previous_close = float(fields[2])
                volume = float(fields[8])
                amount = float(fields[9])
            except (ValueError, IndexError):
                continue
            market_data_time = _parse_sina_time(fields[30], fields[31])
            quotes.append(
                RealtimeQuote(
                    code=code,
                    name=fields[0] or None,
                    market=market_prefix_value.upper(),
                    provider=self.name,
                    price=price,
                    volume=volume,
                    amount=amount,
                    market_data_time=market_data_time,
                    received_at=received_at,
                    previous_close=(
                        previous_close if previous_close > 0 else None
                    ),
                    open_price=_optional_price(fields, 1),
                    bids=_book(fields, [(i+1, i) for i in range(10, 20, 2)]),
                    asks=_book(fields, [(i+1, i) for i in range(20, 30, 2)]),
                    raw_fields=tuple(fields),
                )
            )
        return quotes


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


class RealtimeMarketCrawler:
    """Fetch all requested snapshots with Tencent primary and Sina fallback."""

    def __init__(self, *, batch_size: int = 100) -> None:
        self.batch_size = max(1, min(batch_size, 200))
        self.primary = TencentQuoteProvider()
        self.backup = SinaQuoteProvider()
        self.last_observations: list[RealtimeQuote] = []
        self._fallback_after: dict[str, float] = {}
        self.reference_clock_offset: Optional[float] = None

    async def close(self) -> None:
        await asyncio.gather(self.primary.close(), self.backup.close())

    async def clock_offset(self) -> Optional[float]:
        """Use two HTTPS response clocks, never a halted stock's quote clock."""
        async def sample(url):
            try:
                before = datetime.now(CN_TZ)
                response = await self.primary.client.get(url, timeout=5,
                    params={'_clock': str(time.time_ns())}, headers={'Cache-Control': 'no-cache'})
                after = datetime.now(CN_TZ)
                value = parsedate_to_datetime(response.headers['date'])
                offset = (value - (before + (after-before)/2)).total_seconds()
                if response.status_code == 200 and abs(offset) <= 600 and (after-before).total_seconds() < 3:
                    return offset
            except (httpx.HTTPError, KeyError, ValueError, TypeError):
                return None
        offsets = [v for v in await asyncio.gather(sample('https://qt.gtimg.cn/q=sh600519'),
                    sample('https://www.sse.com.cn/')) if v is not None]
        if len(offsets) == 2 and max(offsets) - min(offsets) < 3:
            self.reference_clock_offset = sum(offsets) / 2
            return self.reference_clock_offset
        return None

    async def fetch_universe(self) -> list[dict[str, str]]:
        """Independent paginated SH/SZ/BJ list; includes zero-volume stocks."""
        base = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'
        client = self.backup.client
        response = await client.get(base + 'getHQNodeStockCount', params={'node': 'hs_a'})
        response.raise_for_status()
        total = int(response.json())
        if not 1000 <= total <= 7000:
            raise ValueError('invalid Sina universe count')
        semaphore = asyncio.Semaphore(6)
        async def page(number):
            async with semaphore:
                response = await client.get(base + 'getHQNodeData', params={
                    'node': 'hs_a', 'page': number, 'num': 80, 'sort': 'symbol', 'asc': 1,
                })
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError('invalid Sina universe page')
                return rows
        pages = await asyncio.gather(*(page(n) for n in range(1, (total+79)//80+1)))
        rows = {r['code']: {'code': r['code'], 'name': r['name']} for values in pages for r in values
                if re.fullmatch(r'(sh|sz|bj)\d{6}', r.get('symbol', ''))
                and re.fullmatch(r'\d{6}', r.get('code', '')) and r.get('name')}
        if len(rows) != total:
            raise ValueError(f'incomplete Sina universe: {len(rows)}/{total}')
        return list(rows.values())

    async def fetch_quotes(self, codes: list[str]) -> tuple[list[RealtimeQuote], dict[str, int | float]]:
        normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
        quotes: list[RealtimeQuote] = []
        requests = 0
        fallback_batches = 0
        failed_batches = 0
        started = time.perf_counter()
        observations: list[RealtimeQuote] = []

        def calibrated(result):
            if self.reference_clock_offset is None:
                return result
            return replace(result, quotes=tuple(replace(q, calibrated_received_at=
                q.received_at + timedelta(seconds=self.reference_clock_offset)) for q in result.quotes))

        async def fetch_batch(
            batch: list[str],
        ) -> tuple[tuple[RealtimeQuote, ...], int, int, int]:
            result = calibrated(await self.primary.fetch_batch(batch))
            observations.extend(result.quotes)
            # One suspended/unavailable symbol must not discard the other
            # genuine observations in a batch. Only request missing symbols.
            merged = {quote.code: quote for quote in result.quotes if quote.code in batch}
            missing = [code for code in batch if (code not in merged or quote_issue(merged[code]))
                       and time.monotonic() >= self._fallback_after.get(code, 0)]
            if not missing:
                return tuple(merged[code] for code in batch if code in merged), 1, 0, int(len(merged) < len(batch))
            fallback = calibrated(await self.backup.fetch_batch(missing))
            observations.extend(fallback.quotes)
            for quote in fallback.quotes:
                if quote.code not in missing:
                    continue
                old = merged.get(quote.code)
                if old is None or (quote_issue(old) and not quote_issue(quote)) or (
                    quote_issue(old) and quote.market_data_time is not None and
                    (old.market_data_time is None or quote.market_data_time > old.market_data_time)
                ):
                    merged[quote.code] = quote
            # Do not repeatedly spend requests on a suspended/stale symbol.
            for code in missing:
                if code not in merged or quote_issue(merged[code]):
                    self._fallback_after[code] = time.monotonic() + 30
            return (
                tuple(merged[code] for code in batch if code in merged),
                2, 1, int(len(merged) < len(batch)),
            )

        batches = [
            normalized[offset : offset + self.batch_size]
            for offset in range(0, len(normalized), self.batch_size)
        ]
        for batch_quotes, batch_requests, used_fallback, failed in await asyncio.gather(
            *(fetch_batch(batch) for batch in batches)
        ):
            quotes.extend(batch_quotes)
            requests += batch_requests
            fallback_batches += used_fallback
            failed_batches += failed
        self.last_observations = observations
        return quotes, {
            "requested": len(normalized),
            "returned": len(quotes),
            "requests": requests,
            "fallback_batches": fallback_batches,
            "failed_batches": failed_batches,
            "missing_symbols": len(normalized) - len(quotes),
            "unusable_symbols": sum(quote_issue(q) is not None for q in quotes),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
