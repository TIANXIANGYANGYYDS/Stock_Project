from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from curl_cffi import requests as curl_requests
import pandas as pd

from app.crawlers.proxy_provider import AsyncDailiProxyPool
from app.crawlers.eastmoney_reverse_core import (
    Kline,
    calculate_chip,
    calculate_indicators,
)


KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_UT = "7eea3edcaed734bea9cbfc24409ed989"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


class ReverseNetworkError(RuntimeError):
    """The target request failed before a valid K-line payload was received."""


class ReverseDataError(RuntimeError):
    """The target returned a valid response that cannot satisfy this stock."""


class ProxyBudgetExhausted(RuntimeError):
    """The run reached its candidate-IP or per-stock attempt budget."""


@dataclass(frozen=True)
class FetchResult:
    rows: List[Kline]
    request_seconds: float
    response_bytes: int


@dataclass(frozen=True)
class ManagedFetchResult:
    response: FetchResult
    attempts: int
    proxy_id: str


@dataclass
class ManagedProxyStats:
    candidate_count: int = 0
    qualified_count: int = 0
    rejected_candidate_count: int = 0
    proactive_rotation_count: int = 0
    healthy_proxy_failure_count: int = 0
    connection_recovery_count: int = 0
    target_request_count: int = 0
    business_success_count: int = 0
    stock_retry_count: int = 0
    circuit_breaker_trip_count: int = 0
    circuit_breaker_sleep_seconds: float = 0.0
    per_proxy_successes: Dict[str, int] = field(default_factory=dict)


@dataclass
class _ProxyState:
    session: Any
    proxy_id: str
    target_requests: int = 0
    last_request_started: float = 0.0
    business_successes: int = 0


