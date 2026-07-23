from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import requests
from app.core.config import Settings


logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class ProxyUnavailableError(RuntimeError):
    """代理不可用异常。"""


class ProxyProvider(Protocol):
    """
    网络出口提供器接口。

    返回值格式兼容 requests:
    {
        "http": "http://host:port",
        "https": "http://host:port",
    }

    不可用时返回 None。
    """

    def get_requests_proxies(self) -> Optional[Dict[str, str]]: ...

    def on_success(self) -> None: ...

    def on_failure(self, exc: Exception) -> None: ...


class AsyncProxyProvider(Protocol):
    async def get_requests_proxies(self) -> Optional[Dict[str, str]]: ...

    def on_success(self) -> None: ...

    def on_failure(self, exc: Exception) -> None: ...

    async def close(self) -> None: ...


class AsyncRequestRateLimiter:
    def __init__(self, max_calls_per_second: float = 10.0) -> None:
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second 必须大于 0")
        self._interval_seconds = 1.0 / max_calls_per_second
        self._lock: Optional[asyncio.Lock] = None
        self._next_allowed_at = 0.0

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            delay = self._next_allowed_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed_at = time.monotonic() + self._interval_seconds


class RequestRateLimiter:
    def __init__(self, max_calls_per_second: float = 10.0) -> None:
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second 必须大于 0")
        self._interval_seconds = 1.0 / max_calls_per_second
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            delay = self._next_allowed_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_allowed_at = time.monotonic() + self._interval_seconds


class NoProxyProvider:
    """
    默认实现：不使用代理。
    """

    def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        return None

    def on_success(self) -> None:
        pass

    def on_failure(self, exc: Exception) -> None:
        pass


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int

    def display(self) -> str:
        return f"{self.host}:{self.port}"


