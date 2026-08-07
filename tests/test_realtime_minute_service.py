from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import app.services.realtime_minute_service as realtime_minute_module
from app.crawlers.realtime_market_crawler import (
    SinaQuoteProvider,
    TencentQuoteProvider,
    market_prefix,
    quote_volume_multiplier,
)
from app.models.realtime_minute_bar import RealtimeMinuteBar
from app.scheduler.crawler_jobs import register_realtime_minute_jobs
from app.services.realtime_minute_service import RealtimeMinuteService


CN_TZ = timezone(timedelta(hours=8))


def test_market_prefix_covers_three_a_share_markets() -> None:
    assert market_prefix("600519") == "sh"
    assert market_prefix("000001") == "sz"
    assert market_prefix("920001") == "bj"


def test_tencent_volume_unit_differs_for_star_market() -> None:
    assert quote_volume_multiplier("600519") == 100
    assert quote_volume_multiplier("300750") == 100
    assert quote_volume_multiplier("920992") == 100
    assert quote_volume_multiplier("688981") == 1
    assert quote_volume_multiplier("689009") == 1


def test_realtime_jobs_are_registered_at_session_boundaries() -> None:
    registered = []

    class FakeScheduler:
        def add_job(self, func, **kwargs):
            registered.append((func, kwargs))
            return type("Job", (), {"id": kwargs["id"]})()

    register_realtime_minute_jobs(FakeScheduler())
    jobs = {kwargs["id"]: kwargs for _, kwargs in registered}
    assert set(jobs) == {
        "realtime_minute_morning",
        "realtime_minute_afternoon",
        "realtime_minute_startup_resume",
    }
    assert jobs["realtime_minute_morning"]["kwargs"] == {"session": "morning"}
    assert jobs["realtime_minute_afternoon"]["kwargs"] == {"session": "afternoon"}
    assert str(jobs["realtime_minute_morning"]["trigger"].fields[4]) == "mon-fri"


def test_universe_refresh_merges_public_target_date_with_mongo(monkeypatch) -> None:
    monkeypatch.setattr(realtime_minute_module, "MIN_UNIVERSE_SYMBOLS", 1)

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        async def to_list(self, length=None):
            return list(self.rows)

    class Collection:
        async def find_one(self, *args, **kwargs):
            return {"trade_date": "2026-08-06"}

        def find(self, *args, **kwargs):
            return Cursor([{"code": "000001", "name": "旧名称"}])

    class Database:
        def __getitem__(self, name):
            assert name == "stock_daily_detail"
            return Collection()

    class Frame:
        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"代码": "000001", "名称": "平安银行"},
                {"代码": "301707", "名称": "N展芯"},
            ]

    class FreshUniverseCrawler:
        def __init__(self):
            self.target_trade_date = None

        async def fetch_stock_list(self, *, target_trade_date=None):
            self.target_trade_date = target_trade_date
            return Frame()

        async def close(self):
            return None

    service = RealtimeMinuteService()
    service.repository.database = Database()
    asyncio.run(service.universe_crawler.close())
    fresh_crawler = FreshUniverseCrawler()
    service.universe_crawler = fresh_crawler
    try:
        rows = asyncio.run(service._load_universe("2026-08-07"))
    finally:
        asyncio.run(service.close())

    assert fresh_crawler.target_trade_date == "2026-08-07"
    assert [row["code"] for row in rows] == ["000001", "301707"]
    assert rows[0]["name"] == "平安银行"


def test_public_quote_parsers_normalize_volume_and_amount() -> None:
    received = datetime(2026, 8, 6, 10, 0, tzinfo=CN_TZ)
    tencent = TencentQuoteProvider()
    sina = SinaQuoteProvider()
    try:
        t_quotes = tencent._parse(
            'v_sh600519="1~贵州茅台~600519~1300~1290~1295~10~1~2~1300~1~1299~1~1298~1~1297~1~1296~1~1301~1~1302~1~1303~1~1304~1~1305~1~~20260806100000~10~0.7~1310~1280~1300/10/12345~10~12345~0.1~20";',
            received,
            {"600519"},
        )
        star_quotes = tencent._parse(
            'v_sh688981="1~中芯国际~688981~128.5~128~128~10~1~2~128.5~1~128.4~1~128.3~1~128.2~1~128.1~1~128.6~1~128.7~1~128.8~1~128.9~1~129~1~~20260806100000~10~0.7~131~127~128.5/10/1285~10~1285~0.1~20";',
            received,
            {"688981"},
        )
        s_quotes = sina._parse(
            'var hq_str_sh600519="贵州茅台,1290,1290,1300,1310,1280,1300,1301,1000,12345,1,1300,1,1299,1,1298,1,1297,1,1296,1,1301,1,1302,1,1303,1,1304,1,1305,2026-08-06,10:00:00,00,D";',
            received,
            {"600519"},
        )
        assert t_quotes[0].volume == 1000
        assert t_quotes[0].amount == 12345
        assert star_quotes[0].volume == 10
        assert star_quotes[0].amount == 1285
        assert s_quotes[0].volume == 1000
        assert s_quotes[0].amount == 12345
    finally:
        import asyncio

        asyncio.run(tencent.close())
        asyncio.run(sina.close())