class EastMoneyReverseTransport:
    """Fetch one page-equivalent K-line payload with a browser TLS fingerprint."""

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _secid(code: str) -> str:
        normalized = str(code).strip().zfill(6)
        return f"{1 if normalized.startswith('6') else 0}.{normalized}"

    @staticmethod
    def _referer(code: str) -> str:
        normalized = str(code).strip().zfill(6)
        if normalized.startswith(("4", "8", "9")):
            market = "bj"
        elif normalized.startswith("6"):
            market = "sh"
        else:
            market = "sz"
        return (
            f"https://quote.eastmoney.com/concept/{market}{normalized}.html"
            "#chart-k-cyq"
        )

    @staticmethod
    def _fqt(adjust: str) -> str:
        try:
            return {"": "0", "qfq": "1", "hfq": "2"}[adjust]
        except KeyError as exc:
            raise ValueError(f"unsupported adjust value: {adjust!r}") from exc

    async def fetch_once(
        self,
        session: Any,
        proxy_url: str,
        code: str,
        target_trade_date: str,
        adjust: str = "qfq",
    ) -> FetchResult:
        params = {
            "secid": self._secid(code),
            "ut": KLINE_UT,
            "fields1": FIELDS1,
            "fields2": FIELDS2,
            "klt": "101",
            "fqt": self._fqt(adjust),
            "end": target_trade_date.replace("-", ""),
            "lmt": "210",
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "Referer": self._referer(code),
            "Accept": "application/json,text/plain,*/*",
        }
        started = time.monotonic()
        try:
            response = await session.get(
                KLINE_URL,
                params=params,
                headers=headers,
                proxy=proxy_url,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise ReverseNetworkError(str(exc) or type(exc).__name__) from exc
        request_seconds = time.monotonic() - started
        if response.status_code >= 400:
            raise ReverseNetworkError(
                f"HTTP {response.status_code}, bytes={len(response.content)}"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise ReverseNetworkError(
                f"invalid JSON, bytes={len(response.content)}"
            ) from exc
        if not isinstance(payload, dict):
            raise ReverseNetworkError("response root is not an object")
        if payload.get("rc") not in (None, 0):
            raise ReverseNetworkError(f"target rc={payload.get('rc')}")
        data = payload.get("data")
        lines = data.get("klines") if isinstance(data, dict) else None
        if not isinstance(lines, list) or not lines:
            raise ReverseDataError(f"no K-lines for code={code}")
        try:
            rows = [Kline.from_csv(str(line)) for line in lines]
        except (TypeError, ValueError) as exc:
            raise ReverseDataError(f"invalid K-line row for code={code}") from exc
        return FetchResult(
            rows=rows,
            request_seconds=request_seconds,
            response_bytes=len(response.content),
        )


class TargetAwareProxyManager:
    """Qualify, pace, reuse and retire proxies against the real target endpoint."""

    def __init__(
        self,
        *,
        pool_size: int = 4,
        request_interval_seconds: float = 1.2,
        max_target_requests_per_proxy: int = 40,
        max_stock_attempts: int = 24,
        max_candidate_count: int = 100,
        circuit_failure_threshold: int = 8,
        circuit_cooldown_seconds: float = 30.0,
        transport: Optional[EastMoneyReverseTransport] = None,
        proxy_pool: Optional[Any] = None,
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if pool_size <= 0:
            raise ValueError("pool_size must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if max_target_requests_per_proxy <= 0:
            raise ValueError("max_target_requests_per_proxy must be positive")
        if max_stock_attempts <= 0 or max_candidate_count <= 0:
            raise ValueError("attempt and candidate budgets must be positive")
        if circuit_failure_threshold <= 0 or circuit_cooldown_seconds < 0:
            raise ValueError("invalid circuit breaker settings")

        self.request_interval_seconds = request_interval_seconds
        self.max_target_requests_per_proxy = max_target_requests_per_proxy
        self.max_stock_attempts = max_stock_attempts
        self.max_candidate_count = max_candidate_count
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.transport = transport or EastMoneyReverseTransport()
        self.proxy_pool = proxy_pool or AsyncDailiProxyPool(
            minutes=3,
            pool_size=pool_size,
            max_concurrency_per_proxy=1,
        )
        self.session_factory = session_factory or (
            lambda: curl_requests.AsyncSession(impersonate="chrome124")
        )
        self.stats = ManagedProxyStats()
        self._states: Dict[str, _ProxyState] = {}
        self._consecutive_candidate_failures = 0
        self._cooldown_until = 0.0
        self._state_lock_instance: Optional[asyncio.Lock] = None

    @property
    def _state_lock(self) -> asyncio.Lock:
        if self._state_lock_instance is None:
            self._state_lock_instance = asyncio.Lock()
        return self._state_lock_instance

    @staticmethod
    def _proxy_url(proxies: Dict[str, str]) -> str:
        value = proxies.get("https") or proxies.get("http")
        if not value:
            raise ReverseNetworkError("proxy mapping has no HTTP/HTTPS endpoint")
        return str(value)

    @staticmethod
    def _proxy_id(proxy_url: str) -> str:
        return hashlib.sha256(proxy_url.encode()).hexdigest()[:12]

    @staticmethod
    async def _close_session(session: Any) -> None:
        result = session.close()
        if inspect.isawaitable(result):
            await result

    async def _wait_for_circuit(self) -> None:
        delay = self._cooldown_until - time.monotonic()
        if delay <= 0:
            return
        self.stats.circuit_breaker_sleep_seconds += delay
        await asyncio.sleep(delay)

    async def _record_candidate_failure(self) -> None:
        async with self._state_lock:
            self.stats.rejected_candidate_count += 1
            self._consecutive_candidate_failures += 1
            if self._consecutive_candidate_failures < self.circuit_failure_threshold:
                return
            self._consecutive_candidate_failures = 0
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + self.circuit_cooldown_seconds,
            )
            self.stats.circuit_breaker_trip_count += 1

    async def _record_candidate_success(self) -> None:
        async with self._state_lock:
            self._consecutive_candidate_failures = 0
            self.stats.qualified_count += 1

    async def _new_state(self, proxy_url: str) -> _ProxyState:
        async with self._state_lock:
            if self.stats.candidate_count >= self.max_candidate_count:
                raise ProxyBudgetExhausted(
                    f"candidate IP budget exhausted: {self.max_candidate_count}"
                )
            self.stats.candidate_count += 1
        state = _ProxyState(
            session=self.session_factory(),
            proxy_id=self._proxy_id(proxy_url),
        )
        self._states[proxy_url] = state
        return state

    async def _drop_proxy(
        self,
        proxies: Dict[str, str],
        proxy_url: str,
        state: _ProxyState,
        exc: Exception,
    ) -> None:
        self._states.pop(proxy_url, None)
        await self._close_session(state.session)
        await self.proxy_pool.on_failure_for(proxies, exc)

    async def _retire_quota_proxy(
        self,
        proxies: Dict[str, str],
        proxy_url: str,
        state: _ProxyState,
    ) -> None:
        self.stats.proactive_rotation_count += 1
        await self._drop_proxy(
            proxies,
            proxy_url,
            state,
            RuntimeError("target request quota reached"),
        )

    async def _request(
        self,
        state: _ProxyState,
        proxy_url: str,
        code: str,
        target_trade_date: str,
        adjust: str,
    ) -> FetchResult:
        delay = (
            state.last_request_started
            + self.request_interval_seconds
            - time.monotonic()
        )
        if delay > 0:
            await asyncio.sleep(delay)
        state.last_request_started = time.monotonic()
        state.target_requests += 1
        self.stats.target_request_count += 1
        return await self.transport.fetch_once(
            state.session,
            proxy_url,
            code,
            target_trade_date,
            adjust,
        )

    async def fetch(
        self,
        code: str,
        target_trade_date: str,
        adjust: str = "qfq",
    ) -> ManagedFetchResult:
        attempts = 0
        while attempts < self.max_stock_attempts:
            await self._wait_for_circuit()
            proxies = await self.proxy_pool.get_requests_proxies()
            if not proxies:
                attempts += 1
                self.stats.stock_retry_count += 1
                await asyncio.sleep(1)
                continue
            proxy_url = self._proxy_url(proxies)
            state = self._states.get(proxy_url)
            is_candidate = state is None
            if is_candidate:
                try:
                    state = await self._new_state(proxy_url)
                except ProxyBudgetExhausted as exc:
                    await self.proxy_pool.on_failure_for(proxies, exc)
                    raise
            elif state.target_requests >= self.max_target_requests_per_proxy:
                await self._retire_quota_proxy(proxies, proxy_url, state)
                continue

            attempts += 1
            try:
                result = await self._request(
                    state,
                    proxy_url,
                    code,
                    target_trade_date,
                    adjust,
                )
            except ReverseDataError:
                await self.proxy_pool.on_success_for(proxies)
                raise
            except Exception as first_error:
                if is_candidate:
                    await self._record_candidate_failure()
                    await self._drop_proxy(
                        proxies,
                        proxy_url,
                        state,
                        first_error,
                    )
                    self.stats.stock_retry_count += 1
                    continue

                self.stats.healthy_proxy_failure_count += 1
                await self._close_session(state.session)
                state.session = self.session_factory()
                if attempts >= self.max_stock_attempts:
                    await self._drop_proxy(
                        proxies,
                        proxy_url,
                        state,
                        first_error,
                    )
                    break
                attempts += 1
                try:
                    result = await self._request(
                        state,
                        proxy_url,
                        code,
                        target_trade_date,
                        adjust,
                    )
                except ReverseDataError:
                    await self.proxy_pool.on_success_for(proxies)
                    raise
                except Exception as second_error:
                    await self._drop_proxy(
                        proxies,
                        proxy_url,
                        state,
                        second_error,
                    )
                    self.stats.stock_retry_count += 1
                    continue
                self.stats.connection_recovery_count += 1

            if is_candidate:
                await self._record_candidate_success()
            state.business_successes += 1
            self.stats.business_success_count += 1
            self.stats.per_proxy_successes[state.proxy_id] = state.business_successes
            await self.proxy_pool.on_success_for(proxies)
            return ManagedFetchResult(
                response=result,
                attempts=attempts,
                proxy_id=state.proxy_id,
            )

        raise ProxyBudgetExhausted(
            f"stock attempt budget exhausted: code={code}, attempts={attempts}"
        )

    def report_stats(self) -> Dict[str, Any]:
        provider_stats = getattr(self.proxy_pool, "stats", None)
        return {
            "manager": asdict(self.stats),
            "provider": asdict(provider_stats) if provider_stats is not None else {},
        }

    async def close(self) -> None:
        states = list(self._states.values())
        self._states.clear()
        for state in states:
            await self._close_session(state.session)
        await self.proxy_pool.close()


class EastMoneyReverseFetcher:
    """Build production-compatible daily data from the reversed HTTP endpoint."""

    SOURCE = "eastmoney.quote_api.reverse"
    INDICATOR_SOURCE = "eastmoney.quote_page.algorithm_reverse"
    CHIP_SOURCE = "eastmoney.quote_page.algorithm_reverse"
    CHIP_HISTORY_LIMIT = 90

    def __init__(
        self,
        *,
        manager: Optional[TargetAwareProxyManager] = None,
    ) -> None:
        self.manager = manager or TargetAwareProxyManager()
        self._owns_manager = manager is None

    @staticmethod
    def get_daily_page_url(code: str) -> str:
        return EastMoneyReverseTransport._referer(code)

    async def fetch_kline(
        self,
        *,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        try:
            start = datetime.strptime(start_date, "%Y%m%d").strftime("%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("start_date and end_date must use YYYYMMDD") from exc
        if start > end:
            raise ValueError("start_date cannot be after end_date")

        fetched = await self.manager.fetch(code, end, adjust=adjust)
        all_rows = fetched.response.rows
        rows = [row for row in all_rows if start <= row.date <= end]
        if not rows:
            raise ReverseDataError(
                f"no K-lines in requested range: code={code}, start={start}, end={end}"
            )

        dataframe = pd.DataFrame(
            [
                {
                    "日期": row.date,
                    "开盘": row.open,
                    "收盘": row.close,
                    "最高": row.high,
                    "最低": row.low,
                    "成交量": row.volume,
                    "成交额": row.amount,
                    "振幅": row.amplitude_pct,
                    "涨跌幅": row.pct_chg,
                    "涨跌额": row.change_amount,
                    "换手率": row.turnover_pct,
                    "股票代码": str(code).strip().zfill(6),
                }
                for row in rows
            ]
        )
        indicator_rows = calculate_indicators(all_rows)
        chip_rows: Dict[str, Dict[str, Any]] = {}
        first_chip_index = max(0, len(all_rows) - self.CHIP_HISTORY_LIMIT)
        for index in range(first_chip_index, len(all_rows)):
            row = all_rows[index]
            if not start <= row.date <= end:
                continue
            chip = calculate_chip(all_rows, index)
            chip_rows[row.date] = {
                "profit_ratio": chip["profit_ratio"],
                "avg_cost": chip["avg_cost"],
                "cost_90_low": chip["cost_90"]["low"],
                "cost_90_high": chip["cost_90"]["high"],
                "cost_90_concentration": chip["cost_90"]["concentration"],
                "cost_70_low": chip["cost_70"]["low"],
                "cost_70_high": chip["cost_70"]["high"],
                "cost_70_concentration": chip["cost_70"]["concentration"],
                "chart_x": chip["chart"]["x"],
                "chart_y": chip["chart"]["y"],
            }
        dataframe.attrs.update(
            {
                "source": self.SOURCE,
                "page_url": self.get_daily_page_url(code),
                "network": "proxy",
                "indicator_source": self.INDICATOR_SOURCE,
                "chip_source": self.CHIP_SOURCE,
                "indicator_rows": indicator_rows,
                "chip_rows": chip_rows,
                "reverse_attempts": fetched.attempts,
                "proxy_id": fetched.proxy_id,
            }
        )
        return dataframe

    async def close(self) -> None:
        if self._owns_manager:
            await self.manager.close()
