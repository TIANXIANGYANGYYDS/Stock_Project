import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_db
from app.api.routers.sentiment import stream_frames
from app.repositories.short_sentiment_repository import EVENTS, SNAPSHOTS, TIMESERIES, ShortSentimentRepository
from app.repositories.realtime_snapshot_repository import AUCTION_COLLECTION, SNAPSHOT_COLLECTION, encode_batch
from app.services.short_sentiment.engine import SentimentEngine, daily_envelope
from app.services.short_sentiment.facts import CN, board_run, quote_fields
from app.services.short_sentiment.metrics import aggregate
from app.services.short_sentiment.service import SentimentQueryService, SentimentWorker
from app.services.short_sentiment.themes import ThemeCatalog
from tests.api.conftest import FakeCollection, FakeCursor, FakeDatabase, matches
from tests.test_market_sentiment_recording import stock as daily_stock


DAY = "2026-09-03"
CATALOG = ThemeCatalog(groups={"机器人": {"成分股": [{"股票代码": "000001", "股票名称": "甲"}, {"股票代码": "000002", "股票名称": "乙"}]},
                               "汽车": {"成分股": [{"股票代码": "000001", "股票名称": "甲"}]}})


def raw_day(day="2026-09-02", codes=("000001", "000002"), up=True):
    return {"scope": "hs_mainboard", "trade_date": day, "version": "daily-observation-v2", "input_hash": day,
            "market": {"trade_date": day, "expected_codes": list(codes), "stocks": [daily_stock(c, close=11 if up else 10) for c in codes]},
            "limit_streaks": {c: 2 for c in codes} if up else {}, "provenance": {}}


def quote(code="000001", price=11, at="10:00:00", **kw):
    timestamp = f"{DAY}T{at}+08:00"
    return {"code": code, "name": "测试股票", "price": price, "previous_close": 10,
            "open_price": 10.1, "amount": 1000000., "limit_up_price": 11, "limit_down_price": 9,
            "market_data_time": timestamp, "received_at": timestamp, "phase": "continuous", "provider": "fixture",
            "bids": [[11, 1000]], "asks": [[0, 0]], "quality_issue": None, **kw}


def envelope(quotes=None, at="10:00:00", expected=("000001", "000002")):
    return {"observed_at": f"{DAY}T{at}+08:00", "session": "continuous", "expected_codes": list(expected),
            "quotes": quotes if quotes is not None else [quote(c, at=at) for c in expected]}


def engine(scope="all_a"):
    previous = raw_day()
    previous["scope"] = scope
    return SentimentEngine(CATALOG, [previous], ["2026-09-02"], scope=scope)


class WriteCollection(FakeCollection):
    async def create_index(self, *args, **kwargs):
        return "index"

    async def update_one(self, filters, update, upsert=False):
        found = next((r for r in self.rows if matches(r, filters)), None)
        if found is None and upsert:
            found = dict(filters)
            found.update(deepcopy(update.get("$setOnInsert", {})))
            self.rows.append(found)
        if found is not None:
            found.update(deepcopy(update.get("$set", {})))

    async def bulk_write(self, operations, ordered=True):
        for op in operations:
            await self.update_one(op._filter, op._doc, op._upsert)

    async def delete_many(self, filters):
        self.rows[:] = [r for r in self.rows if not matches(r, filters)]

    def aggregate(self, pipeline):
        if not any(stage.get("$group", {}).get("_id") == "$timestamp" for stage in pipeline):
            return super().aggregate(pipeline)
        rows = self.find(pipeline[0]["$match"]).sort([("timestamp", 1), ("sequence", -1)]).rows
        grouped = {}
        for row in rows:
            grouped.setdefault(row["timestamp"], row)
        return FakeCursor(list(grouped.values()))


class WriteDatabase(FakeDatabase):
    def __init__(self):
        super().__init__({})

    def __getitem__(self, name):
        return self.collections.setdefault(name, WriteCollection())


def test_ladder_deduplicates_market_but_keeps_theme_memberships():
    result = engine().process(envelope())
    service = SentimentQueryService(WriteDatabase())
    ladder = service.ladder(result)
    assert ladder["unique_count"] == 2
    assert ladder["membership_count"] == 3
    assert result["market"]["values"]["limit_up"] == 2
    assert [s["intraday_streak"] for s in result["stocks"].values()] == [3, 3]
    assert all(s["attributed_theme_id"] is None for s in result["stocks"].values())


def test_board_run_eleven_days_eight_boards_and_consecutive_height_are_distinct():
    values = [True, True, False, True, True, False, True, False, True, True]
    result = board_run([{"is_limit_up": v} for v in values], True)
    assert result["label"] == "11天8板"
    assert result["is_final"] is False
    assert board_run([{"is_limit_up": True}, {"is_limit_up": None}], True)["label"] == "1天1板"
    assert board_run([], False)["label"] is None


