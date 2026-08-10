from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import socket
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests
from pymongo import MongoClient, UpdateOne

from app.core.config import Settings
from app.crawlers.proxy_provider import DailiProxyProvider, get_required_proxies
from app.crawlers.stock_daily_detail_crawler import StockDailyDetailCrawler


CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class StockTarget:
    code: str
    name: str | None
    market: str


@dataclass(frozen=True)
class UpsertStats:
    rows: int = 0
    affected: int = 0


def parse_date(value: str) -> date:
    """Parse either YYYY-MM-DD or YYYYMMDD into a date."""

    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须使用 YYYY-MM-DD 或 YYYYMMDD: {value!r}"
        ) from exc


def today_cn() -> date:
    return datetime.now(CN_TZ).date()


def five_years_before(reference: date) -> date:
    try:
        return reference.replace(year=reference.year - 5)
    except ValueError:
        return reference.replace(year=reference.year - 5, day=28)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("参数必须大于 0")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("参数不能小于 0")
    return number


def normalize_code(value: str) -> str:
    code = str(value).strip().zfill(6)
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码必须是 6 位数字: {value!r}")
    return code


def market_for_code(value: str) -> str:
    code = normalize_code(value)
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    raise ValueError(f"无法识别 A 股市场: {code}")


async def _fetch_current_targets() -> list[StockTarget]:
    crawler = StockDailyDetailCrawler()
    try:
        frame = await crawler.fetch_stock_list()
    finally:
        await crawler.close()

    targets = [
        StockTarget(
            code=normalize_code(row["代码"]),
            name=str(row["名称"]).strip() or None,
            market=market_for_code(row["代码"]),
        )
        for row in frame.to_dict("records")
    ]
    return sorted(targets, key=lambda item: item.code)


def load_targets(
    *,
    only_code: str | None,
    offset: int,
    limit: int | None,
) -> list[StockTarget]:
    """Load the current A-share universe, or build a single explicit target."""

    if only_code:
        code = normalize_code(only_code)
        return [StockTarget(code=code, name=None, market=market_for_code(code))]

    targets = asyncio.run(_fetch_current_targets())
    stop = None if limit is None else offset + limit
    return targets[offset:stop]


def open_database() -> tuple[MongoClient, Any]:
    settings = Settings()
    client = MongoClient(settings.mongo_uri)
    client.admin.command("ping")
    return client, client[settings.mongo_db_name]


def keep_targets_without_data(
    collection: Any,
    targets: Sequence[StockTarget],
) -> list[StockTarget]:
    """Return only targets for which the destination collection has no row."""

    codes = [target.code for target in targets]
    existing_codes = set(
        collection.distinct("code", {"code": {"$in": codes}})
    )
    return [target for target in targets if target.code not in existing_codes]


def fill_missing_target_names(
    reference_collection: Any,
    targets: Sequence[StockTarget],
) -> list[StockTarget]:
    """Fill explicit single-code targets from an existing named collection."""

    resolved: list[StockTarget] = []
    for target in targets:
        if target.name:
            resolved.append(target)
            continue
        row = reference_collection.find_one(
            {"code": target.code, "name": {"$nin": [None, ""]}},
            {"_id": 0, "name": 1},
        )
        name = str(row["name"]).strip() if row and row.get("name") else None
        resolved.append(
            StockTarget(code=target.code, name=name, market=target.market)
        )
    return resolved


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    return None if math.isnan(number) else number


def required_float(value: Any, *, field: str) -> float:
    number = optional_float(value)
    if number is None:
        raise ValueError(f"行情字段 {field} 为空")
    return number


def baostock_bar_timestamp(value: str) -> str:
    raw = str(value).strip()
    if len(raw) < 14:
        raise ValueError(f"BaoStock 分钟时间格式异常: {value!r}")
    parsed = datetime.strptime(raw[:14], "%Y%m%d%H%M%S")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def sina_bar_timestamp(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).strip())
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def ensure_baostock_login(baostock: Any, *, max_attempts: int = 3) -> None:
    """Establish a BaoStock session, retrying transient socket failures."""

    last_error = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            result = baostock.login()
            if result.error_code == "0":
                return
            last_error = result.error_msg
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < max_attempts:
            time.sleep(min(2**attempt, 8))
    raise RuntimeError(f"BaoStock 登录失败: {last_error}")


