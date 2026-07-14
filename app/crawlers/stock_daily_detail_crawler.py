from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, TypeVar

import httpx
import pandas as pd

from app.crawlers.proxy_provider import (
    AsyncProxyProvider,
    AsyncRequestRateLimiter,
    AsyncShanchenProxyProvider,
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


class NonRetryablePageError(RuntimeError):
    """A deterministic page error that changing the network cannot fix."""


class EastMoneyDataFetcher:
    """Fetch EastMoney stock-list/calendar reference data."""

    KLINE_URLS = (
        "https://push2.eastmoney.com/api/qt/stock/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    )
    CLIST_URLS = (
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
    )
    CLIST_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
    CLIST_FIELDS = "f12,f14,f2,f5,f6,f124"
    DEFAULT_TIMEOUT = (5, 12)
    KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
    KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    UT = "7eea3edcaed734bea9cbfc24409ed989"
    CLIST_UT = "bd1d9ddb04089700cf9c27f6f7426281"

    DAILY_COLUMNS = [
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
        "振幅",
        "涨跌幅",
        "涨跌额",
        "换手率",
    ]

    def __init__(
        self,
        *,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    ) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        }
        self._clients: Dict[Optional[str], httpx.AsyncClient] = {}

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def _get_client(
        self,
        proxies: Optional[Dict[str, str]],
    ) -> httpx.AsyncClient:
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
        normalized_code = str(code).strip().zfill(6)
        market_code = 1 if normalized_code.startswith("6") else 0
        return f"{market_code}.{normalized_code}"

    @staticmethod
    def _get_fqt(adjust: str) -> str:
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
        normalized_name = str(name).strip()
        return normalized_name.startswith("退市") or normalized_name.endswith("退")

    @staticmethod
    def _extract_traded_stock_date(item: Dict[str, Any]) -> Optional[str]:
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
        if target_trade_date is None:
            return True
        return (
            EastMoneyDataFetcher._extract_traded_stock_date(item)
            == target_trade_date
        )

    @classmethod
    def _kline_lines_to_daily_df(
        cls,
        lines: Sequence[str],
        *,
        code: str,
    ) -> pd.DataFrame:
        if not lines:
            return pd.DataFrame(columns=[*cls.DAILY_COLUMNS, "股票代码"])

        rows = [line.split(",") for line in lines]
        bad_row = next(
            (row for row in rows if len(row) != len(cls.DAILY_COLUMNS)),
            None,
        )
        if bad_row is not None:
            raise RuntimeError(
                "eastmoney kline row width mismatch, "
                f"expected={len(cls.DAILY_COLUMNS)}, actual={len(bad_row)}, row={bad_row!r}"
            )

        dataframe = pd.DataFrame(rows, columns=cls.DAILY_COLUMNS)
        dataframe["股票代码"] = str(code).strip().zfill(6)
        dataframe["日期"] = pd.to_datetime(dataframe["日期"], errors="coerce").dt.date
        for column in cls.DAILY_COLUMNS[1:]:
            dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")
        return dataframe


class EastMoneyQuotePageFetcher:
    """Read daily, indicator and chip data from EastMoney page runtimes."""

    SOURCE = "eastmoney.quote_page"
    RUNTIME_SOURCE = "eastmoney.quote_page.runtime"
    LOCAL_TIMEOUT_MS = 20_000
    PROXY_TIMEOUT_MS = 20_000
    LOCAL_TOTAL_TIMEOUT_SECONDS = 55
    PROXY_TOTAL_TIMEOUT_SECONDS = 50
    ADJUST_LABELS = {"qfq": "前复权", "hfq": "后复权", "": "不复权"}
    CHART_HOOK = r"""
        (() => {
          window.__eastmoneyCharts = [];
          const wrap = (value) => {
            if (!value || typeof value !== "object" || value.__stockCrawlerHooked) {
              return value;
            }
            if (typeof value.k === "function" && !value.k.__stockCrawlerHooked) {
              const OriginalChart = value.k;
              const WrappedChart = function(...args) {
                const chart = new OriginalChart(...args);
                window.__eastmoneyCharts.push(chart);
                return chart;
              };
              WrappedChart.prototype = OriginalChart.prototype;
              Object.defineProperty(WrappedChart, "__stockCrawlerHooked", {value: true});
              value.k = WrappedChart;
            }
            try {
              Object.defineProperty(value, "__stockCrawlerHooked", {value: true});
            } catch (_) {}
            return value;
          };
          let quoteKChart;
          try {
            Object.defineProperty(window, "quotekchart", {
              configurable: true,
              get() { return quoteKChart; },
              set(value) { quoteKChart = wrap(value); }
            });
          } catch (_) {}
        })();
    """

    CONCEPT_HOOK = r"""
        (() => {
          const capture = window.__eastmoneyConceptCapture = {
            klines: [],
            chipFills: [],
            network: [],
            errors: []
          };
          const rememberNetwork = (url, body, kind) => {
            try {
              const text = typeof body === "string" ? body : JSON.stringify(body);
              if (!text || text.length > 2500000) return;
              capture.network.push({url: String(url || ""), kind, body: text});
              if (capture.network.length > 120) capture.network.shift();
            } catch (error) {
              capture.errors.push(String(error));
            }
          };

          if (typeof window.fetch === "function") {
            const originalFetch = window.fetch;
            window.fetch = async function(...args) {
              const response = await originalFetch.apply(this, args);
              try {
                response.clone().text().then(
                  text => rememberNetwork(response.url, text, "fetch")
                );
              } catch (_) {}
              return response;
            };
          }
          if (window.XMLHttpRequest) {
            const originalOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {
              this.__eastmoneyUrl = url;
              return originalOpen.call(this, method, url, ...rest);
            };
            const originalSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function(...args) {
              this.addEventListener("loadend", () => {
                try {
                  rememberNetwork(
                    this.responseURL || this.__eastmoneyUrl,
                    this.responseText,
                    "xhr"
                  );
                } catch (_) {}
              });
              return originalSend.apply(this, args);
            };
          }

          const wrapLibrary = value => {
            if (!value || typeof value !== "object" || value.__crawlerWrapped) {
              return value;
            }
            const Original = value.kline;
            if (typeof Original === "function" && !Original.__crawlerWrapped) {
              const Wrapped = function(...args) {
                const Target = new.target === Wrapped ? Original : new.target;
                const chart = Reflect.construct(Original, args, Target || Original);
                capture.klines.push(chart);
                return chart;
              };
              Object.setPrototypeOf(Wrapped, Original);
              Wrapped.prototype = Original.prototype;
              Object.defineProperty(Wrapped, "__crawlerWrapped", {value: true});
              value.kline = Wrapped;
            }
            Object.defineProperty(value, "__crawlerWrapped", {value: true});
            return value;
          };
          let quoteChart;
          try {
            Object.defineProperty(window, "quotechart2022", {
              configurable: true,
              get() { return quoteChart; },
              set(value) { quoteChart = wrapLibrary(value); }
            });
          } catch (_) {}

          const proto = window.CanvasRenderingContext2D?.prototype;
          if (!proto) return;
          const paths = new WeakMap();
          const originalBeginPath = proto.beginPath;
          const originalMoveTo = proto.moveTo;
          const originalLineTo = proto.lineTo;
          const originalFill = proto.fill;
          proto.beginPath = function(...args) {
            paths.set(this, []);
            return originalBeginPath.apply(this, args);
          };
          proto.moveTo = function(x, y, ...args) {
            const path = paths.get(this);
            if (path) path.push({op: "M", x, y});
            return originalMoveTo.call(this, x, y, ...args);
          };
          proto.lineTo = function(x, y, ...args) {
            const path = paths.get(this);
            if (path) path.push({op: "L", x, y});
            return originalLineTo.call(this, x, y, ...args);
          };
          proto.fill = function(...args) {
            try {
              if (this.canvas?.closest(".quotechart2022_c_cyq")) {
                capture.chipFills.push(
                  (paths.get(this) || []).map(point => ({...point}))
                );
              }
            } catch (_) {}
            return originalFill.apply(this, args);
          };
        })();
    """

    CONCEPT_RUNTIME_JS = r"""
        async ({startDate, endDate, chipHistoryDays}) => {
          const capture = window.__eastmoneyConceptCapture;
          const chart = [...(capture?.klines || [])].reverse().find(
            item => item?.data?.full_klines?.length
          );
          if (!chart) throw new Error("未找到东方财富概念页 K 线运行时对象");

          const pause = () => new Promise(resolve => requestAnimationFrame(resolve));
          const number = value => {
            if (value === null || value === undefined || value === "-") return null;
            const parsed = Number(String(value).replace(/[% ,]/g, ""));
            return Number.isFinite(parsed) ? parsed : null;
          };
          const indicatorRows = {};
          const mergeSeries = (series, fields) => {
            for (const row of series || []) {
              if (!Array.isArray(row) || !row[0]) continue;
              const target = indicatorRows[String(row[0])] ||= {};
              fields.forEach((field, index) => {
                const parsed = number(row[index + 1]);
                if (parsed !== null) target[field] = parsed;
              });
            }
          };
          const clickIndicator = (selector, label) => {
            const element = Array.from(document.querySelectorAll(selector)).find(
              item => item.textContent.trim() === label
            );
            if (!element) return false;
            element.click();
            return true;
          };

          const buttons = {};
          buttons.MA = clickIndicator(".main_zb li", "均线");
          await pause();
          mergeSeries(chart.common_data.main_indicator_data_source, [
            "ma5", "ma10", "ma20", "ma30", "ma60"
          ]);
          mergeSeries(chart.common_data.volume_ma_data_source, [
            "vol_ma5", "vol_ma10"
          ]);

          buttons.BOLL = clickIndicator(".main_zb li", "BOLL");
          await pause();
          mergeSeries(chart.common_data.main_indicator_data_source, [
            "boll_mid", "boll_upper", "boll_lower"
          ]);

          const secondary = {
            RSI: ["rsi6", "rsi12", "rsi24"],
            KDJ: ["kdj_k", "kdj_d", "kdj_j"],
            MACD: ["macd_dif", "macd_dea", "macd_hist"],
            WR: ["wr10", "wr6"],
            CCI: ["cci14"]
          };
          for (const [name, fields] of Object.entries(secondary)) {
            buttons[name] = clickIndicator(".f_zb li", name);
            await pause();
            mergeSeries(chart.common_data.indicator_data_source, fields);
          }

          const parsePercent = value => {
            const parsed = number(value);
            return parsed === null ? null : parsed / 100;
          };
          const parseRange = value => {
            const matches = String(value || "").match(/\d+(?:\.\d+)?/g) || [];
            return matches.slice(0, 2).map(Number);
          };
          const chipRows = {};
          const klines = chart.data.full_klines || [];
          const indexes = klines
            .map((item, index) => ({date: String(item.date), index}))
            .filter(item => item.date >= startDate && item.date <= endDate)
            .slice(-chipHistoryDays);

          for (const item of indexes) {
            capture.chipFills = [];
            chart.event.trigger("change_data_index", item.index);
            const values = Array.from(
              document.querySelectorAll(
                ".quotechart2022_c_cyq_info .qcyq_t_v"
              )
            ).map(element => element.textContent.trim());
            if (values.length < 7 || values[0] !== item.date) continue;

            const paths = capture.chipFills.slice(-2);
            const red = (paths[0] || []).slice(0, -2);
            const blue = (paths[1] || []).slice(0, -3);
            const points = [...red, ...blue].filter(point => point.op === "L");
            const range90 = parseRange(values[3]);
            const range70 = parseRange(values[5]);
            const row = {
              profit_ratio: parsePercent(values[1]),
              avg_cost: number(values[2]),
              cost_90_low: range90[0] ?? null,
              cost_90_high: range90[1] ?? null,
              cost_90_concentration: parsePercent(values[4]),
              cost_70_low: range70[0] ?? null,
              cost_70_high: range70[1] ?? null,
              cost_70_concentration: parsePercent(values[6])
            };
            if (points.length === 150) {
              row.chart_x = points.map(point => Number(point.x.toFixed(12)));
              row.chart_y = points.map(point => Number((
                chart.common_data.y_max - point.y * chart.common_data.y_scale
              ).toFixed(4)));
            }
            chipRows[item.date] = row;
          }

          return {
            dailyRows: klines.map(item => ({
              date: String(item.date),
              open: number(item.open),
              close: number(item.close),
              high: number(item.high),
              low: number(item.low),
              volume: number(item.volume),
              amount: number(item.volume_money),
              amplitude: number(item.zf),
              pctChange: number(item.zdf),
              changeAmount: number(item.zde),
              turnover: number(item.hsl)
            })),
            indicatorRows,
            chipRows,
            diagnostics: {
              conceptChartCount: capture.klines.length,
              conceptKlineCount: klines.length,
              conceptFirstDate: klines[0]?.date || null,
              conceptLastDate: klines[klines.length - 1]?.date || null,
              indicatorButtons: buttons,
              indicatorDateCount: Object.keys(indicatorRows).length,
              chipDateCount: Object.keys(chipRows).length,
              capturedFetchXhrCount: capture.network.length,
              capturedFetchXhr: capture.network.slice(-40).map(entry => ({
                url: entry.url,
                kind: entry.kind,
                bodyPreview: entry.body.slice(0, 1000)
              })),
              runtimeErrors: capture.errors
            }
          };
        }
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        strict_page_indicators: bool = True,
        strict_page_chip: bool = True,
    ) -> None:
        self.headless = headless
        self.strict_page_indicators = strict_page_indicators
        self.strict_page_chip = strict_page_chip
        self._playwright: Any = None
        self._browser: Any = None
        self._browser_lock = asyncio.Lock()
        self.last_runtime_diagnostics: Dict[str, Any] = {}

    @staticmethod
    def get_symbol(code: str) -> str:
        normalized_code = str(code).strip().zfill(6)
        if normalized_code.startswith(("4", "8", "9")):
            market = "bj"
        elif normalized_code.startswith("6"):
            market = "sh"
        else:
            market = "sz"
        return f"{market}{normalized_code}"

    @classmethod
    def get_quote_url(cls, code: str) -> str:
        return f"https://quote.eastmoney.com/{cls.get_symbol(code)}.html"

    @classmethod
    def get_concept_url(cls, code: str) -> str:
        return (
            f"https://quote.eastmoney.com/concept/{cls.get_symbol(code)}.html"
            "#chart-k-cyq"
        )

    @classmethod
    def get_daily_page_url(cls, code: str) -> str:
        if cls.get_symbol(code).startswith("bj"):
            return cls.get_concept_url(code)
        return cls.get_quote_url(code)

    @staticmethod
    def _raise_for_page_response(url: str, response: Any, label: str) -> None:
        status = None if response is None else response.status
        if response is not None and response.status == 200:
            return
        message = f"{label}响应异常: url={url}, status={status}"
        if status in {404, 410}:
            raise NonRetryablePageError(message)
        raise RuntimeError(message)

    @staticmethod
    def _concept_daily_rows_to_df(
        rows: Sequence[Dict[str, Any]],
        *,
        code: str,
    ) -> pd.DataFrame:
        columns = {
            "date": "日期",
            "open": "开盘",
            "close": "收盘",
            "high": "最高",
            "low": "最低",
            "volume": "成交量",
            "amount": "成交额",
            "amplitude": "振幅",
            "pctChange": "涨跌幅",
            "changeAmount": "涨跌额",
            "turnover": "换手率",
        }
        if not rows:
            return pd.DataFrame(
                columns=[*EastMoneyDataFetcher.DAILY_COLUMNS, "股票代码"]
            )
        dataframe = pd.DataFrame(rows).rename(columns=columns)
        missing = [column for column in columns.values() if column not in dataframe]
        if missing:
            raise RuntimeError(f"东方财富概念页日 K 缺少字段: {missing}")
        dataframe = dataframe[list(columns.values())].copy()
        dataframe["股票代码"] = str(code).strip().zfill(6)
        dataframe["日期"] = pd.to_datetime(
            dataframe["日期"], errors="coerce"
        ).dt.date
        for column in EastMoneyDataFetcher.DAILY_COLUMNS[1:]:
            dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")
        return dataframe

    def dump_last_runtime_diagnostics(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                self.last_runtime_diagnostics,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _ensure_browser(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser

        async with self._browser_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "缺少 playwright，请在 MyAgent 环境安装 playwright 并执行 "
                    "`python -m playwright install chromium`"
                ) from exc

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless
            )
            return self._browser

    @staticmethod
    def _proxy_server(proxies: Optional[Dict[str, str]]) -> Optional[str]:
        if not proxies:
            return None
        server = proxies.get("https") or proxies.get("http")
        if not server:
            raise RuntimeError(f"代理配置缺少 http/https 地址: {proxies!r}")
        return server

    @staticmethod
    async def _abort_heavy_assets(route: Any) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    async def _fetch_standard_daily_data(
        self,
        context: Any,
        *,
        code: str,
        adjust: str,
        timeout_ms: int,
        record_response: Callable[[Any], None],
    ) -> pd.DataFrame:
        page = await context.new_page()
        await page.route("**/*", self._abort_heavy_assets)
        page.on("response", record_response)
        page.set_default_timeout(timeout_ms)
        url = self.get_quote_url(code)
        response = await page.goto(
            url,
            wait_until="commit",
            timeout=timeout_ms,
        )
        self._raise_for_page_response(url, response, "行情页")

        await page.wait_for_function(
            """
            () => window.__eastmoneyCharts && window.__eastmoneyCharts.some(
              chart => chart && chart.data && chart.data.k && chart.data.k.length
            )
            """,
            timeout=timeout_ms,
        )
        adjust_label = self.ADJUST_LABELS[adjust]
        current_label = (await page.locator(".kfq_t").inner_text()).strip()
        if current_label != adjust_label:
            changed = await page.evaluate(
                """
                label => {
                  const links = Array.from(document.querySelectorAll('.kfq ul li a'));
                  const target = links.find(link => link.textContent.trim() === label);
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """,
                adjust_label,
            )
            if not changed:
                raise RuntimeError(f"行情页未找到复权选项: {adjust_label}")
            await page.wait_for_function(
                "label => document.querySelector('.kfq_t')?.textContent.trim() === label",
                arg=adjust_label,
                timeout=timeout_ms,
            )
            await page.wait_for_timeout(500)

        rows = await page.evaluate(
            """
            async timeoutMs => {
              const charts = window.__eastmoneyCharts || [];
              const chart = [...charts].reverse().find(
                item => item && item.data && item.data.k && item.data.k.length
              );
              if (!chart || typeof chart.getOneAllData !== 'function') {
                throw new Error('未找到东方财富日 K 图表对象');
              }
              await Promise.race([
                chart.getOneAllData(),
                new Promise((_, reject) => setTimeout(
                  () => reject(new Error('获取全量日 K 超时')), timeoutMs
                ))
              ]);
              const rows = chart.full_data && chart.full_data.data;
              if (!Array.isArray(rows) || !rows.length) {
                throw new Error('东方财富行情页全量日 K 为空');
              }
              return rows;
            }
            """,
            timeout_ms,
        )
        if not all(isinstance(row, str) for row in rows):
            raise RuntimeError("东方财富行情页日 K 数据格式不是字符串数组")
        return EastMoneyDataFetcher._kline_lines_to_daily_df(rows, code=code)

    async def fetch_kline(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
        *,
        proxies: Optional[Dict[str, str]] = None,
    ) -> pd.DataFrame:
        if adjust not in self.ADJUST_LABELS:
            raise ValueError(f"unsupported adjust value: {adjust!r}")
        normalized_code = str(code).strip().zfill(6)
        is_bse = self.get_symbol(normalized_code).startswith("bj")
        start = pd.to_datetime(start_date, format="%Y%m%d", errors="raise").date()
        end = pd.to_datetime(end_date, format="%Y%m%d", errors="raise").date()
        if start > end:
            raise ValueError("start_date cannot be later than end_date")

        proxy_server = self._proxy_server(proxies)
        timeout_ms = self.PROXY_TIMEOUT_MS if proxy_server else self.LOCAL_TIMEOUT_MS
        total_timeout_seconds = (
            self.PROXY_TOTAL_TIMEOUT_SECONDS
            if proxy_server
            else self.LOCAL_TOTAL_TIMEOUT_SECONDS
        )
        browser = await self._ensure_browser()
        context_options: Dict[str, Any] = {
            "locale": "zh-CN",
            "service_workers": "block",
            "viewport": {"width": 1680, "height": 1050},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if proxy_server:
            context_options["proxy"] = {"server": proxy_server}

        context = await browser.new_context(**context_options)
        diagnostics: Dict[str, Any] = {
            "quote_url": self.get_quote_url(normalized_code),
            "concept_url": self.get_concept_url(normalized_code),
            "daily_page_url": self.get_daily_page_url(normalized_code),
            "network": "proxy" if proxy_server else "local",
            "responses": [],
        }
        indicator_rows: Dict[str, Dict[str, Any]] = {}
        chip_rows: Dict[str, Dict[str, Any]] = {}

        def record_response(response: Any) -> None:
            if len(diagnostics["responses"]) >= 300:
                return
            diagnostics["responses"].append(
                {
                    "status": response.status,
                    "resource_type": response.request.resource_type,
                    "url": response.url,
                }
            )

        try:
            async with asyncio.timeout(total_timeout_seconds):
                await context.add_init_script(self.CHART_HOOK)
                await context.add_init_script(self.CONCEPT_HOOK)
                daily_all: Optional[pd.DataFrame] = None
                if not is_bse:
                    daily_all = await self._fetch_standard_daily_data(
                        context,
                        code=normalized_code,
                        adjust=adjust,
                        timeout_ms=timeout_ms,
                        record_response=record_response,
                    )
                    diagnostics["daily_row_count"] = len(daily_all)

                concept_page = await context.new_page()
                await concept_page.route("**/*", self._abort_heavy_assets)
                concept_page.on("response", record_response)
                concept_page.set_default_timeout(timeout_ms)
                concept_url = self.get_concept_url(normalized_code)
                concept_response = await concept_page.goto(
                    concept_url,
                    wait_until="commit",
                    timeout=timeout_ms,
                )
                self._raise_for_page_response(
                    concept_url,
                    concept_response,
                    "东方财富概念页",
                )
                await concept_page.wait_for_function(
                    """
                () => window.__eastmoneyConceptCapture?.klines?.some(
                  chart => chart?.data?.full_klines?.length
                )
                """,
                    timeout=timeout_ms,
                )
                runtime = await concept_page.evaluate(
                    self.CONCEPT_RUNTIME_JS,
                    {
                        "startDate": start.strftime("%Y-%m-%d"),
                        "endDate": end.strftime("%Y-%m-%d"),
                        "chipHistoryDays": max(1, (end - start).days + 1),
                    },
                )
                indicator_rows = runtime.get("indicatorRows") or {}
                chip_rows = runtime.get("chipRows") or {}
                if is_bse:
                    daily_all = self._concept_daily_rows_to_df(
                        runtime.get("dailyRows") or [],
                        code=normalized_code,
                    )
                    diagnostics["daily_row_count"] = len(daily_all)
                diagnostics["runtime"] = runtime.get("diagnostics") or {}
                self.last_runtime_diagnostics = diagnostics

                if self.strict_page_indicators and not any(indicator_rows.values()):
                    raise NonRetryablePageError(
                        "东方财富网页运行时未解析到技术指标；已禁止本地计算。"
                        "请调用 dump_last_runtime_diagnostics() 保存诊断。"
                    )
                if self.strict_page_chip and not chip_rows:
                    raise NonRetryablePageError(
                        "东方财富网页运行时未解析到筹码详情/筹码图；已禁止本地计算。"
                        "请调用 dump_last_runtime_diagnostics() 保存诊断。"
                    )
                if daily_all is None or daily_all.empty:
                    raise NonRetryablePageError(
                        "东方财富网页运行时未解析到基础日 K 数据"
                    )
        except Exception as exc:
            diagnostics["error"] = repr(exc)
            self.last_runtime_diagnostics = diagnostics
            raise
        finally:
            await context.close()

        mask = (daily_all["日期"] >= start) & (daily_all["日期"] <= end)
        result = daily_all.loc[mask].reset_index(drop=True)
        result.attrs.update(
            {
                "source": self.SOURCE,
                "page_url": self.get_daily_page_url(normalized_code),
                "network": "proxy" if proxy_server else "local",
                "indicator_source": self.RUNTIME_SOURCE,
                "chip_source": self.RUNTIME_SOURCE,
                "indicator_rows": indicator_rows,
                "chip_rows": chip_rows,
                "runtime_diagnostics": diagnostics,
            }
        )
        return result


class LocalQuoteCircuitBreaker:
    def __init__(self) -> None:
        self.retry_after = 0.0
        self.local_verified = False
        self.failure_generation = 0
        self.probe_lock = asyncio.Lock()

    def can_try_local(self) -> bool:
        return time.monotonic() >= self.retry_after

    def can_use_verified_local(self) -> bool:
        return self.local_verified and self.can_try_local()

    def mark_success(self, failure_generation: int) -> None:
        if failure_generation != self.failure_generation:
            return
        self.local_verified = True
        self.retry_after = 0.0

    def mark_failure(self, cooldown_seconds: float) -> None:
        self.local_verified = False
        self.failure_generation += 1
        self.retry_after = time.monotonic() + cooldown_seconds


class StockDailyDetailCrawler:
    """Fetch stock daily details from EastMoney page output only."""

    LOCAL_RETRY_COOLDOWN_SECONDS = 300
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
        proxy_minutes: int = 1,
        quote_page_fetcher: Optional[EastMoneyQuotePageFetcher] = None,
        page_semaphore: Optional[asyncio.Semaphore] = None,
        local_circuit_breaker: Optional[LocalQuoteCircuitBreaker] = None,
        proxy_rate_limiter: Optional[AsyncRequestRateLimiter] = None,
        *,
        strict_page_indicators: bool = True,
        strict_page_chip: bool = True,
    ) -> None:
        self.request_sleep_seconds = request_sleep_seconds
        self.max_retry = max(1, max_retry)
        self.proxy_provider = proxy_provider
        self._owns_proxy_provider = proxy_provider is None
        self.proxy_minutes = proxy_minutes
        self.em_fetcher = EastMoneyDataFetcher()
        self._owns_quote_page_fetcher = quote_page_fetcher is None
        self.quote_page_fetcher = quote_page_fetcher or EastMoneyQuotePageFetcher(
            strict_page_indicators=strict_page_indicators,
            strict_page_chip=strict_page_chip,
        )
        self.page_semaphore = page_semaphore or asyncio.Semaphore(1)
        self.local_circuit_breaker = local_circuit_breaker or LocalQuoteCircuitBreaker()
        self.proxy_rate_limiter = proxy_rate_limiter or AsyncRequestRateLimiter()

    async def close(self) -> None:
        if self._owns_quote_page_fetcher:
            await self.quote_page_fetcher.close()
        await self.em_fetcher.close()
        if self._owns_proxy_provider and self.proxy_provider is not None:
            await self.proxy_provider.close()

    def _get_proxy_provider(self) -> AsyncProxyProvider:
        if self.proxy_provider is None:
            self.proxy_provider = AsyncShanchenProxyProvider(
                minutes=self.proxy_minutes,
                rate_limiter=self.proxy_rate_limiter,
            )
        return self.proxy_provider

    async def _notify_proxy_success(
        self,
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
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
        async def fetch_eastmoney_list(
            proxies: Optional[Dict[str, str]],
        ) -> pd.DataFrame:
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
        async def fetch_non_empty_dates(
            proxies: Optional[Dict[str, str]],
        ) -> tuple[str, ...]:
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
        normalized_code = self._normalize_code(code)
        local_attempted = False
        local_error: Optional[Exception] = None
        local_probe_owner = False
        try_local = self.local_circuit_breaker.can_use_verified_local()
        if not try_local and self.local_circuit_breaker.can_try_local():
            await self.local_circuit_breaker.probe_lock.acquire()
            if self.local_circuit_breaker.can_use_verified_local():
                self.local_circuit_breaker.probe_lock.release()
                try_local = True
            elif self.local_circuit_breaker.can_try_local():
                local_probe_owner = True
                try_local = True
            else:
                self.local_circuit_breaker.probe_lock.release()

        if try_local:
            local_attempted = True
            failure_generation = self.local_circuit_breaker.failure_generation
            try:
                async with self.page_semaphore:
                    dataframe = await self.quote_page_fetcher.fetch_kline(
                        code=normalized_code,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                    )
                    if dataframe.empty:
                        raise RuntimeError("行情页在指定日期范围内没有日 K 数据")
                self.local_circuit_breaker.mark_success(failure_generation)
                return dataframe
            except NonRetryablePageError:
                raise
            except Exception as exc:
                local_error = exc
                self.local_circuit_breaker.mark_failure(
                    self.LOCAL_RETRY_COOLDOWN_SECONDS
                )
            finally:
                if local_probe_owner:
                    self.local_circuit_breaker.probe_lock.release()

        if local_attempted:
            logger.warning(
                "eastmoney_quote_page_local_failed code=%s error=%s "
                "switch=proxy local_retry_after_seconds=%s",
                normalized_code,
                repr(local_error),
                self.LOCAL_RETRY_COOLDOWN_SECONDS,
            )
        else:
            retry_after_seconds = max(
                0.0,
                self.local_circuit_breaker.retry_after - time.monotonic(),
            )
            logger.info(
                "eastmoney_quote_page_local_skipped code=%s reason=cooldown "
                "retry_after_seconds=%.1f",
                normalized_code,
                retry_after_seconds,
            )

        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retry + 1):
            proxies: Optional[Dict[str, str]] = None
            try:
                async with self.page_semaphore:
                    proxies = await get_required_async_proxies(
                        self._get_proxy_provider()
                    )
                    dataframe = await self.quote_page_fetcher.fetch_kline(
                        code=normalized_code,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                        proxies=proxies,
                    )
                if dataframe.empty:
                    raise RuntimeError("行情页在指定日期范围内没有日 K 数据")
                await self._notify_proxy_success(proxies)
                return dataframe
            except NonRetryablePageError:
                await self._notify_proxy_success(proxies)
                raise
            except Exception as exc:
                last_error = exc
                await self._notify_proxy_failure(exc, proxies)
                if attempt < self.max_retry:
                    sleep_seconds = min(2**attempt, 10) + random.random()
                    logger.warning(
                        "eastmoney_quote_page_proxy_failed code=%s attempt=%s/%s "
                        "error=%s sleep=%.2fs",
                        normalized_code,
                        attempt,
                        self.max_retry,
                        repr(exc),
                        sleep_seconds,
                    )
                    await asyncio.sleep(sleep_seconds)

        raise RuntimeError(
            "eastmoney quote page failed after local request and "
            f"{self.max_retry} proxy attempts, code={normalized_code}, "
            f"error={last_error!r}"
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
        daily_with_indicators = self._merge_page_indicators(
            daily_df,
            raw_daily_df.attrs.get("indicator_rows") or {},
        )
        chip_map = self._build_page_chip_map(raw_daily_df.attrs.get("chip_rows") or {})
        daily_source = str(
            raw_daily_df.attrs.get("source") or EastMoneyQuotePageFetcher.SOURCE
        )
        page_url = str(
            raw_daily_df.attrs.get("page_url")
            or EastMoneyQuotePageFetcher.get_daily_page_url(code)
        )
        network = raw_daily_df.attrs.get("network")
        indicator_source = str(
            raw_daily_df.attrs.get("indicator_source")
            or EastMoneyQuotePageFetcher.RUNTIME_SOURCE
        )
        chip_source = str(
            raw_daily_df.attrs.get("chip_source")
            or EastMoneyQuotePageFetcher.RUNTIME_SOURCE
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

    def _merge_page_indicators(
        self,
        dataframe: pd.DataFrame,
        indicator_rows: Dict[str, Dict[str, Any]],
    ) -> pd.DataFrame:
        result = dataframe.copy()
        for column in self.INDICATOR_COLUMNS:
            result[column] = None
        for index, row in result.iterrows():
            page_values = indicator_rows.get(str(row["trade_date"])) or {}
            for column in self.INDICATOR_COLUMNS:
                if column in page_values:
                    result.at[index, column] = self._normalize_float(
                        page_values[column]
                    )
        return result

    def _build_page_chip_map(
        self,
        rows: Dict[str, Dict[str, Any]],
    ) -> Dict[str, ChipDistribution]:
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
                            chip_source if chip is not None else "unavailable_on_page"
                        ),
                    ),
                )
            )
        return items

    async def sleep_after_request(self) -> None:
        await asyncio.sleep(self.request_sleep_seconds + random.random() * 0.2)

    @staticmethod
    def _normalize_code(value: Any) -> str:
        return str(value).strip().zfill(6)

    @staticmethod
    def _normalize_float(value: Any) -> Optional[float]:
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
        number = cls._normalize_float(value)
        return None if number is None else round(number, digits)

    @classmethod
    def _normalize_int(cls, value: Any) -> Optional[int]:
        number = cls._normalize_float(value)
        return None if number is None else int(number)

    @classmethod
    def _normalize_number_list(cls, value: Any) -> List[float]:
        if not isinstance(value, list):
            return []
        return [
            number
            for item in value
            if (number := cls._normalize_float(item)) is not None
        ]
