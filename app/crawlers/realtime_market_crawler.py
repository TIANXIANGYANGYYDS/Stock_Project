from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
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
                price = float(fields[3])
                volume = float(fields[6]) * quote_volume_multiplier(code)
                amount = float(fields[35].split("/")[2])
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
                    volume=max(0.0, volume),
                    amount=max(0.0, amount),
                    market_data_time=market_data_time,
                    received_at=received_at,
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
                    volume=max(0.0, volume),
                    amount=max(0.0, amount),
                    market_data_time=market_data_time,
                    received_at=received_at,
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

    async def close(self) -> None:
        await asyncio.gather(self.primary.close(), self.backup.close())

    async def fetch_quotes(self, codes: list[str]) -> tuple[list[RealtimeQuote], dict[str, int | float]]:
        normalized = list(dict.fromkeys(str(code).zfill(6) for code in codes))
        quotes: list[RealtimeQuote] = []
        requests = 0
        fallback_batches = 0
        failed_batches = 0
        started = time.perf_counter()
        for offset in range(0, len(normalized), self.batch_size):
            batch = normalized[offset : offset + self.batch_size]
            result = await self.primary.fetch_batch(batch)
            requests += 1
            if result.complete:
                quotes.extend(result.quotes)
                continue
            fallback = await self.backup.fetch_batch(batch)
            requests += 1
            fallback_batches += 1
            if fallback.complete:
                quotes.extend(fallback.quotes)
            else:
                failed_batches += 1
        return quotes, {
            "requested": len(normalized),
            "returned": len(quotes),
            "requests": requests,
            "fallback_batches": fallback_batches,
            "failed_batches": failed_batches,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
