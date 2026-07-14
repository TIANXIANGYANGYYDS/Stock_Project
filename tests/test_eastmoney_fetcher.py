from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from app.crawlers import stock_daily_detail_crawler as crawler_module
from app.crawlers import proxy_provider as proxy_module
from app.crawlers.proxy_provider import (
    AsyncShanchenProxyPool,
    AsyncShanchenProxyProvider,
    ProxyEndpoint,
    ShanchenProxyProvider,
)
from app.crawlers.stock_daily_detail_crawler import (
    EastMoneyDataFetcher,
    EastMoneyQuotePageFetcher,
    LocalQuoteCircuitBreaker,
    NonRetryablePageError,
    StockDailyDetailCrawler,
)


def build_daily_df(periods: int = 2) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=periods, freq="D")
    lines = []
    for index, date in enumerate(dates):
        open_price = 10 + index * 0.01
        close_price = open_price + 0.05
        high_price = close_price + 0.1
        low_price = open_price - 0.1
        lines.append(
            (
                f"{date:%Y-%m-%d},"
                f"{open_price:.2f},{close_price:.2f},{high_price:.2f},"
                f"{low_price:.2f},1000,1050000,2.00,0.50,0.05,1.50"
            )
        )

    dataframe = EastMoneyDataFetcher._kline_lines_to_daily_df(lines, code="000049")
    latest_date = f"{dates[-1]:%Y-%m-%d}"
    dataframe.attrs.update(
        {
            "source": EastMoneyQuotePageFetcher.SOURCE,
            "page_url": EastMoneyQuotePageFetcher.get_quote_url("000049"),
            "network": "proxy",
            "indicator_source": EastMoneyQuotePageFetcher.RUNTIME_SOURCE,
            "chip_source": EastMoneyQuotePageFetcher.RUNTIME_SOURCE,
            "indicator_rows": {
                latest_date: {
                    "ma5": 10.03,
                    "ma10": 10.02,
                    "ma20": 10.01,
                    "ma30": 9.99,
                    "ma60": 9.88,
                    "vol_ma5": 1000,
                    "vol_ma10": 900,
                    "macd_dif": 0.11,
                    "macd_dea": 0.08,
                    "macd_hist": 0.06,
                    "kdj_k": 60,
                    "kdj_d": 55,
                    "kdj_j": 70,
                    "rsi6": 65,
                    "rsi12": 60,
                    "rsi24": 58,
                    "boll_mid": 10,
                    "boll_upper": 11,
                    "boll_lower": 9,
                    "cci14": 88,
                    "wr6": 20,
                    "wr10": 30,
                }
            },
            "chip_rows": {
                latest_date: {
                    "profit_ratio": 0.65,
                    "avg_cost": 10.01,
                    "cost_90_low": 9.5,
                    "cost_90_high": 10.5,
                    "cost_90_concentration": 0.05,
                    "cost_70_low": 9.8,
                    "cost_70_high": 10.3,
                    "cost_70_concentration": 0.025,
                    "chart_x": [float(index) for index in range(150)],
                    "chart_y": [9 + index * 0.01 for index in range(150)],
                }
            },
        }
    )
    return dataframe


def test_quote_page_urls_match_eastmoney_routes() -> None:
    assert EastMoneyQuotePageFetcher.get_quote_url("002185") == (
        "https://quote.eastmoney.com/sz002185.html"
    )
    assert EastMoneyQuotePageFetcher.get_concept_url("002185") == (
        "https://quote.eastmoney.com/concept/sz002185.html#chart-k-cyq"
    )
    assert EastMoneyQuotePageFetcher.get_quote_url("600000").endswith("/sh600000.html")
    assert EastMoneyQuotePageFetcher.get_quote_url("920992").endswith("/bj920992.html")
    assert EastMoneyQuotePageFetcher.get_daily_page_url("002185") == (
        "https://quote.eastmoney.com/sz002185.html"
    )
    assert EastMoneyQuotePageFetcher.get_daily_page_url("920992") == (
        "https://quote.eastmoney.com/concept/bj920992.html#chart-k-cyq"
    )


