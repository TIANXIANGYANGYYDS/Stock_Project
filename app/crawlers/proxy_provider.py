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
    """调用方要求强制经代理访问、但当前无法取得有效代理时抛出的异常。"""


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

    def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        """返回可传给 requests 的 HTTP/HTTPS 代理映射，暂不可用时返回 ``None``."""
        ...

    def on_success(self) -> None:
        """通知提供器最近一次使用其代理的请求成功，可据此保留当前代理。"""
        ...

    def on_failure(self, exc: Exception) -> None:
        """通知提供器代理请求失败，并交由实现记录或丢弃当前代理。"""
        ...


class AsyncProxyProvider(Protocol):
    """面向协程爬虫的代理提供器契约。

    获取代理和资源关闭可以异步执行；请求成功或失败的通知保持同步，以便调用方
    在同步异常处理分支中也能安全调用。
    """

    async def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        """异步返回可供请求库使用的代理映射，暂不可用时返回 ``None``。"""
        ...

    def on_success(self) -> None:
        """通知实现最近一次代理请求成功。"""
        ...

    def on_failure(self, exc: Exception) -> None:
        """通知实现最近一次代理请求失败及其异常原因。"""
        ...

    async def close(self) -> None:
        """释放实现持有的异步 HTTP 客户端或其他网络资源。"""
        ...