def test_local_aggregation_uses_cumulative_volume_delta() -> None:
    service = RealtimeMinuteService()
    try:
        first_seen = datetime(2026, 8, 6, 9, 30, 5, tzinfo=CN_TZ)
        service._ingest_quote(
            service_quote(
                price=10.0,
                volume=1000,
                amount=10000,
                received_at=first_seen,
            )
        )
        service._ingest_quote(
            service_quote(
                price=10.2,
                volume=1300,
                amount=13060,
                received_at=first_seen.replace(second=10),
            )
        )
        bar = service._bars[("000001", "2026-08-06T09:30:00+08:00")]
        assert bar.open == 10.0
        assert bar.high == 10.2
        assert bar.low == 10.0
        assert bar.close == 10.2
        assert bar.volume == 300
        assert bar.amount == 3060
        model = bar.to_model()
        assert isinstance(model, RealtimeMinuteBar)
        assert model.interval == "1m"
    finally:
        asyncio.run(service.close())


def test_repeated_stale_snapshot_is_not_recreated_after_flush() -> None:
    class FakeRepository:
        async def upsert_bars(self, bars):
            return len(list(bars))

    async def run() -> None:
        service = RealtimeMinuteService()
        service.repository = FakeRepository()  # type: ignore[assignment]
        quote = service_quote(
            price=10.0,
            volume=1000,
            amount=10000,
            received_at=datetime(2026, 8, 7, 9, 30, 5, tzinfo=CN_TZ),
        )
        try:
            service._ingest_quote(quote)
            await service._flush_before("2026-08-07T09:31:00+08:00")
            service._ingest_quote(quote)
            assert service._bars == {}
        finally:
            await service.close()

    asyncio.run(run())


def test_session_end_snapshot_is_assigned_to_last_trading_minute() -> None:
    quote = service_quote(
        price=10.0,
        volume=1000,
        amount=10000,
        received_at=datetime(2026, 8, 7, 15, 0, 3, tzinfo=CN_TZ),
    )
    bar_time = RealtimeMinuteService._bar_time_in_session(
        quote,
        datetime(2026, 8, 7, 15, 2, tzinfo=CN_TZ),
        datetime(2026, 8, 7, 13, 0, tzinfo=CN_TZ).time(),
        datetime(2026, 8, 7, 15, 0, tzinfo=CN_TZ).time(),
    )
    assert bar_time == datetime(2026, 8, 7, 14, 59, 59, 999999, tzinfo=CN_TZ)


def test_morning_session_end_is_kept_in_the_1129_minute() -> None:
    quote = service_quote(
        price=10.0,
        volume=1000,
        amount=10000,
        received_at=datetime(2026, 8, 7, 11, 30, 3, tzinfo=CN_TZ),
    )
    bar_time = RealtimeMinuteService._bar_time_in_session(
        quote,
        datetime(2026, 8, 7, 11, 32, tzinfo=CN_TZ),
        datetime(2026, 8, 7, 9, 30, tzinfo=CN_TZ).time(),
        datetime(2026, 8, 7, 11, 30, tzinfo=CN_TZ).time(),
    )
    assert bar_time == datetime(2026, 8, 7, 11, 29, 59, 999999, tzinfo=CN_TZ)