class DailiProxyProvider:
    """51代理同步提供器；API 模板中仅 qty 参数会被覆盖。"""

    IP_TTL_MINUTES = 3
    _api_rate_limiter = RequestRateLimiter(max_calls_per_second=10.0)

    def __init__(
        self,
        minutes: int = IP_TTL_MINUTES,
        *,
        count: int = 1,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
    ) -> None:
        if not isinstance(minutes, int):
            raise TypeError("minutes 必须是 int 类型")

        if minutes != self.IP_TTL_MINUTES:
            raise ValueError("51代理 IP 固定有效3分钟，minutes 必须为 3")
        if not isinstance(count, int):
            raise TypeError("count 必须是 int 类型")
        if not 1 <= count <= 200:
            raise ValueError("count 必须在 1 到 200 之间")

        api_url = Settings().proxy_51_api_url.strip()
        if not api_url:
            raise ValueError("未配置51代理 API，请设置 PROXY_51_API_URL")

        self.minutes = minutes
        self.count = count
        self.api_url = api_url
        self.timeout = timeout
        self.refresh_before_seconds = refresh_before_seconds

        self.current_endpoint: Optional[ProxyEndpoint] = None
        self.current_proxies: Optional[Dict[str, str]] = None
        self.current_expire_at: Optional[float] = None
        self.last_endpoint: Optional[ProxyEndpoint] = None

    def _build_api_url(self, *, count: Optional[int] = None) -> str:
        """
        保留配置 URL 的全部参数，仅覆盖 qty。
        """
        request_count = self.count if count is None else count
        if not 1 <= request_count <= 200:
            raise ValueError("count 必须在 1 到 200 之间")
        parts = urlsplit(self.api_url)
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        params["qty"] = str(request_count)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment)
        )

    def _extract_endpoints_from_json(self, data: Any) -> List[ProxyEndpoint]:
        """
        解析51代理 timeip/getip JSON 返回。
        """
        if not isinstance(data, dict):
            return []

        if data.get("code") != 0:
            print(f"[代理池] 51代理接口返回失败: {data.get('msg', data)}")
            return []

        items = data.get("data")
        if not isinstance(items, list) or not items:
            print(f"[代理池] JSON 中没有可用 list: {data}")
            return []

        endpoints: List[ProxyEndpoint] = []
        seen: set[tuple[str, int]] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                print(f"[代理池] list[{index}] 格式异常: {item}")
                continue

            host = item.get("ip")
            port = item.get("port") or item.get("Port")
            if not host or not port:
                print(f"[代理池] data[{index}] 中未找到 ip/port: {item}")
                continue

            try:
                endpoint = ProxyEndpoint(
                    host=str(host).strip(),
                    port=int(str(port).strip()),
                )
            except ValueError:
                print(f"[代理池] port 不是合法整数: {port}")
                continue

            identity = (endpoint.host, endpoint.port)
            if identity in seen:
                continue
            seen.add(identity)
            endpoints.append(endpoint)
        return endpoints

    def _extract_endpoint_from_json(self, data: Any) -> Optional[ProxyEndpoint]:
        endpoints = self._extract_endpoints_from_json(data)
        return endpoints[0] if endpoints else None

    def _fetch_proxy_endpoint(self) -> Optional[ProxyEndpoint]:
        api_url = self._build_api_url()

        try:
            self._api_rate_limiter.acquire()
            resp = requests.get(api_url, timeout=self.timeout)
            resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError:
                print(
                    f"[代理池] 接口没有返回合法 JSON，响应前 300 字符: {resp.text[:300]}"
                )
                self.last_endpoint = None
                return None

            endpoint = self._extract_endpoint_from_json(data)
            self.last_endpoint = endpoint

            if endpoint is None:
                print(f"[代理池] 未解析出可用代理，原始 JSON: {data}")
                return None

            print(f"[代理池] 获取到新代理: {endpoint.display()}")
            return endpoint

        except Exception as e:
            print(f"[代理池] 获取代理失败: {type(e).__name__}")
            self.last_endpoint = None
            return None

    def _build_proxies_from_endpoint(self, endpoint: ProxyEndpoint) -> Dict[str, str]:
        """
        requests 代理格式。

        注意：
        - 这里使用 HTTP 代理地址即可。
        - HTTPS 请求也可以通过 http://host:port 代理转发。
        - requests 官方代理参数就是 proxies={'http': ..., 'https': ...}。
        """
        proxy_url = f"http://{endpoint.host}:{endpoint.port}"
        return {
            "http": proxy_url,
            "https": proxy_url,
        }

    def _clear_current_proxy(self) -> None:
        self.current_endpoint = None
        self.current_proxies = None
        self.current_expire_at = None

    def _set_current_proxy(
        self, endpoint: ProxyEndpoint, proxies: Dict[str, str]
    ) -> None:
        """
        设置当前代理，并按 minutes 计算本地过期时间。

        为了避免代理刚好过期导致请求失败，这里会提前 refresh_before_seconds 秒刷新。
        """
        self.current_endpoint = endpoint
        self.current_proxies = proxies

        ttl_seconds = self.minutes * 60
        usable_ttl_seconds = max(1, ttl_seconds - self.refresh_before_seconds)

        self.current_expire_at = time.monotonic() + usable_ttl_seconds

    def _is_current_proxy_valid(self) -> bool:
        if self.current_endpoint is None:
            return False

        if self.current_proxies is None:
            return False

        if self.current_expire_at is None:
            return False

        if time.monotonic() >= self.current_expire_at:
            print(
                f"[代理池] 当前代理已过期或接近过期: {self.current_endpoint.display()}"
            )
            return False

        return True

    def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        """
        获取 requests 可用的 proxies。

        - 有可用当前代理：复用
        - 没有可用当前代理：从51代理 API 提取新的
        - 提取失败：返回 None
        """
        if self._is_current_proxy_valid():
            assert self.current_endpoint is not None
            assert self.current_proxies is not None

            print(f"[代理池] 继续复用当前代理: {self.current_endpoint.display()}")
            return self.current_proxies

        endpoint = self._fetch_proxy_endpoint()
        if endpoint is None:
            print("[代理池] 当前没有拿到可用代理，本次返回 None")
            self._clear_current_proxy()
            return None

        proxies = self._build_proxies_from_endpoint(endpoint)
        self._set_current_proxy(endpoint, proxies)

        print(f"[代理池] 切换为新代理: {endpoint.display()}")
        print(f"[代理池] 本次 requests 使用代理: {proxies}")

        return proxies

    def on_success(self) -> None:
        if self.current_endpoint:
            print(
                f"[代理池] 当前代理请求成功，继续复用: {self.current_endpoint.display()}"
            )

    def on_failure(self, exc: Exception) -> None:
        if self.current_endpoint:
            print(
                f"[代理池] 当前代理请求失败，准备弃用: "
                f"{self.current_endpoint.display()} | {repr(exc)}"
            )
        else:
            print(f"[代理池] 请求失败，但当前未记录代理: {repr(exc)}")

        self._clear_current_proxy()


