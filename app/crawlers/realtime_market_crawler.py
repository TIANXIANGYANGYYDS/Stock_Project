from __future__ import annotations

import asyncio
import math
import json
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
    limit_up_price: Optional[float] = None
    limit_down_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    turnover_pct: Optional[float] = None
    limit_source: Optional[str] = None
    limit_source_time: Optional[datetime] = None
    limit_raw_fields: tuple[str, ...] = ()
    validation_issue: Optional[str] = None


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
    if quote.validation_issue:
        return quote.validation_issue
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
    request_count: int = 1

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
                    limit_up_price=_optional_price(fields, 47),
                    limit_down_price=_optional_price(fields, 48),
                    high_price=_optional_price(fields, 33),
                    low_price=_optional_price(fields, 34),
                    turnover_pct=_optional_price(fields, 38),
                    limit_source=self.name,
                    limit_source_time=market_data_time,
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
                    high_price=_optional_price(fields, 4),
                    low_price=_optional_price(fields, 5),
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


class EastMoneyQuoteProvider(_PublicQuoteProvider):
    """Targeted third source with explicit source time and daily limit prices."""
    name = "EASTMONEY"
    endpoint = "https://push2.eastmoney.com/api/qt/stock/get"
    referer = "https://quote.eastmoney.com/"

    def __init__(self):
        from curl_cffi.requests import AsyncSession
        from app.core.config import get_settings
        from app.crawlers.proxy_provider import AsyncDailiProxyProvider
        self.client = AsyncSession(impersonate="chrome124", trust_env=False,
                                   headers={"Referer": self.referer})
        self.proxy_provider = (AsyncDailiProxyProvider(minutes=3, timeout=2)
                               if get_settings().proxy_51_api_url else None)
        self._limits = {}
        self._limit_cursor = 0
        self._closed = False

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self.client.close()
        if self.proxy_provider:
            await self.proxy_provider.close()

    async def fetch_batch(self, codes: list[str]) -> QuoteBatchResult:
        started = time.perf_counter()
        quotes = {}
        response_bytes = 0
        error = None
        proxies = None
        request_count = 0
        if self._closed:
            return QuoteBatchResult(self.name, len(codes), 0, None, 0, 0, (), "closed", 0)

        async def capture():
            nonlocal response_bytes, proxies, request_count
            if self.proxy_provider:
                proxies = await self.proxy_provider.get_requests_proxies()
                if not proxies:
                    raise RuntimeError("eastmoney_proxy_unavailable")
            request_count += 1
            response = await self.client.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get", params={
                    "secids": ",".join(f"{1 if market_prefix(c) == 'sh' else 0}.{c}" for c in codes),
                    "fields": "f1,f2,f5,f6,f8,f12,f14,f15,f16,f17,f18,f124",
                    "_": str(int(time.time()*1000)),
                }, proxies=proxies, timeout=2)
            response.raise_for_status()
            response_bytes += len(response.content)
            quotes.update((q.code, q) for q in self._parse_batch(response.text, datetime.now(CN_TZ), set(codes)))
            try:
                server_time = parsedate_to_datetime(response.headers['date'])
                for code, q in list(quotes.items()):
                    if abs((server_time-q.received_at).total_seconds()) <= 600:
                        quotes[code] = replace(q, server_time=server_time)
            except (KeyError, ValueError, TypeError):
                pass
            # Batch feed has no daily limit prices. Carry only same-day static
            # limits with identical previous close, otherwise fetch at most two
            # detail records per poll. Never reconstruct limits by percentages.
            need_limits = []
            for code, q in list(quotes.items()):
                cached = self._limits.get(code)
                if (cached and cached.market_data_time.date() == q.market_data_time.date()
                        and cached.previous_close == q.previous_close):
                    quotes[code] = replace(q, limit_up_price=cached.limit_up_price,
                                          limit_down_price=cached.limit_down_price,
                                          limit_source=cached.provider, limit_source_time=cached.market_data_time,
                                          limit_raw_fields=cached.raw_fields)
                else:
                    need_limits.append(code)
            offset = self._limit_cursor % len(need_limits) if need_limits else 0
            detail_codes = (need_limits[offset:] + need_limits[:offset])[:2]
            self._limit_cursor = offset + len(detail_codes)
            for code in detail_codes:
                await asyncio.sleep(0.3)
                request_count += 1
                response = await self.client.get(self.endpoint, params={
                    "secid": f"{1 if market_prefix(code) == 'sh' else 0}.{code}",
                    "fields": "f43,f44,f45,f46,f47,f48,f51,f52,f57,f58,f59,f60,f86,f168",
                    "_": str(int(time.time()*1000)),
                }, proxies=proxies, timeout=1)
                response.raise_for_status()
                response_bytes += len(response.content)
                rows = self._parse(response.text, datetime.now(CN_TZ), {code})
                for detail in rows:
                    if has_daily_limits(detail):
                        self._limits[code] = detail
                        old = quotes[code]
                        if (detail.market_data_time.date() == old.market_data_time.date()
                                and detail.previous_close == old.previous_close):
                            quotes[code] = replace(old, limit_up_price=detail.limit_up_price,
                                                  limit_down_price=detail.limit_down_price,
                                                  limit_source=self.name,
                                                  limit_source_time=detail.market_data_time,
                                                  limit_raw_fields=detail.raw_fields)

        try:
            async with asyncio.timeout(3):
                await capture()
        except Exception as exc:
            error = type(exc).__name__
            if self.proxy_provider and proxies and not quotes:
                self.proxy_provider.on_failure(exc)
        if self.proxy_provider and quotes:
            self.proxy_provider.on_success()
        return QuoteBatchResult(self.name, len(codes), len(quotes), 200 if quotes else None,
                                round((time.perf_counter()-started)*1000, 3), response_bytes,
                                tuple(quotes.values()), error or (None if len(quotes) == len(codes) else "incomplete_batch"),
                                request_count)

    def _parse_batch(self, text, received_at, requested_codes):
        payload = json.loads(text)
        rows = (payload.get("data") or {}).get("diff")
        if payload.get("rc") != 0 or not isinstance(rows, list):
            raise ValueError("eastmoney_invalid_quote_batch")
        result = []
        for raw in rows:
            # Reuse the detail parser's verified field units and timestamp.
            mapped = {target: raw.get(source) for target, source in {
                "f43": "f2", "f44": "f15", "f45": "f16", "f46": "f17",
                "f47": "f5", "f48": "f6", "f57": "f12", "f58": "f14",
                "f59": "f1", "f60": "f18", "f86": "f124", "f168": "f8",
            }.items()}
            parsed = self._parse(json.dumps({"rc": 0, "data": mapped}), received_at, requested_codes)
            result.extend(replace(q, raw_fields=(json.dumps(raw, ensure_ascii=False),),
                                  limit_source=None, limit_source_time=None) for q in parsed)
        return result

    def _parse(self, text, received_at, requested_codes):
        payload = json.loads(text)
        row = payload.get("data")
        if payload.get("rc") != 0 or not isinstance(row, dict) or row.get("f57") not in requested_codes:
            return []
        try:
            code = row["f57"]
            precision = int(row["f59"])
            if not 0 <= precision <= 4:
                return []
            scale = 10 ** precision
            def price(key):
                value = row.get(key)
                return float(value) / scale if value not in (None, "-", "") else None
            source_time = datetime.fromtimestamp(int(row["f86"]), tz=CN_TZ)
            return [RealtimeQuote(
                code=code, name=row.get("f58"), market=market_prefix(code).upper(), provider=self.name,
                price=float(price("f43")), volume=float(row["f47"])*100,
                amount=float(row["f48"]), previous_close=price("f60"), open_price=price("f46"),
                market_data_time=source_time, received_at=received_at,
                high_price=price("f44"), low_price=price("f45"),
                limit_up_price=price("f51"), limit_down_price=price("f52"),
                turnover_pct=float(row["f168"])/100 if row.get("f168") not in (None, "-", "") else None,
                limit_source=self.name, limit_source_time=source_time,
                raw_fields=(json.dumps(row, ensure_ascii=False, separators=(',', ':')),),
            )]
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            return []


