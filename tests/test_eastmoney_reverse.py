from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.crawlers.eastmoney_reverse_core import (
    Kline,
    calculate_chip,
    calculate_indicators,
)
from app.crawlers.eastmoney_reverse_fetcher import (
    EastMoneyReverseFetcher,
    EastMoneyReverseTransport,
    FetchResult,
    ManagedFetchResult,
    ReverseDataError,
    ReverseNetworkError,
    TargetAwareProxyManager,
)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeProxyPool:
    def __init__(self, proxy_urls: list[str]) -> None:
        self.proxy_urls = list(proxy_urls)
        self.current: str | None = None
        self.successes: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.stats = SimpleNamespace()

    async def get_requests_proxies(self):
        if self.current is None:
            if not self.proxy_urls:
                return None
            self.current = self.proxy_urls.pop(0)
        return {"http": self.current, "https": self.current}

    async def on_success_for(self, proxies) -> None:
        self.successes.append(proxies["https"])

    async def on_failure_for(self, proxies, exc) -> None:
        self.failures.append((proxies["https"], str(exc)))
        self.current = None

    async def close(self) -> None:
        pass


@dataclass
class PlannedTransport:
    outcomes: dict[str, list]

    def __post_init__(self) -> None:
        self.calls: list[tuple[object, str, str]] = []

    async def fetch_once(
        self,
        session,
        proxy_url,
        code,
        target_trade_date,
        adjust="qfq",
    ):
        self.calls.append((session, proxy_url, code))
        outcome = self.outcomes[proxy_url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FetchResult(rows=[], request_seconds=0.01, response_bytes=100)


def make_manager(pool, transport, *, quota=40, session_factory=None):
    return TargetAwareProxyManager(
        pool_size=1,
        request_interval_seconds=0,
        max_target_requests_per_proxy=quota,
        max_stock_attempts=8,
        max_candidate_count=8,
        circuit_failure_threshold=8,
        circuit_cooldown_seconds=0,
        proxy_pool=pool,
        transport=transport,
        session_factory=session_factory or FakeSession,
    )


def make_rows(count: int = 40) -> list[Kline]:
    rows = []
    for index in range(count):
        base = 10 + index * 0.07 + ((index % 5) - 2) * 0.11
        open_price = base - 0.08 + (index % 3) * 0.03
        close = base + ((index % 4) - 1.5) * 0.06
        rows.append(
            Kline(
                date=f"2026-01-{index + 1:02d}",
                open=open_price,
                close=close,
                high=max(open_price, close) + 0.24 + (index % 2) * 0.03,
                low=min(open_price, close) - 0.19,
                volume=10000 + index * 137,
                amount=(10000 + index * 137) * close * 100,
                amplitude_pct=4.2,
                pct_chg=0.7,
                change_amount=0.08,
                turnover_pct=0.8 + (index % 7) * 0.17,
            )
        )
    return rows


def test_candidate_failure_is_discarded_and_stock_moves_to_next_proxy() -> None:
    pool = FakeProxyPool(["http://p1", "http://p2"])
    transport = PlannedTransport(
        {
            "http://p1": [ReverseNetworkError("blocked")],
            "http://p2": ["success"],
        }
    )
    manager = make_manager(pool, transport)

    async def run_test():
        try:
            return await manager.fetch("000001", "2026-07-30")
        finally:
            await manager.close()

    result = asyncio.run(run_test())

    assert result.attempts == 2
    assert [item[0] for item in pool.failures] == ["http://p1"]
    assert pool.successes == ["http://p2"]
    assert manager.stats.candidate_count == 2
    assert manager.stats.qualified_count == 1
    assert manager.stats.rejected_candidate_count == 1


def test_proxy_is_reused_then_rotated_at_target_quota() -> None:
    pool = FakeProxyPool(["http://p1", "http://p2"])
    transport = PlannedTransport(
        {
            "http://p1": ["one", "two"],
            "http://p2": ["three"],
        }
    )
    manager = make_manager(pool, transport, quota=2)

    async def run_test():
        try:
            first = await manager.fetch("000001", "2026-07-30")
            second = await manager.fetch("000017", "2026-07-30")
            third = await manager.fetch("000032", "2026-07-30")
            return first, second, third
        finally:
            await manager.close()

    first, second, third = asyncio.run(run_test())

    assert first.proxy_id == second.proxy_id
    assert third.proxy_id != first.proxy_id
    assert manager.stats.proactive_rotation_count == 1
    assert manager.stats.per_proxy_successes[first.proxy_id] == 2
    assert manager.stats.per_proxy_successes[third.proxy_id] == 1
    assert "target request quota reached" in pool.failures[0][1]


def test_healthy_proxy_gets_one_connection_restart_before_rotation() -> None:
    pool = FakeProxyPool(["http://p1"])
    transport = PlannedTransport(
        {
            "http://p1": [
                "first",
                ReverseNetworkError("stale connection"),
                "recovered",
            ]
        }
    )
    sessions = []

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    manager = make_manager(pool, transport, session_factory=session_factory)

    async def run_test():
        try:
            await manager.fetch("000001", "2026-07-30")
            return await manager.fetch("000017", "2026-07-30")
        finally:
            await manager.close()

    result = asyncio.run(run_test())

    assert result.attempts == 2
    assert manager.stats.connection_recovery_count == 1
    assert pool.failures == []
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)


