import asyncio
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from app.crawlers.realtime_market_crawler import (
    EastMoneyQuoteProvider, RealtimeMarketCrawler, QuoteBatchResult, quote_issue,
)
from app.repositories.realtime_snapshot_repository import snapshot_row
from app.services.short_sentiment.facts import quote_fields
from tests.test_realtime_minute_persistence import quote


class Provider:
    def __init__(self, rows=()): self.rows=rows; self.calls=[]
    async def fetch_batch(self, codes):
        self.calls.append(codes)
        rows = tuple(q for q in self.rows if q.code in codes)
        return QuoteBatchResult("TEST", len(codes), len(rows), 200, 0, 0, rows)


def test_time_regression_triggers_fallback_and_preserves_rejected_evidence():
    async def run():
        crawler = RealtimeMarketCrawler(); await crawler.close()
        current = quote()
        crawler.primary = Provider([current]); crawler.backup = Provider(); crawler.third = Provider()
        await crawler.fetch_quotes([current.code])
        crawler.primary.rows = [replace(current, market_data_time=current.market_data_time-timedelta(seconds=2))]
        newer = replace(current, provider="SINA", market_data_time=current.market_data_time+timedelta(seconds=1))
        crawler.backup.rows = [newer]
        result, metrics = await crawler.fetch_quotes([current.code])
        assert result[0].provider == "SINA" and result[0].market_data_time == newer.market_data_time
        assert quote_issue(crawler.last_observations[0]) == "out_of_order"
        assert snapshot_row(crawler.last_observations[0])["quality_issue"] == "out_of_order"
        assert metrics["unusable_symbols"] == 0
    asyncio.run(run())


def test_static_limits_complete_sina_without_changing_its_price_volume_or_time():
    async def run():
        crawler = RealtimeMarketCrawler(); await crawler.close()
        current = replace(quote(), provider="TENCENT", previous_close=10, price=10.5,
                          limit_up_price=11, limit_down_price=9, limit_source="TENCENT")
        stale = replace(current, market_data_time=current.market_data_time-timedelta(minutes=2))
        sina = replace(current, provider="SINA", price=10.6, volume=200,
                       limit_up_price=None, limit_down_price=None, limit_source=None)
        crawler.primary = Provider([stale]); crawler.backup = Provider([sina]); crawler.third = Provider()
        result, metrics = await crawler.fetch_quotes([current.code])
        saved = snapshot_row(result[0])
        assert (saved["price"], saved["volume"], saved["provider"]) == (10.6, 200, "SINA")
        assert saved["limit_up_price"] == 11 and saved["limit_source"] == "TENCENT"
        assert saved["limit_source_time"] == stale.market_data_time.isoformat()
        assert quote_fields(saved)["limit_down_price"] == 9
        assert metrics["usable_with_daily_limits"] == 1
        assert not crawler.third.calls
    asyncio.run(run())


@pytest.mark.parametrize("change", ["date", "previous_close", "outside_limits"])
def test_static_limits_are_not_reused_across_day_or_conflicting_reference(change):
    async def run():
        crawler = RealtimeMarketCrawler(); await crawler.close()
        source = replace(quote(), previous_close=10, price=10, provider="TENCENT",
                         limit_up_price=11, limit_down_price=9)
        crawler._prepare_quote(source)
        sina = replace(source, provider="SINA", limit_up_price=None, limit_down_price=None)
        if change == "date":
            sina = replace(sina, market_data_time=sina.market_data_time+timedelta(days=1),
                           received_at=sina.received_at+timedelta(days=1))
        elif change == "previous_close": sina = replace(sina, previous_close=12)
        else: sina = replace(sina, price=12)
        assert crawler._prepare_quote(sina).limit_up_price is None
    asyncio.run(run())


def test_third_source_only_receives_unresolved_symbols_and_recovered_prices_are_used():
    async def run():
        crawler = RealtimeMarketCrawler(); await crawler.close()
        current = replace(quote(), previous_close=10, price=10, limit_up_price=11, limit_down_price=9)
        extra = replace(current, code="920002", provider="EASTMONEY")
        crawler.primary = Provider([current]); crawler.backup = Provider(); crawler.third = Provider([extra])
        rows, metrics = await crawler.fetch_quotes([current.code, extra.code])
        assert crawler.third.calls == [[extra.code]]
        assert len(rows) == 2 and metrics["missing_symbols"] == metrics["failed_batches"] == 0
        assert {q.provider for q in crawler.last_observations} >= {"EASTMONEY"}
    asyncio.run(run())


def test_stale_third_source_cannot_become_valid_or_overwrite_fresh_quote():
    async def run():
        crawler = RealtimeMarketCrawler(); await crawler.close()
        current = replace(quote(), provider="SINA")
        stale = replace(current, provider="EASTMONEY", market_data_time=current.market_data_time-timedelta(minutes=2),
                        limit_up_price=11, limit_down_price=9)
        crawler.primary = Provider(); crawler.backup = Provider([current]); crawler.third = Provider([stale])
        rows, metrics = await crawler.fetch_quotes([current.code])
        assert rows[0].provider == "SINA"
        assert any(quote_issue(q) == "stale_source_time" for q in crawler.last_observations)
    asyncio.run(run())


def test_eastmoney_parser_uses_yuan_shares_and_epoch_time_without_inventing_book():
    async def run():
        provider = EastMoneyQuoteProvider(); await provider.close()
        at = quote().received_at
        data = {"f43": 4578, "f44": 4713, "f45": 4526, "f46": 4550, "f47": 7425,
                "f48": 34267636.59, "f51": 6188, "f52": 3332, "f57": "920002", "f58": "测试股票",
                "f59": 2, "f60": 4760, "f86": int(at.timestamp()), "f168": 166}
        rows = provider._parse(json.dumps({"rc":0,"data":data}), at, {"920002"})
        q = rows[0]
        assert q.volume == 742500 and q.amount == 34267636.59
        assert (q.price, q.previous_close, q.limit_up_price, q.limit_down_price) == (45.78,47.6,61.88,33.32)
        assert q.turnover_pct == 1.66 and q.market_data_time == at
        assert q.bids == q.asks == ()
        data["f43"] = "-"
        assert provider._parse(json.dumps({"rc":0,"data":data}), at, {"920002"}) == []
    asyncio.run(run())
