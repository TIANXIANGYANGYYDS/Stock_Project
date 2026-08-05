from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from app.crawlers import stock_daily_detail_crawler as crawler_module
from app.crawlers import proxy_provider as proxy_module
from app.crawlers.eastmoney_reverse_fetcher import EastMoneyReverseFetcher
from app.crawlers.proxy_provider import (
    AsyncDailiProxyPool,
    AsyncDailiProxyProvider,
    ProxyEndpoint,
    DailiProxyProvider,
)
from app.crawlers.stock_daily_detail_crawler import (
    EastMoneyDataFetcher,
    StockDailyDetailCrawler,
)


def build_daily_df(periods: int = 2) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=periods, freq="D")
    rows = []
    for index, date in enumerate(dates):
        open_price = 10 + index * 0.01
        close_price = open_price + 0.05
        high_price = close_price + 0.1
        low_price = open_price - 0.1
        rows.append(
            {
                "日期": f"{date:%Y-%m-%d}",
                "开盘": open_price,
                "收盘": close_price,
                "最高": high_price,
                "最低": low_price,
                "成交量": 1000,
                "成交额": 1_050_000,
                "振幅": 2.0,
                "涨跌幅": 0.5,
                "涨跌额": 0.05,
                "换手率": 1.5,
                "股票代码": "000049",
            }
        )

    dataframe = pd.DataFrame(rows)
    latest_date = f"{dates[-1]:%Y-%m-%d}"
    dataframe.attrs.update(
        {
            "source": EastMoneyReverseFetcher.SOURCE,
            "page_url": EastMoneyReverseFetcher.get_daily_page_url("000049"),
            "network": "proxy",
            "indicator_source": EastMoneyReverseFetcher.INDICATOR_SOURCE,
            "chip_source": EastMoneyReverseFetcher.CHIP_SOURCE,
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


def test_reverse_reference_urls_match_eastmoney_routes() -> None:
    assert EastMoneyReverseFetcher.get_daily_page_url("002185") == (
        "https://quote.eastmoney.com/concept/sz002185.html#chart-k-cyq"
    )
    assert EastMoneyReverseFetcher.get_daily_page_url("600000") == (
        "https://quote.eastmoney.com/concept/sh600000.html#chart-k-cyq"
    )
    assert EastMoneyReverseFetcher.get_daily_page_url("920992") == (
        "https://quote.eastmoney.com/concept/bj920992.html#chart-k-cyq"
    )


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


def test_async_proxy_is_reused_until_failure_or_expiry(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_51_api_url="http://example.test/getip?time=5&qty=1"),
    )
    provider = AsyncDailiProxyProvider(minutes=3)
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
        lambda: SimpleNamespace(
            proxy_51_api_url=(
                "http://example.test/getip?linePoolIndex=1&packid=22&time=5"
                "&qty=1&port=1&format=json&dt=2&dtc=5&pid=p&rid=r&uid=1"
            )
        ),
    )
    provider = DailiProxyProvider(minutes=3, count=4)
    query = parse_qs(urlparse(provider._build_api_url()).query)
    endpoints = provider._extract_endpoints_from_json(
        {
            "code": 0,
            "success": "true",
            "data": [
                {"ip": "127.0.0.1", "port": "8001"},
                {"ip": "127.0.0.1", "port": "8002"},
                {"ip": "127.0.0.1", "port": "8003"},
                {"ip": "127.0.0.1", "port": "8004"},
            ],
        }
    )

    assert query["qty"] == ["4"]
    assert query["time"] == ["5"]
    assert query["dt"] == ["2"]
    assert [endpoint.port for endpoint in endpoints] == [8001, 8002, 8003, 8004]


def test_proxy_pool_limits_each_ip_to_two_pages_and_refills(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_51_api_url="http://example.test/getip?time=5&qty=1"),
    )
    provider = AsyncDailiProxyPool(
        minutes=3,
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


def test_51daili_proxy_pool_builds_api_url_and_parses_response(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(
            proxy_51_api_url=(
                "http://example.test/getip?linePoolIndex=1&packid=22&time=5"
                "&qty=1&port=1&format=json&dt=2&dtc=5&pid=p&rid=r&uid=1"
            )
        ),
    )
    provider = AsyncDailiProxyPool(minutes=3, pool_size=2)

    try:
        query = parse_qs(urlparse(provider._build_api_url()).query)
        endpoints = provider._extract_endpoints_from_json(
            {
                "code": 0,
                "success": True,
                "data": [
                    {"ip": "127.0.0.1", "port": 8001},
                    {"ip": "127.0.0.2", "port": "8002"},
                ],
            }
        )

        assert query["packid"] == ["22"]
        assert query["time"] == ["5"]
        assert query["qty"] == ["2"]
        assert query["format"] == ["json"]
        assert query["pid"] == ["p"]
        assert query["rid"] == ["r"]
        assert [endpoint.display() for endpoint in endpoints] == [
            "127.0.0.1:8001",
            "127.0.0.2:8002",
        ]
    finally:
        asyncio.run(provider.close())


def test_51daili_proxy_pool_rejects_non_fixed_duration(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_51_api_url="http://example.test/getip?time=5"),
    )

    with pytest.raises(ValueError, match="固定有效3分钟"):
        AsyncDailiProxyPool(minutes=4)


def test_reverse_values_are_saved_without_local_fill(monkeypatch) -> None:
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
    assert item.source.indicator == EastMoneyReverseFetcher.INDICATOR_SOURCE
    assert item.source.chip == EastMoneyReverseFetcher.CHIP_SOURCE


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