class AsyncDailiProxyProvider(DailiProxyProvider):
    """Async proxy provider used by coroutine-based page crawlers."""

    def __init__(
        self,
        minutes: int,
        *,
        count: int = 1,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
        rate_limiter: Optional[AsyncRequestRateLimiter] = None,
    ) -> None:
        super().__init__(
            minutes,
            count=count,
            timeout=timeout,
            refresh_before_seconds=refresh_before_seconds,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
        )
        self._lock: Optional[asyncio.Lock] = None
        self._rate_limiter = rate_limiter or AsyncRequestRateLimiter()

    async def _fetch_proxy_endpoint_async(self) -> Optional[ProxyEndpoint]:
        api_url = self._build_api_url()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._api_rate_limiter.acquire)
            await self._rate_limiter.acquire()
            response = await self._client.get(api_url)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("proxy_endpoint_fetch_failed error=%s", type(exc).__name__)
            self.last_endpoint = None
            return None

        endpoint = self._extract_endpoint_from_json(data)
        self.last_endpoint = endpoint
        if endpoint is None:
            logger.warning("proxy_endpoint_unavailable response=%s", data)
            return None
        return endpoint

    async def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._is_current_proxy_valid():
                assert self.current_endpoint is not None
                assert self.current_proxies is not None
                logger.debug("proxy_reused endpoint=%s", self.current_endpoint.display())
                return self.current_proxies

            endpoint = await self._fetch_proxy_endpoint_async()
            if endpoint is None:
                self._clear_current_proxy()
                return None

            proxies = self._build_proxies_from_endpoint(endpoint)
            self._set_current_proxy(endpoint, proxies)
            logger.info("proxy_acquired endpoint=%s", endpoint.display())
            return proxies

    def on_success_for(self, proxies: Optional[Dict[str, str]]) -> None:
        if proxies is None or self.current_proxies != proxies:
            return
        logger.debug("proxy_request_succeeded endpoint=%s", self.current_endpoint)

    def on_failure_for(
        self,
        proxies: Optional[Dict[str, str]],
        exc: Exception,
    ) -> None:
        if proxies is None or self.current_proxies != proxies:
            return
        logger.warning(
            "proxy_discarded endpoint=%s error=%s",
            self.current_endpoint.display() if self.current_endpoint else None,
            repr(exc),
        )
        self._clear_current_proxy()

    async def close(self) -> None:
        await self._client.aclose()


@dataclass
class _ProxyPoolSlot:
    endpoint: ProxyEndpoint
    proxies: Dict[str, str]
    expire_at: float
    in_flight: int = 0
    draining: bool = False


@dataclass
class ProxyPoolStats:
    api_request_count: int = 0
    requested_endpoint_count: int = 0
    received_endpoint_count: int = 0
    added_endpoint_count: int = 0
    discarded_endpoint_count: int = 0
    expired_endpoint_count: int = 0
    lease_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    max_in_flight: int = 0