def test_valid_empty_payload_is_not_retried_with_another_proxy() -> None:
    pool = FakeProxyPool(["http://p1", "http://p2"])
    transport = PlannedTransport(
        {"http://p1": [ReverseDataError("no data")], "http://p2": ["unused"]}
    )
    manager = make_manager(pool, transport)

    async def run_test():
        try:
            with pytest.raises(ReverseDataError, match="no data"):
                await manager.fetch("000001", "2026-07-30")
        finally:
            await manager.close()

    asyncio.run(run_test())

    assert pool.successes == ["http://p1"]
    assert pool.failures == []
    assert len(transport.calls) == 1


def test_reverse_fetcher_builds_production_dataframe_and_preserves_adjust() -> None:
    rows = [
        Kline(
            date=f"2026-07-{day:02d}",
            open=10.0 + day / 100,
            close=10.1 + day / 100,
            high=10.3 + day / 100,
            low=9.9 + day / 100,
            volume=10000 + day,
            amount=1000000 + day,
            amplitude_pct=4.0,
            pct_chg=1.0,
            change_amount=0.1,
            turnover_pct=1.2,
        )
        for day in range(1, 31)
    ]

    class FakeManager:
        def __init__(self) -> None:
            self.calls = []

        async def fetch(self, code, target_trade_date, adjust="qfq"):
            self.calls.append((code, target_trade_date, adjust))
            return ManagedFetchResult(
                response=FetchResult(
                    rows=rows,
                    request_seconds=0.1,
                    response_bytes=1234,
                ),
                attempts=2,
                proxy_id="hashed-proxy",
            )

    manager = FakeManager()
    fetcher = EastMoneyReverseFetcher(manager=manager)
    dataframe = asyncio.run(
        fetcher.fetch_kline(
            code="1",
            start_date="20260729",
            end_date="20260730",
            adjust="hfq",
        )
    )

    assert manager.calls == [("1", "2026-07-30", "hfq")]
    assert dataframe["日期"].tolist() == ["2026-07-29", "2026-07-30"]
    assert dataframe["股票代码"].tolist() == ["000001", "000001"]
    assert dataframe.attrs["source"] == EastMoneyReverseFetcher.SOURCE
    assert dataframe.attrs["reverse_attempts"] == 2
    assert len(dataframe.attrs["chip_rows"]["2026-07-30"]["chart_x"]) == 150


def test_transport_accepts_latest_row_before_requested_end_date() -> None:
    class FakeResponse:
        status_code = 200
        content = b"payload"

        def json(self):
            return {
                "rc": 0,
                "data": {
                    "klines": ["2026-07-30,10,11,12,9,100,1000,3,1,0.1,2"]
                },
            }

    class FakeHttpSession:
        async def get(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return FakeResponse()

    session = FakeHttpSession()
    result = asyncio.run(
        EastMoneyReverseTransport().fetch_once(
            session,
            "http://proxy",
            "000001",
            "2026-07-31",
            "hfq",
        )
    )

    assert result.rows[-1].date == "2026-07-30"
    assert session.kwargs["params"]["end"] == "20260731"
    assert session.kwargs["params"]["fqt"] == "2"


def test_indicator_and_chip_algorithms_keep_golden_values() -> None:
    rows = make_rows()
    values = calculate_indicators(rows)["2026-01-40"]
    assert values == pytest.approx(
        {
            "ma5": 12.608,
            "ma10": 12.427,
            "ma20": 12.065,
            "ma30": 11.719,
            "vol_ma5": 15069.0,
            "vol_ma10": 14726.5,
            "macd_dif": 0.488,
            "macd_dea": 0.453,
            "macd_hist": 0.071,
            "boll_mid": 12.065,
            "boll_upper": 13.03,
            "boll_lower": 11.1,
            "cci14": 169.573,
            "kdj_k": 76.281,
            "kdj_d": 72.556,
            "kdj_j": 83.731,
            "rsi6": 79.31,
            "rsi12": 73.541,
            "rsi24": 71.337,
            "wr10": 15.882,
            "wr6": 20.93,
        }
    )

    chip = calculate_chip(rows, len(rows) - 1)
    assert chip["profit_ratio"] == pytest.approx(0.9875)
    assert chip["avg_cost"] == pytest.approx(11.57)
    assert chip["cost_90"] == pytest.approx(
        {"low": 10.14, "high": 12.8, "concentration": 0.1159}
    )
    assert chip["cost_70"] == pytest.approx(
        {"low": 10.5, "high": 12.44, "concentration": 0.0847}
    )
    assert len(chip["chart"]["x"]) == 150
    assert len(chip["chart"]["y"]) == 150
    assert max(chip["chart"]["x"]) == pytest.approx(230)


def test_boll_uses_javascript_sequential_float_accumulation() -> None:
    closes_from_latest = [
        13.07,
        12.63,
        12.94,
        11.76,
        11.29,
        11.39,
        11.23,
        11.25,
        11.17,
        11.23,
        11.06,
        11.18,
        11.07,
        10.64,
        10.68,
        10.94,
        10.94,
        10.62,
        11.02,
        11.24,
    ]
    rows = [
        Kline(
            date=f"2025-10-{index + 1:02d}",
            open=close,
            close=close,
            high=close,
            low=close,
            volume=1,
            amount=close,
            amplitude_pct=0,
            pct_chg=0,
            change_amount=0,
            turnover_pct=1,
        )
        for index, close in enumerate(reversed(closes_from_latest))
    ]

    values = calculate_indicators(rows)["2025-10-20"]

    assert values["boll_mid"] == 11.368
    assert values["boll_upper"] == 12.746
    assert values["boll_lower"] == 9.989
