import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.crawlers.market_observation_crawler import parse_hot_rank, assemble_billboard, MarketObservationCrawler
from app.crawlers.realtime_market_crawler import CN_TZ
from app.services.market_observation_service import MarketObservationService, hot_slot
from app.services import market_observation_service as service_module
from app.scheduler.market_observation_jobs import register_market_observation_jobs
from apscheduler.schedulers.asyncio import AsyncIOScheduler


def hot_payload():
    return {"status_code": 0, "data": {"stock_list": [
        {"code": f"{i:06d}", "order": i, "name": "测试股票", "rate": str(101-i)}
        for i in range(1, 101)]}}


def test_hot_rank_preserves_source_order_and_requires_complete_unique_100():
    source = hot_payload()
    rows = parse_hot_rank(source)
    assert len(rows) == 100 and rows[0]["rank"] == 1 and rows[-1]["rank"] == 100
    assert rows[0]["raw"] == source["data"]["stock_list"][0]
    for change in (lambda r: r.pop(), lambda r: r[1].update(code=r[0]["code"]),
                   lambda r: r[1].update(order=1), lambda r: r[0].update(rate="nan")):
        invalid = deepcopy(source)
        change(invalid["data"]["stock_list"])
        with pytest.raises(ValueError):
            parse_hot_rank(invalid)


def reports():
    rows = [{"SECURITY_CODE": "000001", "TRADE_DATE": "2026-10-15 00:00:00", "TRADE_ID": i,
             "EXPLANATION": f"原因{i}"} for i in (1, 2)]
    return {"summary": rows, "buy": [dict(r, BUY=100, OPERATEDEPT_NAME="机构专用") for r in rows]*2,
            "sell": [dict(r, SELL=100) for r in rows]}


def test_billboard_preserves_multiple_disclosures_duplicate_institutions_and_ignores_bond_seats():
    data = reports()
    data["buy"].append(dict(data["buy"][0], SECURITY_CODE="127096", TRADE_ID=3))
    result = assemble_billboard("2026-10-15", data)
    assert len(result) == 2
    assert len(result[0]["buy_seats"]) == 2
    assert result[0]["reason"] != result[1]["reason"]


def test_billboard_rejects_wrong_day_missing_seats_and_duplicate_disclosure():
    for change in (lambda d: d["sell"].pop(), lambda d: d["summary"].append(d["summary"][0]),
                   lambda d: d["buy"][0].update(TRADE_DATE="2026-10-14")):
        data = reports()
        change(data)
        with pytest.raises(ValueError):
            assemble_billboard("2026-10-15", data)


@pytest.mark.parametrize("clock,expected", [("09:29:59", None), ("09:30:06", "09:30"),
    ("11:30:10", "11:30"), ("11:31:00", None), ("12:55:00", None), ("13:00:08", "13:00"),
    ("14:59:59", "14:55"), ("15:00:10", "15:00"), ("15:01:00", None)])
def test_hot_slot_boundaries(clock, expected):
    result = hot_slot(datetime.fromisoformat("2026-10-15T"+clock+"+08:00"))
    assert (result.strftime("%H:%M") if result else None) == expected


class Repo:
    def __init__(self):
        self.docs = {}; self.runs = []
    async def create_indexes(self): pass
    async def exists_snapshot(self, kind, identity): return (kind, identity) in self.docs
    async def save(self, kind, identity, data): self.docs.setdefault((kind, identity), data)
    async def record_run(self, *args, **kwargs): self.runs.append((args, kwargs))


def test_collection_calendar_dedup_corrected_time_and_restart(monkeypatch):
    async def run():
        async def calendar(day): return SimpleNamespace(is_reference_trade_day=day != "20261017")
        monkeypatch.setattr(service_module, "resolve_a_stock_target_trade_date", calendar)
        now = datetime(2026, 10, 15, 10, 0, 6, tzinfo=CN_TZ)
        class Crawler:
            calls = 0
            async def fetch_hot_rank(self):
                self.calls += 1
                return {"fetched_at": now+timedelta(seconds=305), "stocks": parse_hot_rank(hot_payload())}
        repo, crawler = Repo(), Crawler()
        service = MarketObservationService(repo, crawler)
        result = await service.collect("hot", now=now, clock_offset=-305)
        assert result["status"] == "success" and result["count"] == 100
        document = repo.docs[("hot", result["id"])]
        assert document["observed_at"] == now and document["clock_offset_seconds"] == -305
        restarted = MarketObservationService(repo, crawler)
        assert (await restarted.collect("hot", now=now))["reason"] == "already_collected"
        assert (await restarted.collect("hot", now=now.replace(hour=12)))["reason"] == "outside_session"
        assert (await restarted.collect("hot", now=now.replace(day=17)))["reason"] == "non_trading_day"
        assert crawler.calls == 1
    asyncio.run(run())


