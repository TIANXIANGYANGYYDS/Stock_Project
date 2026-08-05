from __future__ import annotations

import asyncio

import pandas as pd
import pytest

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
    VolumeMAIndicators,
    WRIndicators,
)
from app.services import stock_daily_detail_service as service


def test_resolve_a_stock_target_trade_date_keeps_trade_day(monkeypatch) -> None:
    async def fake_trade_dates(self, start_date, end_date):
        return "2026-06-25", "2026-06-26"

    monkeypatch.setattr(
        service.StockDailyDetailCrawler,
        "fetch_trade_dates",
        fake_trade_dates,
    )

    decision = asyncio.run(service.resolve_a_stock_target_trade_date("20260626"))

    assert decision.reference_trade_date == "2026-06-26"
    assert decision.target_trade_date == "2026-06-26"
    assert decision.target_yyyymmdd == "20260626"
    assert decision.is_reference_trade_day is True


def test_resolve_a_stock_target_trade_date_falls_back_to_previous_trade_day(
    monkeypatch,
) -> None:
    async def fake_trade_dates(self, start_date, end_date):
        return "2026-06-25", "2026-06-26"

    monkeypatch.setattr(
        service.StockDailyDetailCrawler,
        "fetch_trade_dates",
        fake_trade_dates,
    )

    decision = asyncio.run(service.resolve_a_stock_target_trade_date("20260628"))

    assert decision.reference_trade_date == "2026-06-28"
    assert decision.target_trade_date == "2026-06-26"
    assert decision.target_yyyymmdd == "20260626"
    assert decision.is_reference_trade_day is False


def test_resolve_trade_date_uses_stock_list_when_calendar_is_unavailable(
    monkeypatch,
) -> None:
    async def failing_trade_dates(self, start_date, end_date):
        raise ConnectionError("index kline unavailable")

    async def fake_stock_list(self, **kwargs):
        dataframe = pd.DataFrame([{"代码": "002185", "名称": "华天科技"}])
        dataframe.attrs["latest_trade_date"] = "2026-07-13"
        return dataframe

    monkeypatch.setattr(
        service.StockDailyDetailCrawler,
        "fetch_trade_dates",
        failing_trade_dates,
    )
    monkeypatch.setattr(
        service.StockDailyDetailCrawler,
        "fetch_stock_list",
        fake_stock_list,
    )

    decision = asyncio.run(service.resolve_a_stock_target_trade_date("20260713"))

    assert decision.target_trade_date == "2026-07-13"
    assert decision.is_reference_trade_day is True


def build_complete_item(trade_date: str = "2026-06-26") -> StockDailyDetail:
    return StockDailyDetail(
        trade_date=trade_date,
        trade_date_int=int(trade_date.replace("-", "")),
        code="920992",
        name="中科美菱",
        adjust="qfq",
        open=10.0,
        close=10.5,
        high=10.8,
        low=9.9,
        volume=1000,
        amount=1050000,
        amplitude_pct=9.0,
        pct_chg=1.2,
        change_amount=0.12,
        turnover_pct=2.5,
        ma=MAIndicators(ma5=10.1, ma10=10.2, ma20=10.3, ma30=10.4, ma60=10.5),
        volume_ma=VolumeMAIndicators(vol_ma5=100, vol_ma10=200, vol_ma20=300),
        macd=MACDIndicators(dif=0.1, dea=0.2, hist=-0.2),
        kdj=KDJIndicators(k=50.1, d=40.2, j=70.3),
        rsi=RSIIndicators(rsi6=61.1, rsi12=55.2, rsi24=50.3),
        boll=BOLLIndicators(mid=10.2, upper=11.3, lower=9.1),
        cci=CCIIndicators(cci14=101.2),
        wr=WRIndicators(wr6=30.2, wr10=20.1),
        atr=ATRIndicators(atr14=0.66),
        chip=ChipDistribution(
            profit_ratio=0.6123,
            avg_cost=12.34,
            cost_90=ChipCostRange(low=10.1, high=14.2, concentration=0.22),
            cost_70=ChipCostRange(low=10.8, high=13.5, concentration=0.18),
            chart=ChipChart(x=[10.1, 12.0, 14.2], y=[1.0, 2.0, 3.0]),
        ),
    )


