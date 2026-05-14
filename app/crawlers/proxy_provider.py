from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol
from urllib.parse import urlencode

import requests
from app.core.config import Settings


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

    def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        ...

    def on_success(self) -> None:
        ...

    def on_failure(self, exc: Exception) -> None:
        ...


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


class ShanchenProxyProvider:
    API_BASE_URL = "https://sch.shanchendaili.com/api.html"

    def __init__(
        self,
        minutes: int,
        *,
        timeout: int = 10,
        refresh_before_seconds: int = 10,
    ) -> None:
        if not isinstance(minutes, int):
            raise TypeError("minutes 必须是 int 类型")

        if minutes <= 0:
            raise ValueError("minutes 必须大于 0")


        key = Settings().proxy_api_key.strip()

        if not key:
            raise ValueError("未配置闪臣代理 key，请检查 .env 中的 PROXY_API_KEY")

        self.minutes = minutes
        self.key = key
        self.timeout = timeout
        self.refresh_before_seconds = refresh_before_seconds

        self.current_endpoint: Optional[ProxyEndpoint] = None
        self.current_proxies: Optional[Dict[str, str]] = None
        self.current_expire_at: Optional[float] = None
        self.last_endpoint: Optional[ProxyEndpoint] = None

    def _build_api_url(self) -> str:
        """
        拼接固定参数后的 API URL。

        只允许 time 由 minutes 控制，key 从环境变量读取。
        """
        params = {
            "action": "get_ip",
            "key": self.key,
            "time": self.minutes,
            "count": 1,
            "type": "json",
            "province": 215,
            "city": 215,
            "only": 1,
        }
        return f"{self.API_BASE_URL}?{urlencode(params)}"

    def _extract_endpoint_from_json(self, data: Any) -> Optional[ProxyEndpoint]:
        """
        解析闪臣普通 get_ip JSON 返回。

        文档示例：

        {
            "count": "1",
            "status": "0",
            "list": [
                {
                    "sever": "114.104.100.60",
                    "port": 9700,
                    "net_type": 2
                }
            ]
        }
        """
        if not isinstance(data, dict):
            return None

        status = str(data.get("status", "")).strip()
        if status and status != "0":
            info = data.get("info", "未知错误")
            print(f"[代理池] 代理接口返回失败 status={status}, info={info}")
            return None

        items = data.get("list")
        if not isinstance(items, list) or not items:
            print(f"[代理池] JSON 中没有可用 list: {data}")
            return None

        item = items[0]
        if not isinstance(item, dict):
            print(f"[代理池] list[0] 格式异常: {item}")
            return None

        host = (
            item.get("sever")
            or item.get("server")
            or item.get("ip")
            or item.get("IP")
            or item.get("host")
        )
        port = item.get("port") or item.get("Port")

        if not host or not port:
            print(f"[代理池] list[0] 中未找到 sever/port: {item}")
            return None

        try:
            endpoint = ProxyEndpoint(
                host=str(host).strip(),
                port=int(str(port).strip()),
            )
        except ValueError:
            print(f"[代理池] port 不是合法整数: {port}")
            return None

        return endpoint

    def _fetch_proxy_endpoint(self) -> Optional[ProxyEndpoint]:
        api_url = self._build_api_url()

        try:
            resp = requests.get(api_url, timeout=self.timeout)
            resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError:
                print(f"[代理池] 接口没有返回合法 JSON，响应前 300 字符: {resp.text[:300]}")
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
            print(f"[代理池] 获取代理失败: {repr(e)}")
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

    def _set_current_proxy(self, endpoint: ProxyEndpoint, proxies: Dict[str, str]) -> None:
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
            print(f"[代理池] 当前代理已过期或接近过期: {self.current_endpoint.display()}")
            return False

        return True

    def get_requests_proxies(self) -> Optional[Dict[str, str]]:
        """
        获取 requests 可用的 proxies。

        - 有可用当前代理：复用
        - 没有可用当前代理：从闪臣 API 提取新的
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
            print(f"[代理池] 当前代理请求成功，继续复用: {self.current_endpoint.display()}")

    def on_failure(self, exc: Exception) -> None:
        if self.current_endpoint:
            print(
                f"[代理池] 当前代理请求失败，准备弃用: "
                f"{self.current_endpoint.display()} | {repr(exc)}"
            )
        else:
            print(f"[代理池] 请求失败，但当前未记录代理: {repr(exc)}")

        self._clear_current_proxy()


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
    """
    Linux / macOS:
        export SHANCHEN_PROXY_KEY="你的闪臣key"

    Windows PowerShell:
        $env:SHANCHEN_PROXY_KEY="你的闪臣key"
    """

    provider = ShanchenProxyProvider(minutes=1)

    quick_test_proxy(provider)

    resp = request_with_proxy(
        provider,
        "GET",
        "https://httpbin.org/ip",
        headers={"User-Agent": "Mozilla/5.0"},
    )

    print(resp.text)