def test_failed_collection_has_run_record_and_never_publishes_partial_data(monkeypatch):
    async def run():
        async def calendar(day): return SimpleNamespace(is_reference_trade_day=True)
        async def no_wait(*a): pass
        monkeypatch.setattr(service_module, "resolve_a_stock_target_trade_date", calendar)
        monkeypatch.setattr(service_module.asyncio, "sleep", no_wait)
        class Crawler:
            async def fetch_billboard(self, day): raise ValueError("incomplete")
        repo = Repo(); service = MarketObservationService(repo, Crawler())
        now = datetime(2026, 10, 15, 16, 0, tzinfo=CN_TZ)
        assert (await service.collect("billboard", now=now.replace(hour=15)))["reason"] == "before_1600"
        with pytest.raises(ValueError, match="incomplete"):
            await service.collect("billboard", now=now)
        assert not repo.docs and len(repo.runs) == 3
        assert all(r[1]["status"] == "failed" for r in repo.runs)
    asyncio.run(run())


def test_manual_hot_is_marked_as_actual_capture_not_historical_slot(monkeypatch):
    async def run():
        async def calendar(day): return SimpleNamespace(is_reference_trade_day=True)
        monkeypatch.setattr(service_module, "resolve_a_stock_target_trade_date", calendar)
        now = datetime(2026, 10, 15, 23, 0, tzinfo=CN_TZ)
        class Crawler:
            async def fetch_hot_rank(self): return {"fetched_at": now, "stocks": parse_hot_rank(hot_payload())}
        repo = Repo(); service = MarketObservationService(repo, Crawler())
        result = await service.collect("hot", now=now, manual=True)
        doc = repo.docs[("hot", result["id"])]
        assert result["id"].startswith("manual:") and doc["scheduled_slot"] is None
        assert doc["run_mode"] == "manual"
        with pytest.raises(ValueError, match="no_historical"):
            await service.collect("hot", now=now, manual=True, trade_date="2026-10-14")
    asyncio.run(run())


def test_billboard_pagination_fetches_all_pages_and_detects_source_change():
    async def run():
        crawler = MarketObservationCrawler()
        await crawler.close()
        class Client:
            count = 2
            async def get(self, url, params):
                page = params["pageNumber"]
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                    "success": True, "result": {"count": 2 if page == 1 else self.count,
                    "pages": 2, "data": [{"page": page}]}})
        crawler.client = Client()
        assert await crawler._report("report", "2026-10-15") == [{"page": 1}, {"page": 2}]
        crawler.client.count = 3
        with pytest.raises(ValueError, match="changed_during"):
            await crawler._report("report", "2026-10-15")
    asyncio.run(run())


def test_observation_jobs_are_unique_and_use_shanghai_schedule():
    scheduler = AsyncIOScheduler()
    register_market_observation_jobs(scheduler)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert len(jobs) == 4
    now = datetime(2026, 10, 15, 15, 59, tzinfo=CN_TZ)
    assert jobs["stock_billboard_1600"].trigger.get_next_fire_time(None, now) == now.replace(hour=16, minute=0)
    assert jobs["ths_hot_rank_5m"].trigger.get_next_fire_time(None, now) == now.replace(hour=16, minute=0, second=10)


def test_billboard_retries_late_publication_every_quarter_hour_on_weekdays():
    scheduler = AsyncIOScheduler()
    register_market_observation_jobs(scheduler)
    job = next(job for job in scheduler.get_jobs() if job.id == "stock_billboard_1600")
    first = datetime(2026, 10, 16, 16, 0, tzinfo=CN_TZ)
    assert job.trigger.get_next_fire_time(first, first) == first.replace(minute=15)
    assert job.trigger.get_next_fire_time(first.replace(minute=45), first.replace(minute=45)) == first.replace(hour=17)
    last = first.replace(hour=23, minute=45)
    assert job.trigger.get_next_fire_time(last, last) == first.replace(day=19)
    assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time == 1800


def test_billboard_next_run_recovers_after_three_failures_and_then_skips(monkeypatch):
    async def run():
        async def calendar(day): return SimpleNamespace(is_reference_trade_day=True)
        async def no_wait(*args): pass
        monkeypatch.setattr(service_module, "resolve_a_stock_target_trade_date", calendar)
        monkeypatch.setattr(service_module.asyncio, "sleep", no_wait)
        now = datetime(2026, 10, 15, 16, 0, tzinfo=CN_TZ)

        class Crawler:
            calls = 0
            async def fetch_billboard(self, day):
                self.calls += 1
                if self.calls <= 3:
                    raise ValueError("billboard_report_unavailable:buy")
                return {"fetched_at": now + timedelta(minutes=20),
                        "records": assemble_billboard(day, reports())}

        repo, crawler = Repo(), Crawler()
        service = MarketObservationService(repo, crawler)
        with pytest.raises(ValueError, match="report_unavailable"):
            await service.collect("billboard", now=now, clock_offset=-300)
        assert not repo.docs
        result = await service.collect("billboard", now=now+timedelta(minutes=15), clock_offset=-300)
        assert result["status"] == "success"
        doc = repo.docs[("billboard", "2026-10-15")]
        assert doc["observed_at"] == now + timedelta(minutes=15)
        assert len(doc["records"]) == 2 and repo.runs[-1][1]["status"] == "success"
        result = await MarketObservationService(repo, crawler).collect(
            "billboard", now=now+timedelta(minutes=30), clock_offset=-300)
        assert result["reason"] == "already_collected" and crawler.calls == 4
    asyncio.run(run())
