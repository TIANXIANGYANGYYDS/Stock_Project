from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

import httpx
import pandas as pd

from app.crawlers.eastmoney_reverse_fetcher import EastMoneyReverseFetcher
from app.crawlers.proxy_provider import (
    AsyncProxyProvider,
    AsyncRequestRateLimiter,
    AsyncDailiProxyPool,
    get_required_async_proxies,
)
from app.models.stock_daily_detail import (
    ATRIndicators,
    BOLLIndicators,
    CCIIndicators,
    ChipChart,
    ChipCostRange,
    ChipDistribution,
    KDJIndicators,
    MAIndicators,
    MACDIndicators,
    RSIIndicators,
    StockDailyDetail,
    StockDailyDetailSource,
    VolumeMAIndicators,
    WRIndicators,
)


logger = logging.getLogger(__name__)
_T = TypeVar("_T")
CN_TZ = timezone(timedelta(hours=8))


class EastMoneyDataFetcher:
    """通过东方财富公开 JSON 接口读取股票清单、交易日和基础日 K 数据。

    实例按代理地址复用独立的 httpx 客户端，避免本地连接和不同代理连接池混用；
    多个备用域名按顺序尝试，所有返回值在进入上层组装流程前完成结构校验。
    """

    #: 东方财富日 K 接口的主域名与历史数据备用域名，按顺序尝试。
    KLINE_URLS = (
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    )
    #: A 股实时列表接口的多个可用域名，单页请求失败时顺序回退。
    CLIST_URLS = (
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
    )
    #: 股票列表接口的沪深京 A 股市场筛选表达式。
    CLIST_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
    #: 股票列表所需代码、名称、价格、成交量、成交额和更新时间字段。
    CLIST_FIELDS = "f12,f14,f2,f5,f6,f124"
    #: 默认连接超时和读取超时秒数组合。
    DEFAULT_TIMEOUT = (5, 12)
    #: 日 K 接口固定请求的基础元数据字段集合。
    KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
    #: 日 K 接口返回日期、OHLC、量额和涨跌指标的字段集合。
    KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    #: 东方财富日 K 接口要求的固定访问令牌参数。
    UT = "7eea3edcaed734bea9cbfc24409ed989"
    #: 东方财富股票列表接口要求的固定访问令牌参数。
    CLIST_UT = "bd1d9ddb04089700cf9c27f6f7426281"

    def __init__(
        self,
        *,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化超时、东方财富请求头和按代理地址分组的客户端缓存。"""
        self.timeout = timeout  #: httpx 使用的 ``(连接超时, 读取超时)`` 秒数。
        #: 东方财富 JSON 接口使用的统一桌面浏览器请求头。
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        #: 以代理 URL 为键复用的异步客户端；``None`` 键代表本地直连客户端。
        self._clients: Dict[Optional[str], httpx.AsyncClient] = {}

    async def close(self) -> None:
        """关闭所有本地和代理 httpx 客户端，并清空客户端缓存。"""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def _get_client(
        self,
        proxies: Optional[Dict[str, str]],
    ) -> httpx.AsyncClient:
        """返回与给定代理映射匹配的复用客户端，必要时创建新客户端。

        代理映射必须至少含 ``https`` 或 ``http`` 地址；每个代理 URL 使用独立
        连接池，避免更换代理后复用旧连接。空映射使用禁用环境代理的直连客户端。
        """
        proxy = None
        if proxies:
            proxy = proxies.get("https") or proxies.get("http")
            if not proxy:
                raise RuntimeError(f"代理配置缺少 http/https 地址: {proxies!r}")
        client = self._clients.get(proxy)
        if client is None:
            connect_timeout, read_timeout = self.timeout
            client = httpx.AsyncClient(
                headers=self.headers,
                proxy=proxy,
                timeout=httpx.Timeout(
                    connect=connect_timeout,
                    read=read_timeout,
                    write=read_timeout,
                    pool=connect_timeout,
                ),
                trust_env=False,
            )
            self._clients[proxy] = client
        return client

    @staticmethod
    def get_secid(code: str) -> str:
        """把六位股票代码转换为东方财富 ``市场.代码`` 形式的 ``secid``。"""
        normalized_code = str(code).strip().zfill(6)
        market_code = 1 if normalized_code.startswith("6") else 0
        return f"{market_code}.{normalized_code}"

    @staticmethod
    def _get_fqt(adjust: str) -> str:
        """把复权口径映射为接口 ``fqt`` 值，不支持的口径抛出 ``ValueError``。"""
        try:
            return {"": "0", "qfq": "1", "hfq": "2"}[adjust]
        except KeyError as exc:
            raise ValueError(f"unsupported adjust value: {adjust!r}") from exc

    async def _request_url_json(
        self,
        url: str,
        *,
        params: Dict[str, Any],
        proxies: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """请求一个东方财富 JSON 地址并校验 HTTP、JSON 和业务返回码。

        成功时保证返回顶层字典；非 JSON、非对象或非零 ``rc`` 都转换为带响应
        摘要的 ``RuntimeError``，便于上层备用域名与代理重试逻辑处理。
        """
        response = await self._get_client(proxies).get(
            url,
            params=params,
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"eastmoney returned non-json response, preview={response.text[:200]!r}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(f"eastmoney returned invalid payload: {payload!r}")
        if payload.get("rc") not in (None, 0):
            raise RuntimeError(
                "eastmoney request failed "
                f"rc={payload.get('rc')}, payload_preview={str(payload)[:300]}"
            )
        return payload

    @staticmethod
    def _extract_kline_lines(payload: Dict[str, Any]) -> List[str]:
        """从日 K 响应中提取字符串行列表；无数据返回空列表，类型异常则报错。"""
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        klines = data.get("klines")
        if not klines:
            return []
        if not isinstance(klines, list):
            raise RuntimeError(f"eastmoney klines is not list: {klines!r}")
        return [str(item) for item in klines]

    async def _fetch_kline_lines(
        self,
        *,
        code: Optional[str],
        adjust: str,
        start_date: str,
        end_date: str,
        proxies: Optional[Dict[str, str]] = None,
        secid: Optional[str] = None,
    ) -> List[str]:
        """按备用域名顺序获取指定证券和日期范围的原始日 K CSV 行。

        ``secid`` 可用于指数等无普通股票代码的请求；所有域名均失败时重抛最后
        一个异常，接口正常但没有 K 线时返回空列表。
        """
        params: Dict[str, Any] = {
            "secid": secid or self.get_secid(code or ""),
            "ut": self.UT,
            "fields1": self.KLINE_FIELDS1,
            "fields2": self.KLINE_FIELDS2,
            "klt": "101",
            "fqt": self._get_fqt(adjust),
            "beg": start_date,
            "end": end_date,
            "lmt": "0",
            "_": str(int(time.time() * 1000)),
        }
        last_error: Optional[Exception] = None

        for url in self.KLINE_URLS:
            try:
                lines = self._extract_kline_lines(
                    await self._request_url_json(
                        url,
                        params=params,
                        proxies=proxies,
                    )
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "eastmoney_kline_request_failed url=%s secid=%s error=%s",
                    url,
                    params["secid"],
                    repr(exc),
                )
                continue
            if lines:
                return lines

        if last_error is not None:
            raise last_error
        return []

    async def fetch_trade_dates(
        self,
        start_date: str,
        end_date: str,
        *,
        proxies: Optional[Dict[str, str]] = None,
    ) -> tuple[str, ...]:
        """以沪指日 K 日期作为交易日历，返回范围内排序去重的日期元组。"""
        lines = await self._fetch_kline_lines(
            code=None,
            adjust="",
            start_date=start_date,
            end_date=end_date,
            proxies=proxies,
            secid="1.000001",
        )
        return tuple(sorted({line.split(",", 1)[0] for line in lines if line}))

    async def fetch_stock_list(
        self,
        *,
        proxies: Optional[Dict[str, str]] = None,
        target_trade_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """分页抓取沪深京股票代码和名称，并可过滤到指定日期有成交的股票。

        退市名称、零成交和无有效更新时间的记录会被排除；结果按代码去重，
        ``DataFrame.attrs['latest_trade_date']`` 保存接口观察到的最近成交日期。
        """
        page = 1
        page_size = 500
        rows: List[Dict[str, str]] = []
        fetched_count = 0
        latest_trade_date: Optional[str] = None

        while True:
            params = {
                "pn": str(page),
                "pz": str(page_size),
                "po": "1",
                "np": "1",
                "ut": self.CLIST_UT,
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": self.CLIST_FS,
                "fields": self.CLIST_FIELDS,
                "_": str(int(time.time() * 1000)),
            }
            last_error: Optional[Exception] = None
            for url in self.CLIST_URLS:
                try:
                    payload = await self._request_url_json(
                        url,
                        params=params,
                        proxies=proxies,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            else:
                raise RuntimeError(f"eastmoney stock list page failed: {last_error!r}")
            data = payload.get("data") or {}
            diff = data.get("diff") or []
            if not isinstance(diff, list):
                raise RuntimeError(f"eastmoney stock list diff is not list: {diff!r}")
            if not diff:
                break
            fetched_count += len(diff)

            for item in diff:
                if not isinstance(item, dict):
                    continue
                item_trade_date = self._extract_traded_stock_date(item)
                if item_trade_date and (
                    latest_trade_date is None or item_trade_date > latest_trade_date
                ):
                    latest_trade_date = item_trade_date
                code = item.get("f12")
                name = item.get("f14")
                if (
                    code
                    and name
                    and not self._is_delisting_stock_name(str(name))
                    and self._is_target_date_traded_stock(
                        item,
                        target_trade_date=target_trade_date,
                    )
                ):
                    rows.append(
                        {
                            "代码": str(code).strip().zfill(6),
                            "名称": str(name).strip(),
                        }
                    )

            total = int(data.get("total") or len(rows))
            if fetched_count >= total:
                break
            page += 1
            await asyncio.sleep(0.1)

        if not rows:
            result = pd.DataFrame(columns=["代码", "名称"])
        else:
            result = pd.DataFrame(rows).drop_duplicates(
                subset=["代码"],
                keep="first",
            )
        result.attrs["latest_trade_date"] = latest_trade_date
        return result

    @staticmethod
    def _is_delisting_stock_name(name: str) -> bool:
        """判断股票名称是否使用“退市”前缀或“退”后缀标记已退市证券。"""
        normalized_name = str(name).strip()
        return normalized_name.startswith("退市") or normalized_name.endswith("退")

    @staticmethod
    def _extract_traded_stock_date(item: Dict[str, Any]) -> Optional[str]:
        """从列表项提取最近有效成交日期，量额或更新时间无效时返回 ``None``。"""
        try:
            volume = float(item.get("f5"))
            amount = float(item.get("f6"))
            updated_timestamp = int(item.get("f124"))
        except (TypeError, ValueError):
            return None
        if volume <= 0 or amount <= 0 or updated_timestamp <= 0:
            return None
        return datetime.fromtimestamp(
            updated_timestamp,
            tz=CN_TZ,
        ).strftime("%Y-%m-%d")

    @staticmethod
    def _is_target_date_traded_stock(
        item: Dict[str, Any],
        *,
        target_trade_date: Optional[str],
    ) -> bool:
        """判断列表项是否在目标交易日有有效成交；未指定日期时全部通过。"""
        if target_trade_date is None:
            return True
        return (
            EastMoneyDataFetcher._extract_traded_stock_date(item)
            == target_trade_date
        )

class StockDailyDetailCrawler:
    """协调东方财富股票清单与日线数据，构造完整日线详情模型。

    日线、指标和筹码只通过逆向 HTTP 协议获取。该层负责统一字段、合并指标与
    筹码并建立来源元数据，但不负责持久化。
    """

    #: 逆向指标字典允许合并进日线表的完整字段白名单。
    INDICATOR_COLUMNS = (
        "ma5",
        "ma10",
        "ma20",
        "ma30",
        "ma60",
        "ma120",
        "ma250",
        "vol_ma5",
        "vol_ma10",
        "vol_ma20",
        "vol_ma60",
        "macd_dif",
        "macd_dea",
        "macd_hist",
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "rsi6",
        "rsi12",
        "rsi24",
        "boll_mid",
        "boll_upper",
        "boll_lower",
        "cci14",
        "wr6",
        "wr10",
        "wr14",
        "atr14",
    )

    def __init__(
        self,
        request_sleep_seconds: float = 0.5,
        max_retry: int = 3,
        proxy_provider: Optional[AsyncProxyProvider] = None,
        proxy_minutes: int = 3,
        reverse_fetcher: Optional[EastMoneyReverseFetcher] = None,
        proxy_rate_limiter: Optional[AsyncRequestRateLimiter] = None,
    ) -> None:
        """初始化重试、代理和数据抓取依赖。

        外部注入的逆向抓取器不由本实例关闭。股票清单和交易日接口保留独立的
        本地优先、代理回退逻辑；逐股日线只走逆向抓取器。
        """
        self.request_sleep_seconds = request_sleep_seconds  #: 业务请求后的基础节流秒数。
        self.max_retry = max(1, max_retry)  #: 每类代理请求至少执行一次的最大尝试数。
        self.proxy_provider = proxy_provider  #: 可注入、也可延迟创建的异步代理提供器。
        self._owns_proxy_provider = proxy_provider is None  #: 是否由本实例负责关闭代理提供器。
        self.proxy_minutes = proxy_minutes  #: 延迟创建 51 代理池时使用的固定 IP 有效分钟数。
        self.em_fetcher = EastMoneyDataFetcher()  #: 获取股票清单和交易日 JSON 的轻量客户端。
        self._owns_reverse_fetcher = reverse_fetcher is None
        self.reverse_fetcher = reverse_fetcher
        self.proxy_rate_limiter = proxy_rate_limiter or AsyncRequestRateLimiter()  #: 创建代理池时复用的 API 限流器。

    async def close(self) -> None:
        """关闭本实例拥有的逆向抓取器、JSON 客户端和代理提供器。"""
        if self._owns_reverse_fetcher and self.reverse_fetcher is not None:
            await self.reverse_fetcher.close()
        await self.em_fetcher.close()
        if self._owns_proxy_provider and self.proxy_provider is not None:
            await self.proxy_provider.close()

    def _get_proxy_provider(self) -> AsyncProxyProvider:
        """返回现有代理提供器，或延迟创建容量为一的异步 51 代理池。"""
        if self.proxy_provider is None:
            self.proxy_provider = AsyncDailiProxyPool(
                minutes=self.proxy_minutes,
                pool_size=1,
                rate_limiter=self.proxy_rate_limiter,
            )
        return self.proxy_provider

    async def _notify_proxy_success(
        self,
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        """兼容通知单代理或租约型代理池本次请求成功。

        优先调用带具体映射的 ``on_success_for``；否则回退协议的 ``on_success``，
        并在实现返回 awaitable 时等待完成。
        """
        if self.proxy_provider is not None:
            on_success_for = getattr(self.proxy_provider, "on_success_for", None)
            if callable(on_success_for):
                result = on_success_for(proxies)
            else:
                result = self.proxy_provider.on_success()
            if inspect.isawaitable(result):
                await result

    async def _notify_proxy_failure(
        self,
        exc: Exception,
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        """兼容通知单代理或租约型代理池本次请求失败及异常原因。

        带映射通知可准确归还对应池槽位；传统实现则接收普通失败回调。异步回调
        会被等待，确保下一次重试前代理状态已经更新。
        """
        if self.proxy_provider is not None:
            on_failure_for = getattr(self.proxy_provider, "on_failure_for", None)
            if callable(on_failure_for):
                result = on_failure_for(proxies, exc)
            else:
                result = self.proxy_provider.on_failure(exc)
            if inspect.isawaitable(result):
                await result

    async def _call_with_retry(
        self,
        api_name: str,
        fetcher: Callable[[Optional[Dict[str, str]]], Awaitable[_T]],
    ) -> _T:
        """先以本地网络调用抓取函数，失败后按指数退避切换代理重试。

        ``fetcher`` 接收 ``None`` 表示直连，接收映射表示代理请求。每次代理结果
        都回报提供器；耗尽次数后用最后异常构造包含 API 名的 ``RuntimeError``。
        """
        try:
            return await fetcher(None)
        except Exception as exc:
            last_error: Optional[Exception] = exc
            logger.warning(
                "eastmoney_request_local_failed api=%s error=%s switch=proxy",
                api_name,
                repr(exc),
            )

        for attempt in range(1, self.max_retry + 1):
            proxies: Optional[Dict[str, str]] = None
            try:
                proxies = await get_required_async_proxies(self._get_proxy_provider())
                result = await fetcher(proxies)
                await self._notify_proxy_success(proxies)
                return result
            except Exception as exc:
                last_error = exc
                await self._notify_proxy_failure(exc, proxies)
                if attempt < self.max_retry:
                    sleep_seconds = min(2**attempt, 10) + random.random()
                    logger.warning(
                        "%s_failed attempt=%s/%s error=%s sleep=%.2fs",
                        api_name,
                        attempt,
                        self.max_retry,
                        repr(exc),
                        sleep_seconds,
                    )
                    await asyncio.sleep(sleep_seconds)

        raise RuntimeError(
            f"{api_name} failed after {self.max_retry} attempts, error={last_error!r}"
        )

    async def fetch_stock_list(
        self,
        *,
        target_trade_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """获取并校验 A 股代码名称清单，可限定为目标交易日有成交的股票。

        结果只保留“代码”“名称”两列，代码统一为六位并去重；底层 DataFrame
        的来源属性会复制到规范化结果。
        """
        async def fetch_eastmoney_list(
            proxies: Optional[Dict[str, str]],
        ) -> pd.DataFrame:
            """执行一次股票清单请求，并把空结果转为可触发重试的异常。"""
            result = await self.em_fetcher.fetch_stock_list(
                proxies=proxies,
                target_trade_date=target_trade_date,
            )
            if result is None or result.empty:
                raise RuntimeError("eastmoney stock list returned empty dataframe")
            return result

        dataframe = await self._call_with_retry(
            "eastmoney_stock_list",
            fetch_eastmoney_list,
        )
        source_attrs = dict(dataframe.attrs)
        missing = [
            column for column in ("代码", "名称") if column not in dataframe.columns
        ]
        if missing:
            raise RuntimeError(f"stock list missing columns={missing}")

        result = dataframe[["代码", "名称"]].copy()
        result["代码"] = result["代码"].apply(self._normalize_code)
        result["名称"] = result["名称"].astype(str)
        result = result.drop_duplicates(subset=["代码"], keep="first")
        result.attrs.update(source_attrs)
        return result

    async def fetch_trade_dates(
        self,
        start_date: str,
        end_date: str,
    ) -> tuple[str, ...]:
        """按本地优先和代理回退策略获取日期范围内的非空交易日元组。"""
        async def fetch_non_empty_dates(
            proxies: Optional[Dict[str, str]],
        ) -> tuple[str, ...]:
            """执行一次交易日请求，并把空日历转为可触发重试的异常。"""
            result = await self.em_fetcher.fetch_trade_dates(
                start_date=start_date,
                end_date=end_date,
                proxies=proxies,
            )
            if not result:
                raise RuntimeError("eastmoney trade calendar returned no dates")
            return result

        dates = await self._call_with_retry(
            "eastmoney_trade_calendar",
            fetch_non_empty_dates,
        )
        return dates

    async def fetch_stock_daily_hist(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """通过逆向 HTTP 协议抓取指定区间的日 K、指标和筹码数据。

        IP 复用、轮换、连接恢复与熔断由目标感知管理器负责。
        """
        normalized_code = self._normalize_code(code)
        if self.reverse_fetcher is None:
            self.reverse_fetcher = EastMoneyReverseFetcher()
        return await self.reverse_fetcher.fetch_kline(
            code=normalized_code,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )

    async def build_stock_daily_details(
        self,
        code: str,
        name: Optional[str],
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
        write_start_date: Optional[str] = None,
    ) -> List[StockDailyDetail]:
        """抓取并组装一只股票日期范围内的完整 ``StockDailyDetail`` 列表。

        基础日 K 先统一列名和类型，再按交易日合并逆向技术指标和筹码数据；
        ``write_start_date`` 只影响最终模型输出范围，来源、参考 URL 和网络出口
        会写入每条记录的审计字段。
        """
        code = self._normalize_code(code)
        raw_daily_df = await self.fetch_stock_daily_hist(
            code=code,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        if raw_daily_df.empty:
            return []

        daily_df = self._standardize_daily_df(raw_daily_df, code=code, name=name)
        daily_with_indicators = self._merge_indicators(
            daily_df,
            raw_daily_df.attrs.get("indicator_rows") or {},
        )
        chip_map = self._build_chip_map(raw_daily_df.attrs.get("chip_rows") or {})
        daily_source = str(
            raw_daily_df.attrs.get("source") or EastMoneyReverseFetcher.SOURCE
        )
        page_url = str(
            raw_daily_df.attrs.get("page_url")
            or EastMoneyReverseFetcher.get_daily_page_url(code)
        )
        network = raw_daily_df.attrs.get("network")
        indicator_source = str(
            raw_daily_df.attrs.get("indicator_source")
            or EastMoneyReverseFetcher.INDICATOR_SOURCE
        )
        chip_source = str(
            raw_daily_df.attrs.get("chip_source")
            or EastMoneyReverseFetcher.CHIP_SOURCE
        )
        return self._build_models(
            daily_with_indicators,
            chip_map=chip_map,
            adjust=adjust,
            write_start_date=write_start_date,
            daily_source=daily_source,
            page_url=page_url,
            network=str(network) if network else None,
            indicator_source=indicator_source,
            chip_source=chip_source,
        )

    def _standardize_daily_df(
        self,
        raw_df: pd.DataFrame,
        *,
        code: str,
        name: Optional[str],
    ) -> pd.DataFrame:
        """校验并把东方财富中文日 K 列转换为内部英文列和交易日期字段。

        缺少必需列立即报错；日期按升序排列，股票代码和名称由调用上下文覆盖，
        所有行情值使用 pandas 宽松数值转换。
        """
        rename_columns = {
            "日期": "date",
            "股票代码": "code",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude_pct",
            "涨跌幅": "pct_chg",
            "涨跌额": "change_amount",
            "换手率": "turnover_pct",
        }
        missing = [column for column in rename_columns if column not in raw_df.columns]
        if missing:
            raise RuntimeError(f"daily df missing columns={missing}")

        dataframe = raw_df.rename(columns=rename_columns).copy()
        dataframe["date"] = pd.to_datetime(dataframe["date"])
        dataframe["trade_date"] = dataframe["date"].dt.strftime("%Y-%m-%d")
        dataframe["trade_date_int"] = (
            dataframe["date"].dt.strftime("%Y%m%d").astype(int)
        )
        dataframe["code"] = code
        dataframe["name"] = name

        for column in (
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
            "amplitude_pct",
            "pct_chg",
            "change_amount",
            "turnover_pct",
        ):
            dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")
        return dataframe.sort_values("date").reset_index(drop=True)

    def _merge_indicators(
        self,
        dataframe: pd.DataFrame,
        indicator_rows: Dict[str, Dict[str, Any]],
    ) -> pd.DataFrame:
        """按交易日把逆向指标白名单合并进基础日线表副本。

        所有指标列先置空，逆向结果中的值经有限浮点数规范化后逐格写入，未生成
        的指标保持 ``None``，不会以本地计算值替代。
        """
        result = dataframe.copy()
        for column in self.INDICATOR_COLUMNS:
            result[column] = None
        for index, row in result.iterrows():
            indicator_values = indicator_rows.get(str(row["trade_date"])) or {}
            for column in self.INDICATOR_COLUMNS:
                if column in indicator_values:
                    result.at[index, column] = self._normalize_float(
                        indicator_values[column]
                    )
        return result

    def _build_chip_map(
        self,
        rows: Dict[str, Dict[str, Any]],
    ) -> Dict[str, ChipDistribution]:
        """把逆向筹码字典转换为按交易日索引的类型化筹码分布模型。

        曲线横纵坐标必须同时存在且长度相等才建立 ``ChipChart``；比例、成本和
        集中度分别按既定精度舍入，无效数值保留为空。
        """
        chip_map: Dict[str, ChipDistribution] = {}
        for trade_date, row in rows.items():
            chart_x = self._normalize_number_list(row.get("chart_x"))
            chart_y = self._normalize_number_list(row.get("chart_y"))
            chart = (
                ChipChart(x=chart_x, y=chart_y)
                if chart_x and len(chart_x) == len(chart_y)
                else None
            )
            chip_map[str(trade_date)] = ChipDistribution(
                profit_ratio=self._round_float(row.get("profit_ratio"), digits=6),
                avg_cost=self._round_float(row.get("avg_cost")),
                cost_90=ChipCostRange(
                    low=self._round_float(row.get("cost_90_low")),
                    high=self._round_float(row.get("cost_90_high")),
                    concentration=self._round_float(
                        row.get("cost_90_concentration"), digits=6
                    ),
                ),
                cost_70=ChipCostRange(
                    low=self._round_float(row.get("cost_70_low")),
                    high=self._round_float(row.get("cost_70_high")),
                    concentration=self._round_float(
                        row.get("cost_70_concentration"), digits=6
                    ),
                ),
                chart=chart,
            )
        return chip_map

    def _build_models(
        self,
        dataframe: pd.DataFrame,
        *,
        chip_map: Dict[str, ChipDistribution],
        adjust: str,
        write_start_date: Optional[str],
        daily_source: str,
        page_url: str,
        network: Optional[str],
        indicator_source: str,
        chip_source: str,
    ) -> List[StockDailyDetail]:
        """逐行构造最终日线详情模型，并附加指标、筹码与数据来源审计信息。

        早于 ``write_start_date`` 的行跳过；每个数值按业务精度规范化，没有当日
        筹码时明确写入 ``unavailable`` 来源标识。
        """
        items: List[StockDailyDetail] = []
        for row in dataframe.to_dict("records"):
            trade_date = str(row["trade_date"])
            if write_start_date and trade_date < write_start_date:
                continue
            chip = chip_map.get(trade_date)
            items.append(
                StockDailyDetail(
                    trade_date=trade_date,
                    trade_date_int=int(row["trade_date_int"]),
                    code=self._normalize_code(row["code"]),
                    name=row.get("name"),
                    adjust=adjust,
                    open=self._round_float(row.get("open")),
                    close=self._round_float(row.get("close")),
                    high=self._round_float(row.get("high")),
                    low=self._round_float(row.get("low")),
                    volume=self._normalize_int(row.get("volume")),
                    amount=self._round_float(row.get("amount"), digits=2),
                    amplitude_pct=self._round_float(row.get("amplitude_pct")),
                    pct_chg=self._round_float(row.get("pct_chg")),
                    change_amount=self._round_float(row.get("change_amount")),
                    turnover_pct=self._round_float(row.get("turnover_pct")),
                    ma=MAIndicators(
                        ma5=self._round_float(row.get("ma5")),
                        ma10=self._round_float(row.get("ma10")),
                        ma20=self._round_float(row.get("ma20")),
                        ma30=self._round_float(row.get("ma30")),
                        ma60=self._round_float(row.get("ma60")),
                        ma120=self._round_float(row.get("ma120")),
                        ma250=self._round_float(row.get("ma250")),
                    ),
                    volume_ma=VolumeMAIndicators(
                        vol_ma5=self._round_float(row.get("vol_ma5"), digits=2),
                        vol_ma10=self._round_float(row.get("vol_ma10"), digits=2),
                        vol_ma20=self._round_float(row.get("vol_ma20"), digits=2),
                        vol_ma60=self._round_float(row.get("vol_ma60"), digits=2),
                    ),
                    macd=MACDIndicators(
                        dif=self._round_float(row.get("macd_dif")),
                        dea=self._round_float(row.get("macd_dea")),
                        hist=self._round_float(row.get("macd_hist")),
                    ),
                    kdj=KDJIndicators(
                        k=self._round_float(row.get("kdj_k")),
                        d=self._round_float(row.get("kdj_d")),
                        j=self._round_float(row.get("kdj_j")),
                    ),
                    rsi=RSIIndicators(
                        rsi6=self._round_float(row.get("rsi6")),
                        rsi12=self._round_float(row.get("rsi12")),
                        rsi24=self._round_float(row.get("rsi24")),
                    ),
                    boll=BOLLIndicators(
                        mid=self._round_float(row.get("boll_mid")),
                        upper=self._round_float(row.get("boll_upper")),
                        lower=self._round_float(row.get("boll_lower")),
                    ),
                    cci=CCIIndicators(cci14=self._round_float(row.get("cci14"))),
                    wr=WRIndicators(
                        wr6=self._round_float(row.get("wr6")),
                        wr10=self._round_float(row.get("wr10")),
                        wr14=self._round_float(row.get("wr14")),
                    ),
                    atr=ATRIndicators(atr14=self._round_float(row.get("atr14"))),
                    chip=chip,
                    source=StockDailyDetailSource(
                        daily=daily_source,
                        page_url=page_url,
                        network=network,
                        indicator=indicator_source,
                        chip=(
                            chip_source if chip is not None else "unavailable"
                        ),
                    ),
                )
            )
        return items

    async def sleep_after_request(self) -> None:
        """按基础节流时间加最多 0.2 秒随机抖动异步等待，分散连续请求。"""
        await asyncio.sleep(self.request_sleep_seconds + random.random() * 0.2)

    @staticmethod
    def _normalize_code(value: Any) -> str:
        """把任意股票代码值去空白并左补零为六位字符串。"""
        return str(value).strip().zfill(6)

    @staticmethod
    def _normalize_float(value: Any) -> Optional[float]:
        """把值转换为有限浮点数；空值、NaN、无穷或非法文本返回 ``None``。"""
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _round_float(
        cls,
        value: Any,
        digits: int = 4,
    ) -> Optional[float]:
        """规范化浮点输入并按 ``digits`` 位小数舍入，无效值保持 ``None``。"""
        number = cls._normalize_float(value)
        return None if number is None else round(number, digits)

    @classmethod
    def _normalize_int(cls, value: Any) -> Optional[int]:
        """把有效有限数值截断转换为整数，无效值返回 ``None``。"""
        number = cls._normalize_float(value)
        return None if number is None else int(number)

    @classmethod
    def _normalize_number_list(cls, value: Any) -> List[float]:
        """从列表中保留可转换的有限浮点数；非列表输入返回空列表。"""
        if not isinstance(value, list):
            return []
        return [
            number
            for item in value
            if (number := cls._normalize_float(item)) is not None
        ]