def test_validate_sync_items_requires_target_trade_date() -> None:
    daily_service = service.StockDailyDetailService()

    try:
        with pytest.raises(RuntimeError, match="target trade date missing"):
            daily_service._validate_sync_items_for_target_date(
                [build_complete_item("2026-06-25")],
                target_trade_date="2026-06-26",
                code="920992",
            )
    finally:
        asyncio.run(daily_service.close())


def test_validate_sync_items_accepts_complete_target_trade_date() -> None:
    daily_service = service.StockDailyDetailService()

    try:
        daily_service._validate_sync_items_for_target_date(
            [build_complete_item()],
            target_trade_date="2026-06-26",
            code="920992",
        )
    finally:
        asyncio.run(daily_service.close())


def test_validate_sync_items_allows_indicators_not_exposed_by_page() -> None:
    daily_service = service.StockDailyDetailService()
    item = build_complete_item()
    item.ma.ma120 = None
    item.ma.ma250 = None
    item.volume_ma.vol_ma20 = None
    item.volume_ma.vol_ma60 = None
    item.wr.wr14 = None
    item.atr.atr14 = None

    try:
        daily_service._validate_sync_items_for_target_date(
            [item],
            target_trade_date="2026-06-26",
            code="920992",
        )
    finally:
        asyncio.run(daily_service.close())


def test_validate_sync_items_rejects_local_indicator_source() -> None:
    daily_service = service.StockDailyDetailService()
    item = build_complete_item()
    item.source.indicator = "local.pandas"

    try:
        with pytest.raises(RuntimeError, match="source.indicator"):
            daily_service._validate_sync_items_for_target_date(
                [item],
                target_trade_date="2026-06-26",
                code="920992",
            )
    finally:
        asyncio.run(daily_service.close())


def test_validate_sync_items_accepts_reverse_sources() -> None:
    daily_service = service.StockDailyDetailService()
    item = build_complete_item()
    item.source.daily = service.EastMoneyReverseFetcher.SOURCE
    item.source.indicator = service.EastMoneyReverseFetcher.INDICATOR_SOURCE
    item.source.chip = service.EastMoneyReverseFetcher.CHIP_SOURCE

    try:
        daily_service._validate_sync_items_for_target_date(
            [item],
            target_trade_date="2026-06-26",
            code="920992",
        )
    finally:
        asyncio.run(daily_service.close())


def test_sync_one_reads_only_missing_write_range(
    monkeypatch,
) -> None:
    daily_service = service.StockDailyDetailService()
    captured = {}

    class FakeCrawler:
        async def build_stock_daily_details(self, **kwargs):
            captured.update(kwargs)
            return [build_complete_item("2026-07-10")]

    monkeypatch.setattr(
        daily_service,
        "get_latest_trade_date",
        lambda code, adjust: "2026-07-09",
    )
    monkeypatch.setattr(
        daily_service,
        "bulk_upsert",
        lambda items: len(list(items)),
    )

    async def run_test() -> int:
        try:
            return await daily_service._sync_one_with_crawler(
                crawler=FakeCrawler(),
                code="920992",
                name="中科美菱",
                default_start_date="20240101",
                end_date="20260710",
                adjust="qfq",
                target_trade_date="2026-07-10",
            )
        finally:
            await daily_service.close()

    affected = asyncio.run(run_test())

    assert affected == 1
    assert captured["start_date"] == "20260709"
    assert captured["write_start_date"] == "2026-07-09"
    assert captured["end_date"] == "20260710"