def has_daily_limits(quote):
    upper, lower = quote.limit_up_price, quote.limit_down_price
    return bool(upper is not None and lower is not None
                and math.isfinite(upper) and math.isfinite(lower) and 0 < lower < upper < 99999
                and (quote.price == 0 or lower <= quote.price <= upper))


class RealtimeMarketCrawler:
    """Tencent → Sina → bounded EastMoney fallback, retaining raw observations."""

    def __init__(self, *, batch_size: int = 100) -> None:
        self.batch_size = max(1, min(batch_size, 200))
        self.primary = TencentQuoteProvider()
        self.backup = SinaQuoteProvider()
        self.third = EastMoneyQuoteProvider()
        self.last_observations: list[RealtimeQuote] = []
        self._fallback_after: dict[str, float] = {}
        self.reference_clock_offset: Optional[float] = None
        self._last_source_times: dict[str, datetime] = {}
        self._daily_limits: dict[str, RealtimeQuote] = {}
        self._third_cursor = 0

    async def close(self) -> None:
        await asyncio.gather(self.primary.close(), self.backup.close(), self.third.close())

    def _prepare_quote(self, q):
        if self.reference_clock_offset is not None:
            q = replace(q, calibrated_received_at=q.received_at + timedelta(seconds=self.reference_clock_offset))
        reference = q.calibrated_received_at or q.server_time or q.received_at
        source = q.market_data_time
        if source and source.astimezone(CN_TZ).date() == reference.astimezone(CN_TZ).date():
            cached = self._daily_limits.get(q.code)
            if has_daily_limits(q) and q.previous_close and q.previous_close > 0:
                # Preserve the original static-field provenance if this quote
                # itself was enriched from another source earlier in the poll.
                if q.limit_source in (None, q.provider):
                    self._daily_limits[q.code] = q
            elif (cached and cached.market_data_time.date() == source.date()
                  and cached.previous_close == q.previous_close and q.previous_close
                  and has_daily_limits(replace(q, limit_up_price=cached.limit_up_price,
                                              limit_down_price=cached.limit_down_price))):
                # Only static daily limits may cross providers. No live prices,
                # times, book quantities or volume are carried from an old tick.
                q = replace(q, limit_up_price=cached.limit_up_price, limit_down_price=cached.limit_down_price,
                            limit_source=cached.limit_source or cached.provider,
                            limit_source_time=cached.limit_source_time or cached.market_data_time,
                            limit_raw_fields=cached.limit_raw_fields or cached.raw_fields)
        previous = self._last_source_times.get(q.code)
        if source and previous and source < previous:
            q = replace(q, validation_issue="out_of_order")
        return q

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
            return replace(result, quotes=tuple(self._prepare_quote(q) for q in result.quotes))

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
        merged = {q.code: q for q in quotes}
        unresolved = [code for code in normalized if (code not in merged or quote_issue(merged[code])
                      or merged[code].provider == "SINA" and not has_daily_limits(merged[code]))
                      and time.monotonic() >= self._fallback_after.get(code, 0)]
        # One batch request plus at most two detail requests, under a 3 s
        # deadline. Rotate deferred symbols through the per-symbol cooldown.
        if unresolved:
            offset = self._third_cursor % len(unresolved)
            third_codes = (unresolved[offset:] + unresolved[:offset])[:200]
            self._third_cursor = (offset + len(third_codes)) % len(unresolved)
        else:
            third_codes = []
        if third_codes:
            third = calibrated(await self.third.fetch_batch(third_codes))
            observations.extend(third.quotes)
            requests += third.request_count
            for q in third.quotes:
                if q.code not in third_codes:
                    continue
                old = merged.get(q.code)
                if old is None or (quote_issue(old) and not quote_issue(q)) or (
                    old and not quote_issue(q) and not has_daily_limits(old) and has_daily_limits(q)
                    and q.market_data_time and old.market_data_time and q.market_data_time >= old.market_data_time
                ):
                    merged[q.code] = q
        for code in unresolved:
            # Defer only symbols actually tried at all available sources.
            if code in third_codes and (code not in merged or quote_issue(merged[code])):
                self._fallback_after[code] = time.monotonic() + 30
        # Newly verified daily limits can also complete this cycle's Sina
        # observations, so archive consumers and minute consumers agree.
        observations = [self._prepare_quote(q) for q in observations]
        quotes = [self._prepare_quote(merged[code]) for code in normalized if code in merged]
        failed_batches = sum(any(code not in merged for code in batch) for batch in batches)
        for q in quotes:
            if not quote_issue(q) and q.market_data_time:
                self._last_source_times[q.code] = q.market_data_time
        self.last_observations = observations
        return quotes, {
            "requested": len(normalized),
            "returned": len(quotes),
            "requests": requests,
            "fallback_batches": fallback_batches,
            "third_source_requested": len(third_codes),
            "third_source_returned": len(third.quotes) if third_codes else 0,
            "third_source_error": third.error if third_codes else None,
            "third_source_deferred": max(0, len(unresolved)-len(third_codes)),
            "failed_batches": failed_batches,
            "missing_symbols": len(normalized) - len(quotes),
            "unusable_symbols": sum(quote_issue(q) is not None for q in quotes),
            "usable_with_daily_limits": sum(quote_issue(q) is None and has_daily_limits(q) for q in quotes),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