class AsyncRequestRateLimiter:
    """使用事件循环锁串行化请求开始时间的异步速率限制器。"""

    def __init__(self, max_calls_per_second: float = 10.0) -> None:
        """以每秒最大调用数初始化限流间隔，并延迟创建事件循环相关锁。"""
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second 必须大于 0")
        self._interval_seconds = 1.0 / max_calls_per_second  #: 两次允许请求开始之间的最小单调时钟间隔。
        self._lock: Optional[asyncio.Lock] = None  #: 绑定当前事件循环、首次 acquire 时创建的互斥锁。
        self._next_allowed_at = 0.0  #: 下一次允许开始请求的单调时间戳。

    async def acquire(self) -> None:
        """等待至下一个可用时间窗，并为随后一个请求预留新的时间窗。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            delay = self._next_allowed_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed_at = time.monotonic() + self._interval_seconds


class RequestRateLimiter:
    """供同步 requests 调用共享使用的线程安全速率限制器。"""

    def __init__(self, max_calls_per_second: float = 10.0) -> None:
        """以每秒最大调用数初始化同步锁和请求间隔。"""
        if max_calls_per_second <= 0:
            raise ValueError("max_calls_per_second 必须大于 0")
        self._interval_seconds = 1.0 / max_calls_per_second  #: 两次同步请求开始之间的最小间隔秒数。
        self._lock = threading.Lock()  #: 保护下一次请求时间戳的线程互斥锁。
        self._next_allowed_at = 0.0  #: 下一次允许开始同步请求的单调时间戳。

    def acquire(self) -> None:
        """阻塞当前线程直到限流时间窗可用，并更新下一次可用时间。"""
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
        """明确表示该实现从不分配代理，因此始终返回 ``None``。"""
        return None

    def on_success(self) -> None:
        """忽略成功通知，因为该实现没有需要复用或统计的代理状态。"""
        pass

    def on_failure(self, exc: Exception) -> None:
        """忽略失败通知，因为该实现没有需要淘汰的代理状态。"""
        pass


@dataclass(frozen=True)
class ProxyEndpoint:
    """代理服务返回的一组主机与端口，不包含协议和认证信息。"""

    host: str  #: 代理服务器的 IPv4、IPv6 或域名主机部分。
    port: int  #: 代理服务器接受 HTTP CONNECT/转发请求的 TCP 端口。

    def display(self) -> str:
        """返回日志使用的 ``host:port`` 形式，避免重复拼接端点字符串。"""
        return f"{self.host}:{self.port}"


class DailiProxyProvider:
    """管理一个可复用的 51 代理 IP，并按接近过期或失败状态自动更换。

    API 地址从配置读取；请求参数仅动态覆盖 ``qty``，以保留用户配置的认证和
    地区等限制。同步调用方通过 :meth:`get_requests_proxies` 取得 requests 映射。
    """

    #: 51 代理产品声明的固定 IP 有效分钟数，也是允许的唯一 ``minutes`` 参数。
    IP_TTL_MINUTES = 3
    #: 所有同步实例共享的 API 请求限流器，避免并发实例触发供应商频控。
    _api_rate_limiter = RequestRateLimiter(max_calls_per_second=10.0)

    def __init__(
        self,
        minutes: int = IP_TTL_MINUTES,
        *,
        count: int = 1,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
    ) -> None:
        """校验 51 代理配置并初始化当前代理的本地缓存状态。

        ``count`` 允许请求批量端点数但本类只复用第一条；``timeout`` 控制 API
        请求超时，``refresh_before_seconds`` 让缓存 IP 在实际过期前提前失效。
        """
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

        self.minutes = minutes  #: 已校验的供应商 IP 有效期分钟数。
        self.count = count  #: 每次向供应商 API 请求的代理端点数量。
        self.api_url = api_url  #: 保留配置参数后的 51 代理 API URL 模板。
        self.timeout = timeout  #: 请求 51 代理 API 的超时秒数。
        self.refresh_before_seconds = refresh_before_seconds  #: 缓存 IP 距离实际过期多长时间时提前刷新。

        self.current_endpoint: Optional[ProxyEndpoint] = None  #: 当前复用的代理主机与端口。
        self.current_proxies: Optional[Dict[str, str]] = None  #: 与当前端点对应的 requests 代理映射。
        self.current_expire_at: Optional[float] = None  #: 当前代理在单调时钟上的本地可用截止点。
        self.last_endpoint: Optional[ProxyEndpoint] = None  #: 最近一次 API 解析出的端点，供日志和诊断读取。

    def _build_api_url(self, *, count: Optional[int] = None) -> str:
        """构造本次 51 代理 API URL，只覆盖模板中的 ``qty`` 参数。

        未显式指定数量时使用实例 ``count``；原 URL 的认证、地区和其他查询参数
        及片段均原样保留，数量超出供应商 1 至 200 范围时立即报错。
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
        """解析 51 代理 ``timeip/getip`` JSON，返回去重后的合法端点列表。

        非成功业务码、空列表、非字典项、缺失主机端口和非法端口都会记录并跳过；
        方法不因单条坏数据中断其余端点解析。
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
        """解析供应商响应并返回第一条去重后的有效端点，没有则返回 ``None``。"""
        endpoints = self._extract_endpoints_from_json(data)
        return endpoints[0] if endpoints else None

    def _fetch_proxy_endpoint(self) -> Optional[ProxyEndpoint]:
        """限流调用 51 代理 API 并返回一条可用端点。

        HTTP、JSON 或供应商业务响应异常均被记录并转换为 ``None``；无论成功
        与否都会同步更新 ``last_endpoint``，供调用方判断最近一次获取结果。
        """
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
        """清除当前端点、requests 映射和本地过期时间，使下次调用重新获取。"""
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
        """检查当前代理状态是否完整且尚未到达提前刷新时间。"""
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
        """记录当前代理请求成功；缓存状态保持不变以便后续继续复用。"""
        if self.current_endpoint:
            print(
                f"[代理池] 当前代理请求成功，继续复用: {self.current_endpoint.display()}"
            )

    def on_failure(self, exc: Exception) -> None:
        """记录代理请求失败并立即清除当前代理，强制下次获取新端点。"""
        if self.current_endpoint:
            print(
                f"[代理池] 当前代理请求失败，准备弃用: "
                f"{self.current_endpoint.display()} | {repr(exc)}"
            )
        else:
            print(f"[代理池] 请求失败，但当前未记录代理: {repr(exc)}")

        self._clear_current_proxy()


class AsyncDailiProxyProvider(DailiProxyProvider):
    """为协程页面爬虫提供单代理缓存的异步 51 代理实现。

    供应商 API 使用共享同步限流器与实例异步限流器双重保护；异步锁确保同一
    实例只有一个协程刷新当前端点，HTTP 客户端由 :meth:`close` 显式释放。
    """

    def __init__(
        self,
        minutes: int,
        *,
        count: int = 1,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
        rate_limiter: Optional[AsyncRequestRateLimiter] = None,
    ) -> None:
        """初始化同步缓存规则、异步 HTTP 客户端和延迟绑定事件循环的锁。"""
        super().__init__(
            minutes,
            count=count,
            timeout=timeout,
            refresh_before_seconds=refresh_before_seconds,
        )
        self._client = httpx.AsyncClient(  #: 调用 51 代理 API 的独立异步客户端。
            timeout=timeout,
            trust_env=False,
        )
        self._lock: Optional[asyncio.Lock] = None  #: 串行化当前代理读取和刷新操作的异步锁。
        self._rate_limiter = rate_limiter or AsyncRequestRateLimiter()  #: 当前实例的异步 API 请求限流器。

    async def _fetch_proxy_endpoint_async(self) -> Optional[ProxyEndpoint]:
        """异步请求供应商 API 并解析一条代理端点。

        共享同步限流器在线程池执行，随后等待实例异步限流器；所有网络、状态和
        解析异常都记录为警告并返回 ``None``，同时更新 ``last_endpoint``。
        """
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
        """在异步锁内复用有效代理，或获取并缓存一个新的 requests 映射。"""
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
        """仅当成功通知对应当前代理映射时记录成功，过期通知被忽略。"""
        if proxies is None or self.current_proxies != proxies:
            return
        logger.debug("proxy_request_succeeded endpoint=%s", self.current_endpoint)

    def on_failure_for(
        self,
        proxies: Optional[Dict[str, str]],
        exc: Exception,
    ) -> None:
        """仅在失败映射仍是当前代理时记录异常并清空缓存。

        该身份检查避免较早请求的迟到失败通知误删已经完成刷新后的新代理。
        """
        if proxies is None or self.current_proxies != proxies:
            return
        logger.warning(
            "proxy_discarded endpoint=%s error=%s",
            self.current_endpoint.display() if self.current_endpoint else None,
            repr(exc),
        )
        self._clear_current_proxy()

    async def close(self) -> None:
        """关闭获取代理端点所用的 httpx 异步客户端及其连接池。"""
        await self._client.aclose()


@dataclass
class _ProxyPoolSlot:
    """代理池中的一个端点租约槽位及其实时并发、淘汰状态。"""

    endpoint: ProxyEndpoint  #: 该槽位代表的供应商代理端点。
    proxies: Dict[str, str]  #: 可直接交给 HTTP 调用方的 requests 代理映射。
    expire_at: float  #: 槽位停止分配新请求的单调时钟截止点。
    in_flight: int = 0  #: 当前已租出但尚未收到成功或失败通知的请求数。
    draining: bool = False  #: 是否已过期或失败、只等待在途请求释放后删除。


@dataclass
class ProxyPoolStats:
    """记录代理池从供应商获取、分配、成功、失败和淘汰的累计指标。"""

    api_request_count: int = 0  #: 已发往 51 代理 API 的批量请求次数。
    requested_endpoint_count: int = 0  #: 所有 API 请求中 ``qty`` 的累计请求端点数。
    received_endpoint_count: int = 0  #: API 响应中成功解析出的去重端点总数。
    added_endpoint_count: int = 0  #: 去除池内重复项后实际加入槽位的端点总数。
    discarded_endpoint_count: int = 0  #: 因业务请求失败而标记淘汰的端点数。
    expired_endpoint_count: int = 0  #: 因到达本地可用截止点而进入排空状态的端点数。
    lease_count: int = 0  #: 成功向调用方分配代理映射的累计次数。
    success_count: int = 0  #: 调用方归还的成功代理租约累计次数。
    failure_count: int = 0  #: 调用方归还的失败代理租约累计次数。
    max_in_flight: int = 0  #: 池内所有槽位同时在途请求数的历史峰值。


class AsyncDailiProxyPool(DailiProxyProvider):
    """维护多个 51 代理 IP，并限制每个端点的并发租约数。

    当没有可分配槽位时，一个协程负责批量补池，其他协程在条件变量上等待；
    过期或失败端点停止接收新请求，待在途租约归还后从池中删除。
    """

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
        """校验池容量和单代理并发上限，并初始化空槽位集合与统计指标。"""
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
        self.pool_size = pool_size  #: 期望保持的非排空代理槽位数量。
        self.max_concurrency_per_proxy = max_concurrency_per_proxy  #: 单个代理允许同时租出的最大请求数。
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)  #: 批量获取端点的异步 HTTP 客户端。
        self._condition_instance: Optional[asyncio.Condition] = None  #: 延迟绑定当前事件循环的池状态条件变量。
        self._rate_limiter = rate_limiter or AsyncRequestRateLimiter()  #: 当前池实例的异步供应商 API 限流器。
        self._slots: List[_ProxyPoolSlot] = []  #: 当前有效或正在排空的代理槽位集合。
        self._fetching = False  #: 是否已有协程离开条件锁并正在调用供应商补池。
        self.stats = ProxyPoolStats()  #: 暴露给诊断和测试读取的累计运行指标。

    @property
    def _condition(self) -> asyncio.Condition:
        """返回延迟创建的池状态条件变量，确保它绑定实际运行事件循环。"""
        if self._condition_instance is None:
            self._condition_instance = asyncio.Condition()
        return self._condition_instance

    def _slot_expire_at(self) -> float:
        """计算新槽位在单调时钟上的提前刷新截止点。"""
        usable_ttl_seconds = max(
            1,
            self.minutes * 60 - self.refresh_before_seconds,
        )
        return time.monotonic() + usable_ttl_seconds

    def _mark_expired_and_cleanup(self) -> None:
        """标记已到期槽位，并删除没有在途请求的排空槽位。

        过期计数只在槽位首次进入排空状态时增加，仍有租约的槽位会保留到最后
        一次成功或失败通知归还租约。
        """
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
        """选择负载最低且截止时间最早的可分配槽位，没有则返回 ``None``。"""
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
        """按 ``count`` 批量请求代理端点，并更新供应商 API 相关统计。

        请求依次经过进程级同步限流和实例级异步限流；网络或解析失败返回空列表，
        不在此处修改槽位，由持有条件变量的调用方统一合并结果。
        """
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
        """租出一个可用代理映射，必要时由单个协程批量补池。

        成功租出时增加槽位在途数和统计；无槽位且其他协程正在补池时等待通知。
        如果供应商没有返回任何可加入端点且仍无可用槽位，则返回 ``None``。
        """
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
        """按完整 requests 代理映射查找对应池槽位，找不到时返回 ``None``。"""
        return next((slot for slot in self._slots if slot.proxies == proxies), None)

    async def on_success_for(
        self,
        proxies: Optional[Dict[str, str]],
    ) -> None:
        """归还一个成功租约，减少在途数并唤醒等待分配的协程。"""
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
        """归还失败租约并淘汰对应端点，再唤醒等待补池或分配的协程。"""
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
        """关闭代理池用于批量获取端点的 httpx 客户端连接池。"""
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
    """异步强制取得代理映射，提供器无可用代理时禁止静默回退本机直连。"""
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