def test_open_reseal_and_duplicate_source_timestamps():
    worker = engine()
    first = worker.process(envelope())
    opened = worker.process(envelope([quote(price=10.8, at="10:00:05"), quote("000002", at="10:00:05")], "10:00:05"), first)
    assert [e["type"] for e in opened["events"]] == ["open"]
    assert opened["stocks"]["000001"]["intraday_streak"] == 0
    assert opened["stocks"]["000001"]["target_streak"] == 3
    resealed = worker.process(envelope(at="10:00:10"), opened)
    assert [e["type"] for e in resealed["events"]] == ["reseal"]
    assert resealed["stocks"]["000001"]["observed_open_count"] == 1
    duplicate = worker.process(envelope(at="10:00:10"), resealed)
    assert duplicate["events"] == []
    assert duplicate["stocks"]["000001"]["observed_open_count"] == 1


@pytest.mark.parametrize("mode", ["gap", "missing", "out_of_order", "auction"])
def test_data_discontinuity_cannot_invent_open_events(mode):
    worker = engine()
    first = worker.process(envelope())
    q = quote(price=10.8, at="10:00:05")
    at = "10:00:05"
    if mode == "gap":
        at = "10:01:00"
        q = quote(price=10.8, at=at)
    elif mode == "missing":
        first = worker.process(envelope([], at=at), first)
        at = "10:00:10"
        q = quote(price=10.8, at=at)
    elif mode == "out_of_order":
        q = quote(price=10.8, at="09:59:59")
    else:
        q["phase"] = "closing_auction"
    result = worker.process(envelope([q, quote("000002", at=at)], at=at), first)
    assert not any(e["type"] == "open" for e in result["events"])
    assert result["stocks"]["000001"]["observed_open_count"] == 0


def test_provider_high_only_does_not_invent_first_touch_time():
    result = engine().process(envelope([quote(price=10.8, high_price=11), quote("000002")]))
    stock = result["stocks"]["000001"]
    assert stock["touched_up"] is True
    assert stock["first_touch_at"] is None
    assert stock["limit_status"] == "opened"


def test_missing_or_rounded_limits_never_use_percentage_as_limit():
    q = quote(price=10.99, limit_up_price=None, limit_down_price=None)
    result = engine().process(envelope([q, quote("000002")]))
    assert result["stocks"]["000001"]["is_limit_up"] is None
    assert result["market"]["values"]["limit_up"] is None
    assert result["market"]["values"]["coverage"] == .5
    raw = [""] * 49
    raw[47], raw[48], raw[38] = "11", "9", "1.25"
    extracted = quote_fields({"provider": "TENCENT", "raw_fields": raw})
    assert extracted["limit_up_price"] == 11
    assert extracted["turnover_pct"] == 1.25


def test_yesterday_cohort_remains_fixed_when_stock_becomes_st_or_missing():
    result = engine().process(envelope([quote(), quote("000002", price=9, name="ST测试")]))
    feedback = result["market"]["feedback"]["yesterday_limit_up"]
    assert feedback["sample_count"] == 2
    assert feedback["promotion"] == .5
    assert feedback["return_median"] == 0
    assert result["market"]["values"]["eligible"] == 1
    assert all(e.get("code") != "000002" for e in result["events"])
    missing = engine().process(envelope([quote()]))
    assert missing["market"]["feedback"]["yesterday_limit_up"]["promotion"] is None


@pytest.mark.parametrize("final", [False, True])
def test_suspended_yesterday_winner_keeps_cohort_incomplete(final):
    previous = raw_day()
    previous["limit_streaks"] = {"000001": 1, "000002": 1}
    worker = SentimentEngine(CATALOG, [previous], ["2026-09-02"], scope="hs_mainboard")
    if final:
        current = raw_day(DAY)
        current["market"]["stocks"][1]["is_suspended"] = True
        captured = daily_envelope(current)
    else:
        captured = envelope([quote(), quote("000002", price=10, is_suspended=True)])
    result = worker.process(captured, final=final)
    suspended = result["stocks"]["000002"]
    assert suspended["excluded_reason"] == "suspended"
    assert suspended["valid"] is False
    feedback = result["market"]["feedback"]["yesterday_first_board"]
    assert feedback["sample_count"] == 2
    assert feedback["observed_count"] == 1
    assert feedback["promotion"] is None
    assert feedback["return_median"] is None
    assert feedback["missing_reason"] == "incomplete_current_cohort"
    assert result["market"]["values"]["one_to_two"] is None