def test_kline_lines_to_daily_df_matches_quote_page_schema() -> None:
    dataframe = EastMoneyDataFetcher._kline_lines_to_daily_df(
        [
            "2026-07-10,26.10,25.31,26.10,25.10,4948817,"
            "12764273171.63,4.21,6.66,1.58,14.89"
        ],
        code="2185",
    )

    assert dataframe.loc[0, "股票代码"] == "002185"
    assert dataframe.loc[0, "收盘"] == 25.31
    assert dataframe.loc[0, "成交量"] == 4_948_817


def test_bse_concept_daily_rows_match_quote_page_schema() -> None:
    dataframe = EastMoneyQuotePageFetcher._concept_daily_rows_to_df(
        [
            {
                "date": "2026-07-13",
                "open": 11.92,
                "close": 11.45,
                "high": 12.05,
                "low": 11.38,
                "volume": 10_818,
                "amount": 12_514_985.53,
                "amplitude": 5.57,
                "pctChange": -4.82,
                "changeAmount": -0.58,
                "turnover": 2.21,
            }
        ],
        code="920992",
    )

    assert list(dataframe.columns) == [
        *EastMoneyDataFetcher.DAILY_COLUMNS,
        "股票代码",
    ]
    assert dataframe.loc[0, "股票代码"] == "920992"
    assert dataframe.loc[0, "收盘"] == 11.45
    assert dataframe.loc[0, "成交额"] == 12_514_985.53


def test_daily_crawler_contains_no_local_indicator_or_chip_fallback() -> None:
    source = inspect.getsource(crawler_module)
    forbidden = (
        "import akshare",
        ".rolling(",
        ".ewm(",
        "calculate_chip_distribution",
        "_add_indicators",
        "_calculate_rsi",
    )
    assert all(token not in source for token in forbidden)


def test_runtime_diagnostics_can_be_dumped(tmp_path) -> None:
    fetcher = EastMoneyQuotePageFetcher()
    assert fetcher.strict_page_indicators is True
    assert fetcher.strict_page_chip is True
    fetcher.last_runtime_diagnostics = {"runtime": {"chipDateCount": 1}}
    target = fetcher.dump_last_runtime_diagnostics(tmp_path / "diagnostics.json")

    assert target.read_text(encoding="utf-8").find('"chipDateCount": 1') > 0


class FakeProxyProvider:
    def __init__(self) -> None:
        self.proxy_index = 0
        self.success_count = 0
        self.failures: list[Exception] = []

    async def get_requests_proxies(self) -> dict[str, str]:
        self.proxy_index += 1
        url = f"http://127.0.0.1:{8000 + self.proxy_index}"
        return {"http": url, "https": url}

    def on_success(self) -> None:
        self.success_count += 1

    def on_failure(self, exc: Exception) -> None:
        self.failures.append(exc)

    async def close(self) -> None:
        pass


class FakeQuotePageFetcher:
    def __init__(self, fail_proxy_count: int = 0) -> None:
        self.fail_proxy_count = fail_proxy_count
        self.calls: list[Optional[dict[str, str]]] = []

    async def fetch_kline(self, **kwargs) -> pd.DataFrame:
        proxies = kwargs.get("proxies")
        self.calls.append(proxies)
        if proxies is None:
            raise ConnectionError("local IP blocked")
        if self.fail_proxy_count:
            self.fail_proxy_count -= 1
            raise ConnectionError("proxy blocked")
        return build_daily_df()

    async def close(self) -> None:
        pass