def open_http_connect_tunnel(
    proxy_url: str,
    *,
    target_host: str,
    target_port: int,
    connect_timeout: int,
    socket_timeout: int,
) -> socket.socket:
    """Open a TCP tunnel through an HTTP proxy using CONNECT."""

    parts = urlsplit(proxy_url)
    if parts.scheme != "http" or not parts.hostname or not parts.port:
        raise ValueError("BaoStock 代理必须是包含主机和端口的 HTTP URL")

    tunnel = socket.create_connection(
        (parts.hostname, parts.port),
        timeout=connect_timeout,
    )
    tunnel.settimeout(socket_timeout)
    try:
        tunnel.sendall(
            (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                "Proxy-Connection: Keep-Alive\r\n\r\n"
            ).encode("ascii")
        )
        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16384:
            chunk = tunnel.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        status_line = bytes(response).split(b"\r\n", 1)[0].decode(
            "latin1",
            "replace",
        )
        status_parts = status_line.split(" ", 2)
        status_code = status_parts[1] if len(status_parts) > 1 else "invalid"
        if status_code != "200":
            raise RuntimeError(f"HTTP CONNECT 失败: status={status_code}")
        return tunnel
    except Exception:
        tunnel.close()
        raise


class BaoStockTunnelSocket:
    """Raise on proxy EOF so BaoStock cannot spin forever on empty recv()."""

    def __init__(self, raw_socket: socket.socket) -> None:
        self.raw_socket = raw_socket
        self.failure: Exception | None = None

    def recv(self, size: int) -> bytes:
        try:
            data = self.raw_socket.recv(size)
        except Exception as exc:
            self.failure = exc
            raise
        if not data:
            self.failure = ConnectionError("BaoStock 代理连接已关闭")
            raise self.failure
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw_socket, name)