def test_daily_finalization_preserves_observed_times_and_excludes_st():
    worker = engine()
    intraday = worker.process(envelope())
    raw = raw_day(DAY)
    raw["market"]["stocks"][1]["is_st"] = True
    result = worker.process(daily_envelope(raw, intraday), intraday, final=True)
    assert result["meta"]["is_final"] is True
    assert result["market"]["values"]["limit_up"] == 1
    assert result["stocks"]["000001"]["first_touch_at"] == intraday["stocks"]["000001"]["first_touch_at"]
    assert result["stocks"]["000001"]["seal_amount_yuan"] is None
    assert not any(e["type"] in {"first_touch", "open", "reseal"} for e in result["events"])


def test_daily_bootstrap_does_not_invent_fifteen_oclock_touch_time():
    result = engine().process(daily_envelope(raw_day(DAY)), final=True)
    stock = result["stocks"]["000001"]
    assert stock["first_touch_at"] is None
    assert stock["first_seal_at"] is None
    assert stock["last_reseal_at"] is None
    assert stock["observed_open_count"] == 0


def test_daily_finalization_after_postclose_quotes_preserves_intraday_path():
    worker = engine()
    live = worker.process(envelope())
    after_close = worker.process(envelope([
        quote(c, at="15:04:00", phase="outside_session") for c in ("000001", "000002")
    ], at="15:04:05"), live)
    assert after_close["stocks"]["000001"]["valid"] is False
    final = worker.process(daily_envelope(raw_day(DAY), after_close), after_close, final=True)
    assert final["market"]["values"]["coverage"] == 1
    assert final["market"]["values"]["limit_up"] == 2
    for code in ("000001", "000002"):
        stock = final["stocks"][code]
        assert stock["valid"] is True
        assert stock["source_time"] == f"{DAY}T15:00:00+08:00"
        assert stock["price"] == 11
        assert "quote" not in stock["missing_reasons"]
        for field in ("first_touch_at", "first_seal_at", "last_open_at", "last_reseal_at", "observed_open_count"):
            assert stock[field] == live["stocks"][code][field]
    assert not any(e["type"] in {"first_touch", "first_seal", "open", "reseal", "limit_down"} for e in final["events"])


def test_daily_finalization_reuses_raw_price_and_status_validation():
    raw = raw_day(DAY)
    raw["market"]["stocks"][0]["raw_high"] = 10
    result = engine().process(daily_envelope(raw), final=True)
    assert result["stocks"]["000001"]["eligible"] is False
    assert result["stocks"]["000001"]["missing_reasons"]["quote"] == "invalid_ohlc"
    assert result["market"]["values"]["coverage"] == .5


def test_historical_codes_do_not_pollute_current_universe_or_shrink_yesterday_cohort():
    result = engine().process(envelope([quote(), quote("000002"), quote("000003", quality_issue="stale_source_time")],
                                        expected=("000001", "000002", "000003")))
    assert set(result["stocks"]) == {"000001", "000002"}
    raw = raw_day(DAY, codes=("000001",))
    final = engine().process(daily_envelope(raw, result), result, final=True)
    assert final["market"]["values"]["eligible"] == 1
    assert final["market"]["values"]["coverage"] == 1
    assert final["market"]["feedback"]["yesterday_limit_up"]["sample_count"] == 2
    assert final["market"]["feedback"]["yesterday_limit_up"]["promotion"] is None


def test_small_theme_has_insufficient_data_not_a_forced_leader_or_phase():
    result = engine().process(envelope())
    assert all(t["emotion"]["status"] == ("unclassified" if t["theme_id"] == "unclassified" else "insufficient_data") for t in result["themes"])


def test_missing_book_does_not_turn_limit_price_into_verified_seal():
    result = engine().process(envelope([quote(bids=[], asks=[]), quote("000002")]))
    stock = result["stocks"]["000001"]
    assert stock["limit_status"] == "at_limit"
    assert stock["is_sealed"] is None
    assert stock["seal_amount_yuan"] is None


def test_lunch_does_not_flag_complete_morning_snapshot_as_stale():
    result = engine().process(envelope(at="11:30:00"))
    service = SentimentQueryService(WriteDatabase())
    noon = datetime(2026, 9, 3, 12, tzinfo=CN)
    assert service.meta(result, now=noon)["is_stale"] is False
    result["meta"]["as_of"] = f"{DAY}T11:20:00+08:00"
    assert service.meta(result, now=noon)["is_stale"] is True


@pytest.mark.parametrize("offset", [-300.0, 300.0])
def test_calibrated_capture_clock_ages_fresh_and_stopped_observations(offset):
    captured = envelope()
    captured["clock_offset_seconds"] = offset
    result = engine().process(captured)
    service = SentimentQueryService(WriteDatabase())
    observed = datetime.fromisoformat(captured["observed_at"])
    server_now = observed - timedelta(seconds=offset) + timedelta(seconds=5)
    fresh = service.meta(result, now=server_now)
    assert fresh["clock_basis"] == "calibrated_capture"
    assert fresh["age_seconds"] == 5
    assert fresh["is_stale"] is False
    frozen = service.meta(result, now=server_now + timedelta(seconds=120))
    assert frozen["age_seconds"] == 125
    assert frozen["is_stale"] is True