class NonRetryableFakeQuotePageFetcher:
    def __init__(self) -> None:
        self.calls: list[Optional[dict[str, str]]] = []

    async def fetch_kline(self, **kwargs) -> pd.DataFrame:
        self.calls.append(kwargs.get("proxies"))
        raise NonRetryablePageError("行情页响应异常: status=404")

    async def close(self) -> None:
        pass


def test_daily_history_uses_local_ip_then_rotates_proxy(monkeypatch) -> None:
    provider = FakeProxyProvider()
    page_fetcher = FakeQuotePageFetcher(fail_proxy_count=1)
    crawler = StockDailyDetailCrawler(
        max_retry=2,
        proxy_provider=provider,
        quote_page_fetcher=page_fetcher,
    )

    async def run_test() -> pd.DataFrame:
        async def no_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(crawler_module.asyncio, "sleep", no_sleep)
        try:
            return await crawler.fetch_stock_daily_hist(
                "002185", "20260101", "20260102"
            )
        finally:
            await crawler.close()

    dataframe = asyncio.run(run_test())

    assert page_fetcher.calls == [
        None,
        {"http": "http://127.0.0.1:8001", "https": "http://127.0.0.1:8001"},
        {"http": "http://127.0.0.1:8002", "https": "http://127.0.0.1:8002"},
    ]
    assert len(provider.failures) == 1
    assert provider.success_count == 1
    assert dataframe.attrs["source"] == EastMoneyQuotePageFetcher.SOURCE


def test_non_retryable_page_error_does_not_switch_proxy_or_trip_circuit() -> None:
    provider = FakeProxyProvider()
    page_fetcher = NonRetryableFakeQuotePageFetcher()
    crawler = StockDailyDetailCrawler(
        max_retry=3,
        proxy_provider=provider,
        quote_page_fetcher=page_fetcher,
    )

    async def run_test() -> None:
        try:
            with pytest.raises(NonRetryablePageError):
                await crawler.fetch_stock_daily_hist(
                    "920992", "20260713", "20260713"
                )
        finally:
            await crawler.close()

    asyncio.run(run_test())

    assert page_fetcher.calls == [None]
    assert provider.proxy_index == 0
    assert provider.failures == []
    assert crawler.local_circuit_breaker.retry_after == 0


def test_non_retryable_page_data_error_keeps_proxy() -> None:
    provider = FakeProxyProvider()
    page_fetcher = NonRetryableFakeQuotePageFetcher()
    circuit_breaker = LocalQuoteCircuitBreaker()
    circuit_breaker.mark_failure(300)
    crawler = StockDailyDetailCrawler(
        max_retry=2,
        proxy_provider=provider,
        quote_page_fetcher=page_fetcher,
        local_circuit_breaker=circuit_breaker,
    )

    async def run_test() -> None:
        try:
            with pytest.raises(NonRetryablePageError):
                await crawler.fetch_stock_daily_hist(
                    "920680", "20260713", "20260713"
                )
        finally:
            await crawler.close()

    asyncio.run(run_test())

    assert len(page_fetcher.calls) == 1
    assert page_fetcher.calls[0] is not None
    assert provider.proxy_index == 1
    assert provider.success_count == 1
    assert provider.failures == []


def test_concurrent_crawlers_only_run_one_local_probe() -> None:
    provider = FakeProxyProvider()
    page_fetcher = FakeQuotePageFetcher()
    circuit_breaker = LocalQuoteCircuitBreaker()
    semaphore = asyncio.Semaphore(5)
    crawlers = [
        StockDailyDetailCrawler(
            max_retry=1,
            proxy_provider=provider,
            quote_page_fetcher=page_fetcher,
            page_semaphore=semaphore,
            local_circuit_breaker=circuit_breaker,
        )
        for _ in range(5)
    ]

    async def run_test() -> None:
        try:
            await asyncio.gather(
                *(
                    crawler.fetch_stock_daily_hist(
                        f"{2185 + index:06d}",
                        "20260101",
                        "20260102",
                    )
                    for index, crawler in enumerate(crawlers)
                )
            )
        finally:
            for crawler in crawlers:
                await crawler.close()
            await provider.close()

    asyncio.run(run_test())

    assert page_fetcher.calls.count(None) == 1
    assert len([proxies for proxies in page_fetcher.calls if proxies]) == 5