class BaoStockProxySession:
    """Keep one BaoStock TCP session on a short-lived rotating HTTP proxy."""

    TARGET_HOST = "public-api.baostock.com"
    TARGET_PORT = 10030

    def __init__(
        self,
        baostock: Any,
        *,
        proxy_provider: Any | None = None,
        connect_timeout: int = 10,
        socket_timeout: int = 90,
        max_queries_per_proxy: int = 40,
        max_lifetime_seconds: int = 150,
        login_attempts: int = 20,
        retry_delay_seconds: int = 15,
    ) -> None:
        self.baostock = baostock
        self.proxy_provider = proxy_provider or DailiProxyProvider(minutes=3)
        self.connect_timeout = connect_timeout
        self.socket_timeout = socket_timeout
        self.max_queries_per_proxy = max_queries_per_proxy
        self.max_lifetime_seconds = max_lifetime_seconds
        self.login_attempts = login_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self._socket: BaoStockTunnelSocket | None = None
        self._connected_at = 0.0
        self._query_count = 0

    @staticmethod
    def _quiet_call(function: Any, *args: Any) -> Any:
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)

    def _close_socket(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.close()
        finally:
            try:
                import baostock.common.context as baostock_context

                if getattr(baostock_context, "default_socket", None) is self._socket:
                    setattr(baostock_context, "default_socket", None)
            finally:
                self._socket = None
                self._connected_at = 0.0
                self._query_count = 0

    def rotate(self, reason: Exception | None = None) -> None:
        self._close_socket()
        self._quiet_call(
            self.proxy_provider.on_failure,
            reason or RuntimeError("BaoStock 代理轮换"),
        )

    def _login_once(self) -> None:
        proxies = self._quiet_call(get_required_proxies, self.proxy_provider)
        proxy_url = proxies.get("https") or proxies.get("http")
        if not proxy_url:
            raise RuntimeError("代理池未返回 HTTP 代理地址")

        raw_tunnel = open_http_connect_tunnel(
            proxy_url,
            target_host=self.TARGET_HOST,
            target_port=self.TARGET_PORT,
            connect_timeout=self.connect_timeout,
            socket_timeout=self.socket_timeout,
        )
        tunnel = BaoStockTunnelSocket(raw_tunnel)
        self._socket = tunnel
        import baostock.common.context as baostock_context
        import baostock.util.socketutil as socketutil

        original_connect = socketutil.SocketUtil.connect

        def use_tunnel(_socket_util: Any) -> None:
            setattr(baostock_context, "default_socket", tunnel)

        socketutil.SocketUtil.connect = use_tunnel
        try:
            result = self._quiet_call(self.baostock.login)
        finally:
            socketutil.SocketUtil.connect = original_connect

        if result.error_code != "0":
            raise RuntimeError(
                f"BaoStock 登录失败: {result.error_code} {result.error_msg}"
            )

        self._connected_at = time.monotonic()
        self._query_count = 0
        self._quiet_call(self.proxy_provider.on_success)

    def ensure_login(self) -> None:
        if self._socket is not None:
            age = time.monotonic() - self._connected_at
            if (
                age < self.max_lifetime_seconds
                and self._query_count < self.max_queries_per_proxy
            ):
                return
            self.rotate()

        last_error: Exception | None = None
        for attempt in range(1, self.login_attempts + 1):
            started = time.monotonic()
            try:
                self._login_once()
                print(
                    f"baostock_proxy_login=success attempt={attempt} "
                    f"seconds={time.monotonic() - started:.2f}",
                    flush=True,
                )
                return
            except Exception as exc:
                last_error = exc
                self.rotate(exc)
                print(
                    f"baostock_proxy_login=failed attempt={attempt}/"
                    f"{self.login_attempts} error={type(exc).__name__}: {exc}",
                    flush=True,
                )
                if attempt < self.login_attempts:
                    time.sleep(self.retry_delay_seconds)
        raise RuntimeError(f"BaoStock 代理登录失败: {last_error}")

    def note_query(self) -> None:
        self._query_count += 1

    def close(self) -> None:
        self._close_socket()


class EastMoneyKlineClient:
    """Small synchronous fallback for public EastMoney daily and 15m bars."""

    URLS = (
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    )
    UT = "7eea3edcaed734bea9cbfc24409ed989"
    FIELDS1 = "f1,f2,f3,f4,f5,f6"
    FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

    def __init__(self) -> None:
        self.proxy_provider = DailiProxyProvider(minutes=3)
        self.session = curl_requests.Session(impersonate="chrome124")

    def close(self) -> None:
        self.session.close()

    def fetch_rows(
        self,
        *,
        code: str,
        interval: str,
        start_date: date,
        end_date: date,
        max_attempts: int = 6,
    ) -> list[dict[str, str]]:
        if interval not in {"daily", "15m"}:
            raise ValueError(f"unsupported interval: {interval}")

        params = {
            "secid": f"0.{normalize_code(code)}",
            "ut": self.UT,
            "fields1": self.FIELDS1,
            "fields2": self.FIELDS2,
            "klt": "101" if interval == "daily" else "15",
            "fqt": "0",
            "beg": start_date.strftime("%Y%m%d"),
            "end": end_date.strftime("%Y%m%d"),
            "lmt": "1000000",
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "Referer": f"https://quote.eastmoney.com/bj{code}.html",
            "Accept": "application/json,text/plain,*/*",
        }
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            proxies = None
            try:
                proxies = get_required_proxies(self.proxy_provider)
                proxy_url = proxies.get("https") or proxies.get("http")
                for url in self.URLS:
                    try:
                        response = self.session.get(
                            url,
                            params=params,
                            headers=headers,
                            proxy=proxy_url,
                            timeout=20,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict) or payload.get("rc") not in (
                            None,
                            0,
                        ):
                            raise RuntimeError(
                                f"东方财富返回异常 rc={payload.get('rc') if isinstance(payload, dict) else None}"
                            )
                        data = payload.get("data")
                        lines = data.get("klines") if isinstance(data, dict) else None
                        if not lines:
                            self.proxy_provider.on_success()
                            return []
                        if not isinstance(lines, list):
                            raise RuntimeError("东方财富 K 线字段不是列表")
                        self.proxy_provider.on_success()
                        return [self._parse_line(str(line)) for line in lines]
                    except Exception as exc:
                        last_error = exc
                assert last_error is not None
                raise last_error
            except Exception as exc:
                last_error = exc
                if proxies is not None:
                    self.proxy_provider.on_failure(exc)
                self.session.close()
                self.session = curl_requests.Session(impersonate="chrome124")
                if attempt < max_attempts:
                    time.sleep(min(2**attempt, 8))

        raise RuntimeError(f"东方财富 K 线请求失败: {last_error}")

    @staticmethod
    def _parse_line(line: str) -> dict[str, str]:
        fields = line.split(",")
        if len(fields) < 7:
            raise ValueError(f"东方财富 K 线格式异常: {line!r}")
        return {
            "time": fields[0],
            "open": fields[1],
            "close": fields[2],
            "high": fields[3],
            "low": fields[4],
            "volume": fields[5],
            "amount": fields[6],
        }


class SinaMinuteClient:
    """Fetch Sina 15m bars through the configured rotating proxy provider."""

    URL = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/"
        "CN_MarketDataService.getKLineData"
    )

    def __init__(self) -> None:
        self.proxy_provider = DailiProxyProvider(minutes=3)
        self.session = curl_requests.Session(impersonate="chrome124")

    def close(self) -> None:
        self.session.close()

    def fetch_rows(
        self,
        *,
        code: str,
        max_attempts: int = 6,
    ) -> list[dict[str, Any]]:
        symbol = f"bj{normalize_code(code)}"
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            proxies = None
            try:
                proxies = get_required_proxies(self.proxy_provider)
                proxy_url = proxies.get("https") or proxies.get("http")
                response = self.session.get(
                    self.URL,
                    params={
                        "symbol": symbol,
                        "scale": "15",
                        "ma": "no",
                        "datalen": "1970",
                    },
                    headers={
                        "Referer": (
                            f"https://finance.sina.com.cn/realstock/company/{symbol}/nc.shtml"
                        )
                    },
                    proxy=proxy_url,
                    timeout=20,
                )
                response.raise_for_status()
                try:
                    payload = json.loads(
                        response.text.split("=(", 1)[1].rsplit(");", 1)[0]
                    )
                except (IndexError, json.JSONDecodeError) as exc:
                    raise RuntimeError("新浪分钟接口返回无效 JSONP") from exc
                if payload is None:
                    raise RuntimeError("新浪分钟接口被限流或未返回数据")
                if not isinstance(payload, list):
                    raise RuntimeError("新浪分钟接口结果不是列表")
                self.proxy_provider.on_success()
                return [dict(row) for row in payload]
            except Exception as exc:
                last_error = exc
                if proxies is not None:
                    self.proxy_provider.on_failure(exc)
                self.session.close()
                self.session = curl_requests.Session(impersonate="chrome124")
                if attempt < max_attempts:
                    time.sleep(min(2**attempt, 8))

        raise RuntimeError(f"新浪分钟请求失败: {last_error}")