def test_calibrated_clock_controls_lunch_and_afternoon_staleness():
    captured = envelope(at="11:30:00")
    captured["clock_offset_seconds"] = -300.0
    result = engine().process(captured)
    service = SentimentQueryService(WriteDatabase())
    assert service.meta(result, now=datetime(2026, 9, 3, 13, 3, tzinfo=CN))["is_stale"] is False
    assert service.meta(result, now=datetime(2026, 9, 3, 13, 6, tzinfo=CN))["is_stale"] is True


def test_missing_previous_day_is_not_interpreted_as_first_board():
    worker = SentimentEngine(CATALOG, [raw_day("2026-09-01")], ["2026-09-01", "2026-09-02"])
    result = worker.process(envelope())
    assert result["stocks"]["000001"]["intraday_streak"] is None
    assert result["market"]["values"]["height"] is None


def test_publication_failure_is_invisible_and_retry_is_idempotent():
    async def run():
        db = WriteDatabase()
        repo = ShortSentimentRepository(db)
        first = engine().process(envelope())
        original = db[SNAPSHOTS].update_one
        async def fail(*args, **kwargs):
            raise RuntimeError("publication interrupted")
        db[SNAPSHOTS].update_one = fail
        with pytest.raises(RuntimeError):
            await repo.publish(deepcopy(first))
        assert await repo.latest() is None
        assert db[EVENTS].rows
        db[SNAPSHOTS].update_one = original
        await repo.publish(deepcopy(first))
        await repo.publish(deepcopy(first))
        restored = await repo.latest()
        assert restored["meta"]["snapshot_id"] == first["meta"]["snapshot_id"]
        assert len(db[EVENTS].rows) == len(first["events"])
        assert len([r for r in db[TIMESERIES].rows if r["interval"] == "1m"]) == 1
        assert len([r for r in db[TIMESERIES].rows if r["interval"] == "5m"]) == 1
        page = await repo.events(restored, limit=1)
        assert page["has_more"] is True
        second = await repo.events(restored, after=page["next_cursor"], limit=100)
        assert len(second["items"]) == len(first["events"]) - 1
    asyncio.run(run())


def test_retry_of_unpublished_sequence_removes_orphan_events_and_cannot_regress():
    async def run():
        db = WriteDatabase()
        repo = ShortSentimentRepository(db)
        first = engine().process(envelope())
        db[EVENTS].rows.append({"_id": "orphan", "event_id": "orphan", "scope": "all_a", "trade_date": DAY, "sequence": 1})
        await repo.publish(first)
        assert all(e["event_id"] != "orphan" for e in db[EVENTS].rows)
        different = deepcopy(first)
        different["input_id"] = "different_source"
        with pytest.raises(ValueError, match="拒绝覆盖"):
            await repo.publish(different)
        second = engine().process(envelope(at="10:00:05"), first)
        await repo.publish(second)
        with pytest.raises(ValueError, match="倒退"):
            await repo.publish(first)
    asyncio.run(run())


def test_all_rest_endpoints_filters_errors_and_snapshot_conflict():
    db = WriteDatabase()
    snapshot = engine().process(envelope())
    asyncio.run(ShortSentimentRepository(db).publish(snapshot))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    theme_id = CATALOG.ids("000002")[0]
    with TestClient(app) as client:
        base = "/api/v1/sentiment"
        for path in ["/dashboard", "/ladder", f"/themes/{theme_id}", "/stocks/000001", "/events", "/timeseries"]:
            response = client.get(base + path)
            assert response.status_code == 200, response.text
            assert response.json()["meta"]["snapshot_id"] == snapshot["meta"]["snapshot_id"]
        assert client.get(base + "/ladder?group_by=market").json()["data"]["unique_count"] == 2
        assert client.get(base + "/ladder?min_streak=4").json()["data"]["unique_count"] == 0
        assert client.get(base + "/dashboard?snapshot_id=old").status_code == 409
        assert client.get(base + "/dashboard?scope=unsupported").status_code == 422
        assert client.get(base + "/stocks/bad").status_code == 422
        assert client.get(base + "/themes/missing").status_code == 404
        assert client.get(base + "/events?after=invalid").status_code == 422
        assert client.get(base + "/events?limit=1000").status_code == 422
        assert client.get(base + "/timeseries?start_date=2020-01-01").status_code == 422
        assert client.get(base + "/dashboard?trade_date=2000-01-01").status_code == 404
        assert "/api/v1/sentiment/stream" in client.get("/openapi.json").json()["paths"]