class AsyncDailiProxyPool(DailiProxyProvider):
    """51代理批量 IP 池，每个 IP 的并发数受控。"""

    def __init__(
        self,
        minutes: int,
        *,
        pool_size: int = 4,
        max_concurrency_per_proxy: int = 2,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
        rate_limiter: Optional[AsyncRequestRateLimiter] = None,
    ) -> None:
        if pool_size <= 0 or pool_size > 200:
            raise ValueError("pool_size 必须在 1 到 200 之间")
        if max_concurrency_per_proxy <= 0:
            raise ValueError("max_concurrency_per_proxy 必须大于 0")
        super().__init__(
            minutes,
            count=pool_size,
            timeout=timeout,
            refresh_before_seconds=refresh_before_seconds,
        )
        self.pool_size = pool_size
        self.max_concurrency_per_proxy = max_concurrency_per_proxy
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._condition_instance: Optional[asyncio.Condition] = None
        self._rate_limiter = rate_limiter or AsyncRequestRateLimiter()
        self._slots: List[_ProxyPoolSlot] = []
        self._fetching = False
        self.stats = ProxyPoolStats()

    @property
    def _condition(self) -> asyncio.Condition:
        if self._condition_instance is None:
            self._condition_instance = asyncio.Condition()
        return self._condition_instance

    def _slot_expire_at(self) -> float:
        usable_ttl_seconds = max(
            1,
            self.minutes * 60 - self.refresh_before_seconds,
        )
        return time.monotonic() + usable_ttl_seconds

    def _mark_expired_and_cleanup(self) -> None:
        now = time.monotonic()
        for slot in self._slots:
            if now >= slot.expire_at and not slot.draining:
                slot.draining = True
                self.stats.expired_endpoint_count += 1
        self._slots = [
            slot
            for slot in self._slots
            if not (slot.draining and slot.in_flight == 0)
        ]

    def _select_available_slot(self) -> Optional[_ProxyPoolSlot]:
        self._mark_expired_and_cleanup()
        candidates = [
            slot
            for slot in self._slots
            if not slot.draining
            and slot.in_flight < self.max_concurrency_per_proxy
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda slot: (slot.in_flight, slot.expire_at))

    async def _fetch_proxy_endpoints_async(
        self,
        count: int,
    ) -> List[ProxyEndpoint]:
        self.stats.api_request_count += 1
        self.stats.requested_endpoint_count += count
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._api_rate_limiter.acquire)
            await self._rate_limiter.acquire()
            response = await self._client.get(self._build_api_url(count=count))
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "proxy_pool_fetch_failed count=%s error=%s",
                count,
                type(exc).__name__,
            )
            return []

        endpoints = self._extract_endpoints_from_json(data)
        self.stats.received_endpoint_count += len(endpoints)
        if not endpoints:
            logger.warning("proxy_pool_fetch_empty count=%s response=%s", count, data)
        return endpoints

    async def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        while True:
            fetch_count = 0
            async with self._condition:
                slot = self._select_available_slot()
                if slot is not None:
                    slot.in_flight += 1
                    self.stats.lease_count += 1
                    self.stats.max_in_flight = max(
                        self.stats.max_in_flight,
                        sum(slot.in_flight for slot in self._slots),
                    )
                    return slot.proxies

                active_count = sum(not slot.draining for slot in self._slots)
                if not self._fetching and active_count < self.pool_size:
                    self._fetching = True
                    fetch_count = self.pool_size - active_count
                else:
                    await self._condition.wait()
                    continue

            endpoints = await self._fetch_proxy_endpoints_async(fetch_count)
            async with self._condition:
                existing = {
                    (slot.endpoint.host, slot.endpoint.port) for slot in self._slots
                }
                added = 0
                for endpoint in endpoints:
                    identity = (endpoint.host, endpoint.port)
                    if identity in existing:
                        continue
                    existing.add(identity)
                    self._slots.append(
                        _ProxyPoolSlot(
                            endpoint=endpoint,
                            proxies=self._build_proxies_from_endpoint(endpoint),
                            expire_at=self._slot_expire_at(),
                        )
                    )
                    added += 1
                    self.stats.added_endpoint_count += 1
                self._fetching = False
                self._condition.notify_all()
                if added:
                    logger.info(
                        "proxy_pool_filled requested=%s added=%s active=%s",
                        fetch_count,
                        added,
                        sum(not slot.draining for slot in self._slots),
                    )
                elif self._select_available_slot() is None:
                    return None

    def _find_slot(self, proxies: Dict[str, str]) -> Optional[_ProxyPoolSlot]:
        return next((slot for slot in self._slots if slot.proxies == proxies), None)

    async def on_success_for(
        self,
        proxies: Optional[Dict[str, str]],
    ) -> None:
        if proxies is None:
            return
        async with self._condition:
            slot = self._find_slot(proxies)
            if slot is None:
                return
            slot.in_flight = max(0, slot.in_flight - 1)
            self.stats.success_count += 1
            self._mark_expired_and_cleanup()
            self._condition.notify_all()

    async def on_failure_for(
        self,
        proxies: Optional[Dict[str, str]],
        exc: Exception,
    ) -> None:
        if proxies is None:
            return
        async with self._condition:
            slot = self._find_slot(proxies)
            if slot is None:
                return
            slot.in_flight = max(0, slot.in_flight - 1)
            self.stats.failure_count += 1
            if not slot.draining:
                slot.draining = True
                self.stats.discarded_endpoint_count += 1
                logger.warning(
                    "proxy_pool_discarded endpoint=%s error=%s",
                    slot.endpoint.display(),
                    repr(exc),
                )
            self._mark_expired_and_cleanup()
            self._condition.notify_all()

    async def close(self) -> None:
        await self._client.aclose()