def upsert_documents(
    collection: Any,
    documents: Iterable[dict[str, Any]],
    *,
    key_fields: Sequence[str],
    batch_size: int,
) -> UpsertStats:
    """Stream documents into idempotent unordered MongoDB bulk writes."""

    operations: list[UpdateOne] = []
    rows = 0
    affected = 0

    def flush() -> None:
        nonlocal affected
        if not operations:
            return
        result = collection.bulk_write(operations, ordered=False)
        affected += int(result.upserted_count + result.modified_count)
        operations.clear()

    for source_document in documents:
        document = dict(source_document)
        now = datetime.now(CN_TZ)
        document["updated_at"] = now
        key = {field: document[field] for field in key_fields}
        operations.append(
            UpdateOne(
                key,
                {"$set": document, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        )
        rows += 1
        if len(operations) >= batch_size:
            flush()

    flush()
    return UpsertStats(rows=rows, affected=affected)


def insert_missing_documents(
    collection: Any,
    documents: Iterable[dict[str, Any]],
    *,
    key_fields: Sequence[str],
    batch_size: int,
) -> UpsertStats:
    """Insert absent keys without changing rows already stored in MongoDB."""

    operations: list[UpdateOne] = []
    rows = 0
    affected = 0

    def flush() -> None:
        nonlocal affected
        if not operations:
            return
        result = collection.bulk_write(operations, ordered=False)
        affected += int(result.upserted_count)
        operations.clear()

    for source_document in documents:
        document = dict(source_document)
        now = datetime.now(CN_TZ)
        document["created_at"] = now
        document["updated_at"] = now
        key = {field: document[field] for field in key_fields}
        operations.append(
            UpdateOne(
                key,
                {"$setOnInsert": document},
                upsert=True,
            )
        )
        rows += 1
        if len(operations) >= batch_size:
            flush()

    flush()
    return UpsertStats(rows=rows, affected=affected)