def test_sse_initial_resync_update_removal_and_heartbeat():
    async def run():
        db = WriteDatabase()
        worker = engine()
        first = worker.process(envelope())
        repo = ShortSentimentRepository(db)
        await repo.publish(first)
        service = SentimentQueryService(db, cache_ttl=0)
        class Request:
            async def is_disconnected(self):
                return False
        frames = stream_frames(Request(), service, first, "old", interval=0)
        assert "event: resync" in await anext(frames)
        assert "event: heartbeat" in await anext(frames)
        missing = worker.process(envelope([], at="10:00:05"), first)
        await repo.publish(missing)
        update = await anext(frames)
        assert "event: update" in update
        data = json.loads(update.split("data: ")[1])
        assert data["data"]["removed_codes"] == ["000001", "000002"]
        await frames.aclose()
    asyncio.run(run())


def test_sse_delivers_intermediate_events_between_display_refreshes():
    async def run():
        db = WriteDatabase()
        worker = engine()
        first = worker.process(envelope())
        repo = ShortSentimentRepository(db)
        await repo.publish(first)
        service = SentimentQueryService(db, cache_ttl=0)
        class Request:
            async def is_disconnected(self):
                return False
        frames = stream_frames(Request(), service, first, None, interval=0)
        await anext(frames)
        second = worker.process(envelope([quote(price=10.8, at="10:00:05"), quote("000002", at="10:00:05")], "10:00:05"), first)
        await repo.publish(second)
        third = worker.process(envelope(at="10:00:10"), second)
        await repo.publish(third)
        data = json.loads((await anext(frames)).split("data: ")[1])
        assert [e["type"] for e in data["data"]["events"]["items"]] == ["open", "reseal"]
        await frames.aclose()
    asyncio.run(run())


def test_worker_cursor_keeps_same_time_batches_across_sources(tmp_path):
    async def scenario():
        db = WriteDatabase()
        db["market_sentiment_raw_daily"].rows = [raw_day()]
        documents = []
        for identity, clock in [("a", "10:00:00"), ("b", "10:00:00"), ("c", "10:00:05")]:
            captured = envelope(at=clock)
            captured["batch_id"] = identity
            documents.append({"_id": identity, "trade_date": DAY,
                              "observed_at": datetime.fromisoformat(captured["observed_at"]),
                              **encode_batch(captured)})
        db[SNAPSHOT_COLLECTION].rows = [documents[2], documents[0]]
        db[AUCTION_COLLECTION].rows = [documents[1]]
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        for sequence, identity in enumerate(["a", "b", "c"], 1):
            assert (await worker.sync(trade_date=DAY, max_batches=1))["processed"] == 1
            stored = await worker.repo.latest(DAY)
            assert stored["sequence"] == sequence
            assert stored["source_cursor"]["batch_id"] == identity
        # The first idle pass publishes the historical day's pending-close
        # status; further passes must not churn IDs or move the source cursor.
        await worker.sync(trade_date=DAY, max_batches=1)
        assert (await worker.sync(trade_date=DAY, max_batches=1))["processed"] == 0
        assert (await worker.repo.checkpoint(DAY))["source_cursor"]["batch_id"] == "c"
    asyncio.run(scenario())


def test_worker_finalizes_existing_daily_sources_without_network(tmp_path):
    async def run():
        db = WriteDatabase()
        db["market_sentiment_raw_daily"].rows = [raw_day(), raw_day(DAY)]
        db["market_sentiment_daily"].rows = [{"scope": "hs_mainboard", "trade_date": DAY}]
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock", scope="hs_mainboard")
        result = await worker.sync(trade_date=DAY)
        assert result["status"] == "finalized"
        assert (await worker.repo.latest())["meta"]["is_final"] is True
        assert (await worker.sync(trade_date=DAY))["processed"] == 0
    asyncio.run(run())


def test_worker_rejects_incomplete_finalization_without_replacing_checkpoint(tmp_path):
    async def run():
        db = WriteDatabase()
        invalid_raw = raw_day(DAY)
        invalid_raw["market"]["stocks"][0]["raw_high"] = 10
        db["market_sentiment_raw_daily"].rows = [raw_day(), invalid_raw]
        db["market_sentiment_daily"].rows = [{"scope": "hs_mainboard", "trade_date": DAY}]
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock", scope="hs_mainboard")
        live = engine("hs_mainboard").process(envelope())
        await worker.repo.publish(live)
        status = await worker.sync(trade_date=DAY)
        assert status["status"] == "waiting_finalization"
        assert status["pending_reasons"] == ["final_coverage_below_threshold"]
        retained = await worker.repo.latest(DAY)
        assert retained["stocks"] == live["stocks"]
        assert retained["meta"]["finalization"]["pending_reasons"] == ["final_coverage_below_threshold"]
        assert retained["meta"]["is_final"] is False
        assert retained["market"]["values"]["coverage"] == 1
        assert not any(r["interval"] == "1d" for r in db[TIMESERIES].rows)
    asyncio.run(run())