def test_async_proxy_is_reused_until_failure_or_expiry(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_api_key="test-key"),
    )
    provider = AsyncShanchenProxyProvider(minutes=1)
    extraction_count = 0

    async def fake_fetch_endpoint() -> ProxyEndpoint:
        nonlocal extraction_count
        extraction_count += 1
        return ProxyEndpoint("127.0.0.1", 8000 + extraction_count)

    monkeypatch.setattr(provider, "_fetch_proxy_endpoint_async", fake_fetch_endpoint)

    async def run_test() -> None:
        try:
            first = await provider.get_requests_proxies()
            assert await provider.get_requests_proxies() == first
            assert extraction_count == 1

            provider.on_failure_for(first, ConnectionError("proxy failed"))
            second = await provider.get_requests_proxies()
            assert second != first
            assert extraction_count == 2

            provider.on_failure_for(first, ConnectionError("stale request failed"))
            assert await provider.get_requests_proxies() == second
            assert extraction_count == 2
        finally:
            await provider.close()

    asyncio.run(run_test())


def test_proxy_api_count_and_multi_endpoint_json(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_api_key="test-key"),
    )
    provider = ShanchenProxyProvider(minutes=1, count=4)
    query = parse_qs(urlparse(provider._build_api_url()).query)
    endpoints = provider._extract_endpoints_from_json(
        {
            "count": "4",
            "status": "0",
            "list": [
                {"sever": "127.0.0.1", "port": 8001, "net_type": 2},
                {"sever": "127.0.0.1", "port": 8002, "net_type": 2},
                {"sever": "127.0.0.1", "port": 8003, "net_type": 2},
                {"sever": "127.0.0.1", "port": 8004, "net_type": 2},
            ],
        }
    )

    assert query["count"] == ["4"]
    assert [endpoint.port for endpoint in endpoints] == [8001, 8002, 8003, 8004]


def test_proxy_pool_limits_each_ip_to_two_pages_and_refills(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_api_key="test-key"),
    )
    provider = AsyncShanchenProxyPool(
        minutes=1,
        pool_size=4,
        max_concurrency_per_proxy=2,
    )
    requested_counts: list[int] = []

    async def fake_fetch_endpoints(count: int) -> list[ProxyEndpoint]:
        requested_counts.append(count)
        base_port = 8000 if len(requested_counts) == 1 else 9000
        return [
            ProxyEndpoint("127.0.0.1", base_port + index)
            for index in range(1, count + 1)
        ]

    monkeypatch.setattr(
        provider,
        "_fetch_proxy_endpoints_async",
        fake_fetch_endpoints,
    )

    async def run_test() -> None:
        try:
            leases = await asyncio.gather(
                *(provider.get_requests_proxies() for _ in range(8))
            )
            proxy_urls = [lease["https"] for lease in leases if lease]
            assert sorted(Counter(proxy_urls).values()) == [2, 2, 2, 2]
            assert requested_counts == [4]

            waiting = asyncio.create_task(provider.get_requests_proxies())
            await asyncio.sleep(0)
            assert not waiting.done()

            await provider.on_success_for(leases[0])
            replacement_lease = await asyncio.wait_for(waiting, timeout=1)
            assert replacement_lease == leases[0]

            await provider.on_failure_for(
                replacement_lease,
                ConnectionError("proxy failed"),
            )
            new_lease = await provider.get_requests_proxies()
            assert requested_counts == [4, 1]
            assert new_lease["https"].endswith(":9001")

            await provider.on_success_for(new_lease)
            for lease in leases[1:]:
                await provider.on_success_for(lease)

            assert provider.stats.added_endpoint_count == 5
            assert provider.stats.discarded_endpoint_count == 1
            assert provider.stats.lease_count == 10
            assert provider.stats.success_count == 9
            assert provider.stats.failure_count == 1
            assert provider.stats.max_in_flight == 8
        finally:
            await provider.close()

    asyncio.run(run_test())