def get_required_proxies(provider: ProxyProvider) -> Dict[str, str]:
    """
    强制获取代理。

    关键点：
    - provider.get_requests_proxies() 返回 None 时直接抛错。
    - 防止 requests 在 proxies=None 时走本机直连，造成“代理测试成功”的误判。
    """
    proxies = provider.get_requests_proxies()
    if proxies is None:
        raise ProxyUnavailableError("未获取到代理，禁止本机直连请求")

    return proxies


async def get_required_async_proxies(
    provider: AsyncProxyProvider,
) -> Dict[str, str]:
    proxies = await provider.get_requests_proxies()
    if proxies is None:
        raise ProxyUnavailableError("未获取到代理，禁止本机直连请求")
    return proxies


def quick_test_proxy(
    provider: ProxyProvider,
    test_url: str = "https://httpbin.org/ip",
) -> None:
    """
    代理连通性测试。

    不允许直连：
    - 如果代理池拿不到代理，直接抛错
    - 不会 fallback 到本机网络
    """
    print("=" * 80)
    print("开始代理连通性测试")

    try:
        proxies = get_required_proxies(provider)
        print(f"测试时使用代理: {proxies}")

        resp = requests.get(
            test_url,
            timeout=15,
            proxies=proxies,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()

        print(f"代理测试状态码: {resp.status_code}")
        print(f"代理测试响应前 300 字符: {resp.text[:300]}")

        provider.on_success()

    except Exception as e:
        provider.on_failure(e)
        print(f"代理测试失败: {repr(e)}")
        raise

    print("=" * 80)


def request_with_proxy(
    provider: ProxyProvider,
    method: str,
    url: str,
    **kwargs: Any,
) -> requests.Response:
    """
    业务请求推荐统一走这个函数。

    优点：
    - 自动获取代理
    - 获取不到代理时禁止直连
    - 成功后继续复用当前代理
    - 失败后清空当前代理，下次自动换新
    """
    try:
        proxies = get_required_proxies(provider)

        kwargs.setdefault("timeout", 20)
        kwargs["proxies"] = proxies

        resp = requests.request(
            method=method,
            url=url,
            **kwargs,
        )
        resp.raise_for_status()

        provider.on_success()
        return resp

    except Exception as e:
        provider.on_failure(e)
        raise


if __name__ == "__main__":
    provider = DailiProxyProvider(minutes=3)

    quick_test_proxy(provider)

    resp = request_with_proxy(
        provider,
        "GET",
        "https://httpbin.org/ip",
        headers={"User-Agent": "Mozilla/5.0"},
    )

    print(resp.text)