def all_board_envelope():
    quotes = [quote("600001"), quote("000001"),
              quote("300001", price=12, limit_up_price=12, limit_down_price=8),
              quote("302132", price=11, limit_up_price=12, limit_down_price=8),
              quote("688001", price=12, limit_up_price=12, limit_down_price=8),
              quote("920001", price=13, limit_up_price=13, limit_down_price=7)]
    return envelope(quotes, expected=[q["code"] for q in quotes])


def test_all_boards_use_individual_limits_and_reject_non_a_shares():
    source = all_board_envelope()
    for code in ("510300", "159001", "200001", "900001", "830001"):
        source["quotes"].append(quote(code))
        source["expected_codes"].append(code)
    snapshot = SentimentEngine(CATALOG, [], []).process(source)
    assert len(snapshot["stocks"]) == 6
    assert snapshot["market"]["values"]["limit_up"] == 5
    assert snapshot["stocks"]["302132"]["board_type"] == "chinext"
    assert snapshot["stocks"]["302132"]["is_limit_up"] is False
    assert snapshot["stocks"]["920001"]["market"] == "BJ"
    assert snapshot["stocks"]["920001"]["limit_up_price"] == 13
    assert sum(b["values"]["limit_up"] for b in snapshot["boards"]) == 5
    service = SentimentQueryService(WriteDatabase())
    assert service.ladder(snapshot, board_type="chinext")["unique_count"] == 1
    assert service.ladder(snapshot, board_type="star")["unique_count"] == 1
    assert service.ladder(snapshot, board_type="bse")["unique_count"] == 1


def test_scope_storage_cache_events_and_api_are_isolated():
    async def prepare():
        db = WriteDatabase()
        snapshots = {}
        for scope in ("all_a", "hs_mainboard"):
            result = SentimentEngine(CATALOG, [], [], scope=scope).process(all_board_envelope())
            await ShortSentimentRepository(db, scope=scope).publish(result)
            snapshots[scope] = result
        assert len(db[SNAPSHOTS].rows) == 2
        assert len([r for r in db[TIMESERIES].rows if r["interval"] == "1m"]) == 2
        assert len([r for r in db[TIMESERIES].rows if r["interval"] == "5m"]) == 2
        assert len({r["_id"] for r in db[EVENTS].rows}) == len(db[EVENTS].rows)
        return db, snapshots
    db, snapshots = asyncio.run(prepare())
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    base = "/api/v1/sentiment"
    with TestClient(app) as client:
        for scope, count in [("all_a", 5), ("hs_mainboard", 2), ("all_a", 5)]:
            params = {"scope": scope}
            dashboard = client.get(base + "/dashboard", params=params).json()
            assert dashboard["meta"]["scope"] == scope
            assert dashboard["data"]["market"]["values"]["limit_up"] == count
            for stock_code in ("300001", "302132", "688001", "920001"):
                assert client.get(base + "/stocks/" + stock_code, params=params).status_code == (200 if scope == "all_a" else 404)
            page = client.get(base + "/events", params={**params, "limit": 1}).json()["data"]
            second = client.get(base + "/events", params={**params, "after": page["next_cursor"]})
            assert second.status_code == 200
            assert all(e["scope"] == scope for e in second.json()["data"]["items"])
        old_id = snapshots["hs_mainboard"]["meta"]["snapshot_id"]
        assert client.get(base + "/dashboard", params={"snapshot_id": old_id}).status_code == 409
        old_cursor = snapshots["hs_mainboard"]["events"][0]["event_id"]
        assert client.get(base + "/events", params={"after": old_cursor}).status_code == 422
        future = f"all_a:{DAY}:99999999:00000"
        assert client.get(base + "/events", params={"after": future}).status_code == 409
        assert client.get(base + "/ladder", params={"board_type": "bse"}).json()["data"]["unique_count"] == 1


def test_expanded_daily_raw_prices_rounding_and_ipo_exclusion():
    from app.services.short_sentiment.daily_source import expanded_daily_quotes
    sessions = ["2026-08-27", "2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", DAY]
    codes = ["sz.302132", "sh.688001", "sz.300002"]
    rows = [{"code": c, "date": DAY, "adjustflag": "3", "open": "10.03", "high": "12.04",
             "low": "10.03", "close": "12.04", "preclose": "10.03", "amount": "10000",
             "volume": "1000", "tradestatus": "1", "isST": "0"} for c in codes]
    universe = [{"code": c, "code_name": "测试"} for c in codes]
    basics = [{"code": c, "ipoDate": sessions[1] if c == codes[1] else sessions[0]} for c in codes]
    expected, quotes = expanded_daily_quotes(DAY, rows, universe, basics, sessions)
    assert len(expected) == 3
    assert quotes[0]["limit_up_price"] == 12.04
    assert quotes[0]["limit_down_price"] == 8.02
    assert quotes[1]["has_price_limit"] is False  # Fifth session, not sixth.
    result = SentimentEngine(CATALOG, [], []).process(envelope(quotes, at="15:00:00", expected=expected), final=True)
    assert result["market"]["values"]["limit_up"] == 2
    assert result["stocks"]["688001"]["excluded_reason"] == "no_price_limit"
    rows[0]["adjustflag"] = "2"
    with pytest.raises(ValueError, match="原始价"):
        expanded_daily_quotes(DAY, rows, universe, basics, sessions)