@pytest.mark.parametrize(
    ("configured_concurrency", "expected_concurrency"),
    [(None, 20), (8, 8)],
)
def test_reverse_queue_caps_workers_and_candidate_ip_budget(
    monkeypatch,
    configured_concurrency,
    expected_concurrency,
) -> None:
    daily_service = service.StockDailyDetailService(
        concurrency=configured_concurrency,
        request_sleep_seconds=0,
    )
    manager_configs = []
    worker_instances = []

    class FakeReverseManager:
        def __init__(self, **kwargs):
            manager_configs.append(kwargs)

        def report_stats(self):
            return {"manager": {}, "provider": {}}

        async def close(self):
            pass

    class FakeWorkerCrawler:
        def __init__(self, **kwargs):
            worker_instances.append(kwargs)

        async def sleep_after_request(self):
            pass

        async def close(self):
            pass

    async def fake_sync_one_with_crawler(**kwargs):
        return 1

    monkeypatch.setattr(service, "TargetAwareProxyManager", FakeReverseManager)
    monkeypatch.setattr(service, "StockDailyDetailCrawler", FakeWorkerCrawler)
    monkeypatch.setattr(
        daily_service,
        "_sync_one_with_crawler",
        fake_sync_one_with_crawler,
    )

    async def run_test():
        try:
            stock_rows = [
                (index, f"{index:06d}", f"股票{index}")
                for index in range(1, 501)
            ]
            return await daily_service._sync_stock_rows(
                stock_rows=stock_rows,
                total=len(stock_rows),
                default_start_date="20260730",
                end_date="20260730",
                adjust="qfq",
                target_trade_date="2026-07-30",
                retry_failed_once=False,
            )
        finally:
            await daily_service.close()

    results = asyncio.run(run_test())

    assert len(results) == 500
    assert len(worker_instances) == expected_concurrency
    assert len({id(item["reverse_fetcher"]) for item in worker_instances}) == 1
    assert manager_configs == [
        {
            "pool_size": expected_concurrency,
            "request_interval_seconds": 1.2,
            "max_target_requests_per_proxy": 40,
            "max_candidate_count": 40,
        }
    ]


def test_sync_queue_retries_all_non_terminal_failures_once(monkeypatch) -> None:
    daily_service = service.StockDailyDetailService(
        concurrency=4,
        request_sleep_seconds=0,
    )
    attempts: dict[str, int] = {}

    class FakeWorkerCrawler:
        def __init__(self, **kwargs):
            pass

        async def sleep_after_request(self):
            pass

        async def close(self):
            pass

    async def fake_sync_one_with_crawler(*, code, **kwargs):
        attempts[code] = attempts.get(code, 0) + 1
        if code == "000002" and attempts[code] == 1:
            raise TimeoutError("reverse request timeout")
        if code == "000003":
            raise RuntimeError("reverse chip unavailable")
        return 1

    monkeypatch.setattr(service, "StockDailyDetailCrawler", FakeWorkerCrawler)
    monkeypatch.setattr(
        daily_service,
        "_sync_one_with_crawler",
        fake_sync_one_with_crawler,
    )

    async def run_test():
        try:
            return await daily_service._sync_stock_rows(
                stock_rows=[
                    (1, "000001", "股票1"),
                    (2, "000002", "股票2"),
                    (3, "000003", "股票3"),
                ],
                total=3,
                default_start_date="20260713",
                end_date="20260713",
                adjust="qfq",
                target_trade_date="2026-07-13",
            )
        finally:
            await daily_service.close()

    results = asyncio.run(run_test())

    assert attempts == {"000001": 1, "000002": 2, "000003": 2}
    assert results[0].error is None
    assert results[1].error is None
    assert results[2].error == "reverse chip unavailable"