def test_page_values_are_saved_without_local_fill(monkeypatch) -> None:
    crawler = StockDailyDetailCrawler(max_retry=1)
    daily_df = build_daily_df()

    async def fake_fetch(*args, **kwargs) -> pd.DataFrame:
        return daily_df

    monkeypatch.setattr(crawler, "fetch_stock_daily_hist", fake_fetch)

    async def run_test():
        try:
            return await crawler.build_stock_daily_details(
                code="49",
                name="德赛电池",
                start_date="20260101",
                end_date="20260102",
                write_start_date="2026-01-02",
            )
        finally:
            await crawler.close()

    items = asyncio.run(run_test())

    assert len(items) == 1
    item = items[0]
    assert item.ma.ma5 == 10.03
    assert item.ma.ma250 is None
    assert item.volume_ma.vol_ma20 is None
    assert item.wr.wr6 == 20
    assert item.wr.wr14 is None
    assert item.atr.atr14 is None
    assert item.chip is not None
    assert item.chip.chart is not None
    assert len(item.chip.chart.x) == 150
    assert item.source.indicator == "eastmoney.quote_page.runtime"
    assert item.source.chip == "eastmoney.quote_page.runtime"


def test_stock_universe_keeps_only_target_date_traded_stocks() -> None:
    timestamp = int(datetime.fromisoformat("2026-07-13T15:01:00+08:00").timestamp())
    traded = {"f5": 100, "f6": 1000, "f124": timestamp}
    suspended = {"f5": 0, "f6": 0, "f124": timestamp}
    stale = {"f5": 100, "f6": 1000, "f124": timestamp - 86400}

    assert EastMoneyDataFetcher._is_target_date_traded_stock(
        traded,
        target_trade_date="2026-07-13",
    )
    assert not EastMoneyDataFetcher._is_target_date_traded_stock(
        suspended,
        target_trade_date="2026-07-13",
    )
    assert not EastMoneyDataFetcher._is_target_date_traded_stock(
        stale,
        target_trade_date="2026-07-13",
    )


def test_stock_universe_skips_delisting_names_only() -> None:
    assert EastMoneyDataFetcher._is_delisting_stock_name("广道退")
    assert EastMoneyDataFetcher._is_delisting_stock_name("退市园城")
    assert not EastMoneyDataFetcher._is_delisting_stock_name("*ST卓然")
    assert not EastMoneyDataFetcher._is_delisting_stock_name("华天科技")


def test_stock_list_excludes_traded_delisting_stock(monkeypatch) -> None:
    fetcher = EastMoneyDataFetcher()
    timestamp = int(datetime.fromisoformat("2026-07-13T15:01:00+08:00").timestamp())

    async def fake_request(*args, **kwargs):
        return {
            "data": {
                "total": 2,
                "diff": [
                    {
                        "f12": "920680",
                        "f14": "广道退",
                        "f5": 100,
                        "f6": 1000,
                        "f124": timestamp,
                    },
                    {
                        "f12": "920992",
                        "f14": "中科美菱",
                        "f5": 100,
                        "f6": 1000,
                        "f124": timestamp,
                    },
                ],
            }
        }

    monkeypatch.setattr(fetcher, "_request_url_json", fake_request)

    async def run_test() -> pd.DataFrame:
        try:
            return await fetcher.fetch_stock_list(
                target_trade_date="2026-07-13"
            )
        finally:
            await fetcher.close()

    dataframe = asyncio.run(run_test())

    assert dataframe.to_dict("records") == [{"代码": "920992", "名称": "中科美菱"}]