def test_bse_close_source_rejects_intraday_and_preserves_missing_members():
    from app.services.short_sentiment.daily_source import build_daily_source
    codes = ["sz.300001", "sh.688001"]
    rows = [{"code": c, "date": DAY, "adjustflag": "3", "open": "10", "high": "12",
             "low": "10", "close": "12", "preclose": "10", "amount": "10000", "volume": "1000",
             "tradestatus": "1", "isST": "0"} for c in codes]
    universe = [{"code": c, "code_name": "测试"} for c in codes]
    basics = [{"code": c, "ipoDate": "2020-01-01"} for c in codes]
    bse = quote("920001", price=13, at="15:04:00", limit_up_price=13, limit_down_price=7,
                high_price=13, low_price=10, volume=1000, phase="outside_session")
    batch = envelope([bse], at="15:04:05", expected=("920001", "920002"))
    batch["batch_id"] = "bse-close-fixture"
    raw = build_daily_source(raw_day(DAY), rows, universe, basics, batch, ["2026-09-01", DAY])
    assert raw["features"]["920001"]["is_limit_up"] is True
    assert raw["features"]["920002"]["is_limit_up"] is None
    assert "920002" in raw["envelope"]["expected_codes"]
    bse["market_data_time"] = f"{DAY}T14:59:59+08:00"
    with pytest.raises(ValueError, match="收盘报价"):
        build_daily_source(raw_day(DAY), rows, universe, basics, batch, ["2026-09-01", DAY])


def test_full_market_worker_refuses_a_missing_board_even_with_high_total_coverage(tmp_path):
    from app.services.short_sentiment.daily_source import DAILY_SOURCES, SOURCE_VERSION
    async def run():
        db = WriteDatabase()
        source = all_board_envelope()
        for i in range(100):
            code = f"600{i + 100:03d}"
            source["quotes"].append(quote(code))
            source["expected_codes"].append(code)
        source["quotes"] = [q for q in source["quotes"] if q["code"] != "920001"]
        source.update(batch_id="partial-close", input_hash="partial-close")
        db[DAILY_SOURCES].rows = [{"_id": DAY, "source_version": SOURCE_VERSION,
                                  "scope": "all_a", "trade_date": DAY, "envelope": source, "features": {}}]
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        status = await worker.sync(trade_date=DAY)
        assert status["pending_reasons"] == ["final_board_coverage_below_threshold"]
        assert not db[SNAPSHOTS].rows
    asyncio.run(run())


def test_finalized_legacy_scope_remains_readable_after_catalog_expansion(tmp_path):
    async def run():
        db = WriteDatabase()
        final = engine("hs_mainboard").process(daily_envelope(raw_day(DAY)), final=True)
        await ShortSentimentRepository(db, scope="hs_mainboard").publish(final)
        worker = SentimentWorker(db, scope="hs_mainboard", catalog=ThemeCatalog(groups={}), lock_path=tmp_path / "lock")
        assert (await worker.sync(trade_date=DAY))["status"] == "final"
        assert (await worker.repo.latest(DAY))["meta"]["snapshot_id"] == final["meta"]["snapshot_id"]
    asyncio.run(run())


def test_worker_consumes_pending_intraday_batches_before_daily_finalization(tmp_path):
    async def run():
        db = WriteDatabase()
        db["market_sentiment_raw_daily"].rows = [raw_day(), raw_day(DAY)]
        db["market_sentiment_daily"].rows = [{"scope": "hs_mainboard", "trade_date": DAY}]
        for identity, at, price in [("first", "10:00:00", 11), ("second", "10:00:05", 10.8)]:
            captured = envelope([quote(price=price, at=at), quote("000002", at=at)], at)
            captured["batch_id"] = identity
            db[SNAPSHOT_COLLECTION].rows.append({"_id": identity, "trade_date": DAY,
                "observed_at": datetime.fromisoformat(captured["observed_at"]), **encode_batch(captured)})
        worker = SentimentWorker(db, scope="hs_mainboard", catalog=CATALOG, lock_path=tmp_path / "lock")
        assert (await worker.sync(trade_date=DAY, max_batches=1, replay=True))["processed"] == 1
        assert (await worker.sync(trade_date=DAY, max_batches=1))["status"] == "updated"
        assert not (await worker.repo.latest(DAY))["meta"]["is_final"]
        assert (await worker.sync(trade_date=DAY))["status"] == "finalized"
        final = await worker.repo.latest(DAY)
        assert final["sequence"] == 3
        assert final["stocks"]["000001"]["observed_open_count"] == 1
    asyncio.run(run())