def test_close_auction_updates_last_minute_and_final_bucket() -> None:
    class FakeRepository:
        def __init__(self) -> None:
            self.saved = []

        async def upsert_bars(self, bars):
            self.saved = list(bars)
            return len(self.saved)

    async def run() -> list[RealtimeMinuteBar]:
        service = RealtimeMinuteService()
        repository = FakeRepository()
        service.repository = repository  # type: ignore[assignment]
        try:
            service._ingest_quote(
                service_quote(
                    price=10.0,
                    volume=1000,
                    amount=10000,
                    received_at=datetime(2026, 8, 7, 14, 59, 55, tzinfo=CN_TZ),
                )
            )
            close_quote = service_quote(
                price=10.1,
                volume=1500,
                amount=15050,
                received_at=datetime(2026, 8, 7, 15, 0, 5, tzinfo=CN_TZ),
            )
            close_bar_time = RealtimeMinuteService._bar_time_in_session(
                close_quote,
                datetime(2026, 8, 7, 15, 2, tzinfo=CN_TZ),
                datetime(2026, 8, 7, 13, 0, tzinfo=CN_TZ).time(),
                datetime(2026, 8, 7, 15, 0, tzinfo=CN_TZ).time(),
            )
            service._ingest_quote(close_quote, bar_time=close_bar_time)
            await service._flush_before(None, force=True)
            return repository.saved
        finally:
            await service.close()

    saved = asyncio.run(run())
    one_minute = sorted(
        (bar for bar in saved if bar.interval == "1m"),
        key=lambda bar: bar.timestamp,
    )
    assert [bar.timestamp for bar in one_minute] == ["2026-08-07T14:59:00+08:00"]
    final_five_minute = next(
        bar
        for bar in saved
        if bar.interval == "5m" and bar.timestamp == "2026-08-07T14:55:00+08:00"
    )
    assert final_five_minute.close == 10.1
    assert final_five_minute.volume == 500


def test_source_clock_offset_corrects_server_receive_time() -> None:
    service = RealtimeMinuteService()
    try:
        quote = service_quote(
            price=10.0,
            volume=1000,
            amount=10000,
            received_at=datetime(2026, 8, 7, 9, 33, 5, tzinfo=CN_TZ),
        )
        quote = replace(
            quote,
            market_data_time=datetime(2026, 8, 7, 9, 30, 5, tzinfo=CN_TZ),
        )
        service._observe_source_clock(quote)
        corrected = service._exchange_now(quote.received_at)
        assert corrected == datetime(2026, 8, 7, 9, 30, 5, tzinfo=CN_TZ)
    finally:
        asyncio.run(service.close())


def test_multi_period_buckets_are_aligned_to_each_market_session() -> None:
    assert RealtimeMinuteService._period_bucket_start(
        datetime(2026, 8, 7, 10, 31, tzinfo=CN_TZ), 60
    ) == "2026-08-07T10:30:00+08:00"
    assert RealtimeMinuteService._period_bucket_start(
        datetime(2026, 8, 7, 11, 29, tzinfo=CN_TZ), 120
    ) == "2026-08-07T09:30:00+08:00"
    assert RealtimeMinuteService._period_bucket_start(
        datetime(2026, 8, 7, 13, 1, tzinfo=CN_TZ), 120
    ) == "2026-08-07T13:00:00+08:00"


def test_flush_writes_one_and_all_requested_aggregate_intervals() -> None:
    class FakeRepository:
        def __init__(self) -> None:
            self.saved = []

        async def upsert_bars(self, bars):
            self.saved = list(bars)
            return len(self.saved)

    async def run() -> list[RealtimeMinuteBar]:
        service = RealtimeMinuteService()
        repository = FakeRepository()
        service.repository = repository  # type: ignore[assignment]
        try:
            service._ingest_quote(
                service_quote(
                    price=10.0,
                    volume=1000,
                    amount=10000,
                    received_at=datetime(2026, 8, 7, 9, 30, 5, tzinfo=CN_TZ),
                )
            )
            service._ingest_quote(
                service_quote(
                    price=10.2,
                    volume=1300,
                    amount=13060,
                    received_at=datetime(2026, 8, 7, 9, 30, 10, tzinfo=CN_TZ),
                )
            )
            service._ingest_quote(
                service_quote(
                    price=10.1,
                    volume=1600,
                    amount=16090,
                    received_at=datetime(2026, 8, 7, 9, 31, 5, tzinfo=CN_TZ),
                )
            )
            written = await service._flush_before("2026-08-07T09:32:00+08:00")
            assert written == 7
            return repository.saved
        finally:
            await service.close()

    saved = asyncio.run(run())
    assert sorted(bar.interval for bar in saved) == [
        "120m",
        "15m",
        "1m",
        "1m",
        "30m",
        "5m",
        "60m",
    ]
    five_minute = next(bar for bar in saved if bar.interval == "5m")
    assert five_minute.timestamp == "2026-08-07T09:30:00+08:00"
    assert five_minute.open == 10.0
    assert five_minute.high == 10.2
    assert five_minute.low == 10.0
    assert five_minute.close == 10.1
    assert five_minute.volume == 600
    assert five_minute.amount == 6090


def service_quote(*, price: float, volume: float, amount: float, received_at: datetime):
    from app.crawlers.realtime_market_crawler import RealtimeQuote

    return RealtimeQuote(
        code="000001",
        name="平安银行",
        market="SZ",
        provider="TENCENT",
        price=price,
        volume=volume,
        amount=amount,
        market_data_time=received_at,
        received_at=received_at,
    )