def test_retry_failed_run_uses_queue_and_reconciles_source(monkeypatch) -> None:
    daily_service = service.StockDailyDetailService(
        concurrency=4,
        request_sleep_seconds=0,
    )
    source_run = {
        "run_id": "source-run",
        "target_trade_date": "2026-07-14",
        "start_date": "20260714",
        "end_date": "20260714",
        "adjust": "qfq",
        "expected_count": 3,
        "success_count": 0,
        "failed_count": 3,
        "affected_total": 0,
        "failed_items": [
            {"code": "000001", "name": "股票1", "error": "reverse request timeout"},
            {"code": "000002", "name": "股票2", "error": "net::ERR_FAILED"},
            {"code": "000003", "name": "股票3", "error": "reverse chip unavailable"},
        ],
    }
    updates = []
    captured = {}

    class FakeRunCollection:
        def find_one(self, query, **kwargs):
            return dict(source_run) if query.get("run_id") == "source-run" else None

        def update_one(self, query, update, **kwargs):
            updates.append((query, update))

    async def fake_sync_stock_rows(**kwargs):
        captured.update(kwargs)
        return [
            service.StockDailyDetailItemResult(
                index=1,
                total=3,
                code="000001",
                name="股票1",
                affected=1,
            ),
            service.StockDailyDetailItemResult(
                index=2,
                total=3,
                code="000002",
                name="股票2",
                error="reverse request timeout",
            ),
            service.StockDailyDetailItemResult(
                index=3,
                total=3,
                code="000003",
                name="股票3",
                error="reverse chip unavailable",
            ),
        ]

    daily_service.sync_run_collection = FakeRunCollection()
    monkeypatch.setattr(daily_service, "ensure_indexes", lambda: None)
    monkeypatch.setattr(daily_service, "_build_run_id", lambda **kwargs: "retry-run")
    monkeypatch.setattr(daily_service, "_sync_stock_rows", fake_sync_stock_rows)

    async def run_test():
        try:
            return await daily_service.retry_failed_run("source-run")
        finally:
            await daily_service.close()

    result = asyncio.run(run_test())

    assert result.expected_count == 3
    assert result.success_count == 1
    assert result.failed_count == 2
    assert captured["retry_failed_once"] is False
    assert [row[1] for row in captured["stock_rows"]] == [
        "000001",
        "000002",
        "000003",
    ]
    source_update = next(
        update["$set"]
        for query, update in updates
        if query == {"run_id": "source-run"} and "last_retry_run_id" in update["$set"]
    )
    assert source_update["status"] == service.SYNC_STATUS_PARTIAL_FAILED
    assert source_update["success_count"] == 1
    assert source_update["failed_count"] == 2
    assert [item["code"] for item in source_update["failed_items"]] == [
        "000002",
        "000003",
    ]


def test_retryable_failures_are_not_permanently_exhausted(monkeypatch) -> None:
    updates = []

    class FakeService:
        def __init__(self, **kwargs):
            self.sync_run_collection = self

        def ensure_indexes(self):
            pass

        def find_latest_incomplete_full_run(self, **kwargs):
            return {
                "run_id": "source-run",
                "failed_items": [
                    {"code": "000001", "error": "reverse request timeout"}
                ],
                "automatic_compensation_count": 2,
            }

        def update_one(self, query, update):
            updates.append((query, update))

        async def retry_failed_run(self, run_id):
            return service.StockDailyDetailSyncResult(
                run_id="retry-run",
                target_trade_date="2026-07-14",
                adjust="qfq",
                run_mode="retry",
                scope_key="retry:source-run",
                expected_count=1,
                failed_count=1,
                status=service.SYNC_STATUS_FAILED,
            )

        async def close(self):
            pass

    monkeypatch.setattr(service, "StockDailyDetailService", FakeService)

    result = asyncio.run(
        service.retry_latest_incomplete_stock_daily_detail_run(
            "2026-07-14",
            max_automatic_compensations=2,
        )
    )

    assert result is not None
    assert updates[-1][0] == {"run_id": "source-run"}
    assert updates[-1][1]["$set"]["automatic_compensation_count"] == 3
    assert updates[-1][1]["$set"]["automatic_retry_exhausted"] is False


def test_only_terminal_reverse_failures_are_not_retryable() -> None:
    assert service.StockDailyDetailService._is_retryable_item_error(
        "ReverseNetworkError('connection reset')"
    )
    assert service.StockDailyDetailService._is_retryable_item_error(
        "reverse chip unavailable"
    )
    assert not service.StockDailyDetailService._is_retryable_item_error(
        "逆向接口响应异常: status=404"
    )
    assert not service.StockDailyDetailService._is_retryable_item_error(
        "逆向接口响应异常: status=410"
    )
    assert not service.StockDailyDetailService._is_retryable_item_error(
        "指定日期范围内没有日 K 数据"
    )