def test_cli_retries_busy_and_short_batch_until_no_pending_sources(monkeypatch):
    from types import SimpleNamespace
    from datetime import date
    import app.manually_execute_script.sync_short_sentiment as cli
    responses = iter([{"status": "busy", "processed": 0}, {"status": "updated", "processed": 1},
                      {"status": "waiting", "processed": 0}])
    calls = []
    class Worker:
        async def sync(self, **kwargs):
            calls.append(kwargs)
            return next(responses)
    class Client:
        closed = False
        def __getitem__(self, name):
            return WriteDatabase()
        def close(self):
            self.closed = True
    client = Client()
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(cli, "AsyncIOMotorClient", lambda *a: client)
    monkeypatch.setattr(cli, "SentimentWorker", lambda *a, **kw: Worker())
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(mongo_uri="unused", mongo_db_name="unused"))
    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)
    asyncio.run(cli.run(SimpleNamespace(scope="all_a", backfill_days=None, trade_date=date.fromisoformat(DAY), replay=True)))
    assert len(calls) == 3 and client.closed


def test_restart_restores_archived_intraday_catalog(tmp_path, monkeypatch):
    import app.services.short_sentiment.themes as themes_module
    import app.services.short_sentiment.service as service_module
    monkeypatch.setattr(themes_module, "ARCHIVE_ROOT", tmp_path / "catalogs")
    CATALOG.archive()
    class UpdatedCatalog(ThemeCatalog):
        def __init__(self, path=None):
            super().__init__(path=path, groups={} if path is None else None)
    monkeypatch.setattr(service_module, "ThemeCatalog", UpdatedCatalog)
    async def run():
        db = WriteDatabase()
        initial = engine("hs_mainboard").process(envelope())
        await ShortSentimentRepository(db, scope="hs_mainboard").publish(initial)
        captured = envelope(at="10:00:05")
        captured["batch_id"] = "new-batch"
        db[SNAPSHOT_COLLECTION].rows = [{"_id": "new-batch", "trade_date": DAY,
            "observed_at": datetime.fromisoformat(captured["observed_at"]), **encode_batch(captured)}]
        worker = SentimentWorker(db, scope="hs_mainboard", lock_path=tmp_path / "lock")
        assert worker.catalog.version != CATALOG.version
        await worker.sync(trade_date=DAY)
        result = await worker.repo.latest(DAY)
        assert result["meta"]["classification_version"] == CATALOG.version
        assert result["stocks"]["000001"]["theme_ids"] == CATALOG.ids("000001")
    asyncio.run(run())


def test_expanded_history_derives_streaks_without_bridging_missing_sessions():
    from app.services.short_sentiment.facts import daily_history
    sessions = ["2026-09-01", "2026-09-02", DAY]
    rows = [{"scope": "all_a", "trade_date": day,
             "envelope": {"expected_codes": ["300001"]},
             "features": {"300001": {"is_limit_up": index > 0, "is_touch_up": index > 0,
                                      "eligible": True, "streak": None if index > 0 else 0}}}
            for index, day in enumerate(sessions)]
    history, previous, expected = daily_history(rows, sessions)
    assert previous["300001"]["streak"] == 2
    assert expected == ["300001"]
    assert [r["is_limit_up"] for r in history["300001"]] == [False, True, True]
    history, previous, _ = daily_history([rows[0], rows[2]], sessions)
    assert history["300001"][1]["is_limit_up"] is None
    assert previous["300001"]["streak"] is None


def test_scheduler_scope_failure_does_not_block_other_scope(monkeypatch, caplog):
    import app.scheduler.sentiment_jobs as jobs
    calls = []
    class Worker:
        def __init__(self, scope):
            self.scope = scope
        async def sync(self):
            calls.append(self.scope)
            if self.scope == "all_a":
                raise ValueError("北交所收盘依据尚未补齐")
            return {"status": "updated", "processed": 1}
    monkeypatch.setattr(jobs, "_live_workers", {scope: Worker(scope) for scope in ("all_a", "hs_mainboard")})
    async def clock(): return datetime(2026, 9, 3, 10, tzinfo=CN), -300
    monkeypatch.setattr(jobs, "observation_now", clock)
    asyncio.run(jobs.update_short_sentiment_job())
    assert calls == ["all_a", "hs_mainboard"]
    assert "short_sentiment_failed scope=all_a" in caplog.text
