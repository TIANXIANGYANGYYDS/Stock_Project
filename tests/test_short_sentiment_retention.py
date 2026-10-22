import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_db
from app.api.routers.sentiment import stream_frames
from app.repositories.realtime_snapshot_repository import SNAPSHOT_COLLECTION, encode_batch
from app.repositories.short_sentiment_repository import RETAINED, SNAPSHOTS, TIMESERIES, ShortSentimentRepository
from app.services.short_sentiment.daily_source import DAILY_SOURCES, SOURCE_VERSION
from app.services.short_sentiment.engine import SentimentEngine
from app.services.short_sentiment.facts import CN
from app.services.short_sentiment.retention import public_snapshot, usable_observation
from app.services.short_sentiment.service import SentimentQueryService, SentimentWorker
from app.services.short_sentiment.themes import ThemeCatalog
from tests.test_short_sentiment import DAY, CATALOG, WriteDatabase, all_board_envelope, engine, envelope, quote, raw_day


def capture(db, identity, at, phase="continuous", *, quotes=None):
    source = all_board_envelope()
    source["observed_at"] = f"{DAY}T{at}+08:00"
    source["clock_offset_seconds"] = -304
    source["batch_id"] = identity
    for item in source["quotes"]:
        item.update(market_data_time=source["observed_at"], phase=phase)
    if quotes is not None:
        source["quotes"] = quotes
    db[SNAPSHOT_COLLECTION].rows.append({"_id": identity, "trade_date": DAY,
        "observed_at": datetime.fromisoformat(source["observed_at"]), **encode_batch(source)})
    return source


@pytest.mark.parametrize("scope", ["all_a", "hs_mainboard"])
def test_close_retention_is_durable_and_does_not_flatten_real_minutes(tmp_path, scope):
    async def run():
        db = WriteDatabase()
        worker = SentimentWorker(db, scope=scope, catalog=CATALOG, lock_path=tmp_path / scope)
        capture(db, "a", "14:56:03")
        await worker.sync(trade_date=DAY)
        good = await worker.repo.latest(DAY)
        assert usable_observation(good)
        capture(db, "b", "14:58:03", "closing_auction")
        await worker.sync(trade_date=DAY)
        auction = await worker.repo.latest(DAY)
        assert auction["meta"]["session"] == "closing_auction"
        assert auction["meta"]["is_retained"] is True
        assert auction["stocks"] == good["stocks"]
        # A new worker and a fresh reader get the same complete snapshot.
        restarted = SentimentWorker(db, scope=scope, catalog=CATALOG, lock_path=tmp_path / scope)
        capture(db, "c", "15:04:58", "outside_session")
        await restarted.sync(trade_date=DAY)
        visible = await SentimentQueryService(db, scope=scope).snapshot(DAY)
        raw = await restarted.repo.checkpoint(DAY)
        assert raw["market"]["values"]["coverage"] == 0
        assert visible["stocks"] == good["stocks"]
        assert visible["market"] == good["market"]
        assert visible["meta"]["as_of"] == good["meta"]["as_of"]
        assert visible["meta"]["processed_as_of"] == f"{DAY}T15:04:58+08:00"
        assert visible["meta"]["retained_from_snapshot_id"] == good["meta"]["snapshot_id"]
        assert visible["meta"]["snapshot_id"] != good["meta"]["snapshot_id"]
        assert visible["meta"]["finalization"] == {"status": "pending", "pending_reasons": ["daily_summary_missing", "daily_raw_missing"]}
        assert not visible["meta"]["is_final"]
        assert raw["source_cursor"]["batch_id"] == "c"
        minutes = db[TIMESERIES].rows
        assert len(minutes) == 3
        assert [p["market"]["values"]["coverage"] for p in minutes] == [1, 0, 0]
        assert len(db[RETAINED].rows) == 1
        identity = visible["meta"]["snapshot_id"]
        await restarted.sync(trade_date=DAY)
        await restarted.sync(trade_date=DAY)
        assert (await restarted.repo.latest(DAY))["meta"]["snapshot_id"] == identity
        return db, identity
    db, identity = asyncio.run(run())
    theme_id = CATALOG.ids("000001")[0]
    for _ in range(2):  # First visit and browser refresh use new API instances.
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            params = {"scope": scope, "trade_date": DAY, "snapshot_id": identity}
            for path in ["dashboard", "themes/" + theme_id, "stocks/000001", "ladder", "timeseries"]:
                response = client.get("/api/v1/sentiment/" + path, params=params)
                assert response.status_code == 200, response.text
                assert response.json()["meta"]["snapshot_id"] == identity
            members = client.get("/api/v1/sentiment/themes/" + theme_id, params=params).json()["data"]["members"]
            assert members["items"][0]["price"] is not None


def test_retention_preserves_real_missing_and_excluded_stocks():
    source = envelope(expected=[])
    source["quotes"] = [quote(f"600{i:03d}") for i in range(100, 220)]
    source["quotes"] += [quote("000003", is_st=True), quote("000004", is_suspended=True)]
    source["expected_codes"] = [q["code"] for q in source["quotes"]] + ["000005"]
    worker = SentimentEngine(CATALOG, [], [])
    good = worker.process(source)
    assert usable_observation(good)
    broken = envelope([], "15:04:00", source["expected_codes"])
    broken["session"] = "closed_pending_final"
    observation = worker.process(broken, good)
    visible = public_snapshot(observation, good)
    assert visible["stocks"] == good["stocks"]
    assert visible["stocks"]["000003"]["excluded_reason"] == "st"
    assert visible["stocks"]["000004"]["excluded_reason"] == "suspended"
    assert visible["stocks"]["000005"]["price"] is None
    assert all(not visible["stocks"][c]["eligible"] for c in ["000003", "000004", "000005"])
    assert visible["market"]["values"]["missing"] == 1


def test_continuous_trading_degradation_is_not_hidden_by_retention():
    worker = engine()
    good = worker.process(envelope())
    failed = worker.process(envelope([], "10:00:05"), good)
    visible = public_snapshot(failed, good)
    assert not visible["meta"]["is_retained"]
    assert visible["market"]["values"]["coverage"] == 0
    assert visible["stocks"]["000001"]["missing_reasons"]["quote"] == "missing_quote"


@pytest.mark.parametrize("changed", ["scope", "trade_date", "classification_version"])
def test_retention_never_crosses_scope_day_or_catalog(changed):
    worker = engine()
    good = worker.process(envelope())
    bad_envelope = envelope([], "15:04:00")
    bad_envelope["session"] = "closed_pending_final"
    failed = worker.process(bad_envelope, good)
    incompatible = deepcopy(good)
    incompatible["meta"][changed] = "different"
    visible = public_snapshot(failed, incompatible)
    assert not visible["meta"]["is_retained"]
    assert visible["meta"]["retention_reason"] == "no_same_day_usable_observation"
    assert visible["market"]["values"]["coverage"] == 0


@pytest.mark.parametrize("scope", ["all_a", "hs_mainboard"])
def test_pending_close_automatically_finalizes_and_notifies_sse(tmp_path, scope):
    async def run():
        db = WriteDatabase()
        worker = SentimentWorker(db, scope=scope, catalog=CATALOG, lock_path=tmp_path / scope)
        capture(db, "a", "14:56:03")
        await worker.sync(trade_date=DAY)
        capture(db, "b", "15:04:00", "outside_session")
        await worker.sync(trade_date=DAY)
        pending = await worker.repo.latest(DAY)
        class Request:
            async def is_disconnected(self):
                return False
        service = SentimentQueryService(db, scope=scope, cache_ttl=0)
        frames = stream_frames(Request(), service, pending, None, interval=0)
        first = json.loads((await anext(frames)).split("data: ")[1])
        assert first["meta"]["is_retained"]
        if scope == "hs_mainboard":
            db["market_sentiment_daily"].rows = [{"scope": scope, "trade_date": DAY}]
            db["market_sentiment_raw_daily"].rows = [raw_day(DAY, codes=("600001", "000001"))]
        else:
            final = all_board_envelope()
            final.update(observed_at=f"{DAY}T15:00:00+08:00", batch_id="daily-fixture", input_hash="daily-fixture")
            for q in final["quotes"]:
                q.update(market_data_time=final["observed_at"], phase="session_close")
            db[DAILY_SOURCES].rows = [{"_id": DAY, "source_version": SOURCE_VERSION, "trade_date": DAY,
                                      "scope": scope, "envelope": final, "features": {}}]
        assert (await worker.sync(trade_date=DAY))["status"] == "finalized"
        complete = await worker.repo.latest(DAY)
        assert complete["meta"]["is_final"] and not complete["meta"]["is_retained"]
        assert complete["meta"]["finalization"] == {"status": "finalized", "pending_reasons": []}
        assert complete["meta"]["as_of"] == f"{DAY}T15:00:00+08:00"
        update = await anext(frames)
        assert "event: update" in update
        assert json.loads(update.split("data: ")[1])["meta"]["is_final"] is True
        assert len([r for r in db[TIMESERIES].rows if r["interval"] == "1d"]) == 1
        assert [r["market"]["values"]["coverage"] for r in db[TIMESERIES].rows if r["interval"] == "1m"] == [1, 0]
        await frames.aclose()
        return db, complete["meta"]["snapshot_id"], pending["meta"]["snapshot_id"]
    db, identity, pending_id = asyncio.run(run())
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        params = {"scope": scope, "trade_date": DAY, "snapshot_id": identity}
        for path in ("dashboard", "themes/" + CATALOG.ids("000001")[0], "stocks/000001"):
            response = client.get("/api/v1/sentiment/" + path, params=params)
            assert response.status_code == 200, response.text
            meta = response.json()["meta"]
            assert meta["is_final"] and not meta["is_retained"]
            assert meta["snapshot_id"] == identity and meta["session"] == "closed"
        daily = client.get("/api/v1/sentiment/timeseries", params={**params, "interval": "1d"}).json()
        assert daily["data"]["count"] == 1
        assert daily["data"]["items"][0]["is_final"]
        assert client.get("/api/v1/sentiment/dashboard", params={**params, "snapshot_id": pending_id}).status_code == 409


def test_fresh_session_close_can_replace_retained_observation_without_becoming_final(tmp_path):
    async def run():
        db = WriteDatabase()
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        capture(db, "a", "14:56:03")
        await worker.sync(trade_date=DAY)
        capture(db, "b", "14:58:03", "closing_auction")
        await worker.sync(trade_date=DAY)
        capture(db, "c", "15:00:00", "session_close")
        await worker.sync(trade_date=DAY)
        snapshot = await worker.repo.latest(DAY)
        assert snapshot["meta"]["as_of"].endswith("15:00:00+08:00")
        assert not snapshot["meta"]["is_final"] and not snapshot["meta"]["is_retained"]
        assert snapshot["meta"]["coverage"] == 1
    asyncio.run(run())


def test_metadata_transition_uses_calibrated_clock_without_retimestamping_quotes(tmp_path, monkeypatch):
    import app.services.short_sentiment.service as service_module
    current = datetime.fromisoformat(f"{DAY}T14:56:30+08:00")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current + timedelta(seconds=304)
    monkeypatch.setattr(service_module, "datetime", Clock)
    async def run():
        nonlocal current
        db = WriteDatabase()
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        capture(db, "a", "14:56:03")
        await worker.sync(trade_date=DAY)
        assert (await worker.sync(trade_date=DAY))["processed"] == 0
        current = current.replace(hour=15, minute=10)
        await worker.sync(trade_date=DAY)
        visible = await worker.repo.latest(DAY)
        assert visible["meta"]["session"] == "closed_pending_final"
        assert visible["meta"]["retention_reason"] == "session_ended_without_new_observation"
        assert visible["meta"]["as_of"] == f"{DAY}T14:56:03+08:00"
        meta = SentimentQueryService.meta(visible, now=current + timedelta(seconds=304))
        assert meta["age_seconds"] == 14 * 60 + 27
        assert meta["is_stale"] is True
        assert len(db[TIMESERIES].rows) == 1
    asyncio.run(run())


def test_full_member_ranking_includes_untouched_stocks_and_nulls_last():
    codes = [f"600{i:03d}" for i in range(100, 110)]
    catalog = ThemeCatalog(groups={"测试题材": {"成分股": [{"股票代码": c, "股票名称": c} for c in codes]}})
    snapshot = SentimentEngine(catalog, [], []).process(envelope([quote(c, price=10.5) for c in codes], expected=codes))
    facts = [(2, 4, -.1), (3, 3, -.2), (0, None, .05), (0, None, .03),
             (0, None, .02), (0, None, .01), (0, None, 0), (0, None, None),
             (None, 10, .5), (0, None, -.01)]
    for code, (streak, days, change) in zip(codes, facts):
        snapshot["stocks"][code].update(intraday_streak=streak, change_ratio=change, target_streak=999)
        snapshot["stocks"][code]["board_run"]["limit_up_days"] = days
    db = WriteDatabase()
    asyncio.run(ShortSentimentRepository(db).publish(snapshot))
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        url = "/api/v1/sentiment/themes/" + catalog.ids(codes[0])[0]
        first = client.get(url, params={"page_size": 7}).json()["data"]["members"]
        full = client.get(url, params={"page_size": 200}).json()["data"]["members"]
        assert first["total"] == 10
        assert first["items"] == full["items"][:7]
        assert [s["code"] for s in first["items"]] == [codes[1], codes[0], *codes[2:7]]
        assert any(not s["touched_up"] for s in first["items"])
        assert full["items"][-1]["intraday_streak"] is None
        assert full["items"][-2]["change_ratio"] is None
        assert first["sort"] == "board_strength"
        by_change = client.get(url, params={"sort": "change_ratio"}).json()["data"]["members"]
        assert by_change["items"][0]["code"] == codes[8]
        assert client.get(url, params={"sort": "target_streak"}).status_code == 422


def test_lunch_resume_uses_real_checkpoint_and_does_not_fabricate_open_event(tmp_path):
    async def run():
        db = WriteDatabase()
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        capture(db, "a", "11:29:58")
        await worker.sync(trade_date=DAY)
        capture(db, "b", "11:35:00", "outside_session")
        await worker.sync(trade_date=DAY)
        assert (await worker.repo.latest(DAY))["meta"]["is_retained"]
        source = capture(db, "c", "13:00:03")
        for q in source["quotes"]:
            q["price"] = q["previous_close"]
        db[SNAPSHOT_COLLECTION].rows[-1].update(encode_batch(source))
        resumed = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        await resumed.sync(trade_date=DAY)
        snapshot = await resumed.repo.latest(DAY)
        assert snapshot["meta"]["session"] == "continuous"
        assert not snapshot["meta"]["is_retained"]
        assert snapshot["stocks"]["000001"]["change_ratio"] == 0
        assert not any(e["type"] in {"open", "reseal"} for e in snapshot["events"])
    asyncio.run(run())


def test_next_day_missing_observation_does_not_reuse_previous_day_or_scope(tmp_path):
    async def run():
        db = WriteDatabase()
        capture(db, "a", "14:56:03")
        workers = [SentimentWorker(db, scope=s, catalog=CATALOG, lock_path=tmp_path / s)
                   for s in ("all_a", "hs_mainboard")]
        for worker in workers:
            await worker.sync(trade_date=DAY)
        capture(db, "b", "15:04:00", "outside_session")
        for worker in workers:
            await worker.sync(trade_date=DAY)
        historical = [await w.repo.latest(DAY) for w in workers]
        next_day = "2026-09-04"
        source = all_board_envelope()
        source.update(observed_at=f"{next_day}T15:04:00+08:00", quotes=[], batch_id="next")
        db[SNAPSHOT_COLLECTION].rows.append({"_id": "next", "trade_date": next_day,
            "observed_at": datetime.fromisoformat(source["observed_at"]), **encode_batch(source)})
        class Request:
            async def is_disconnected(self):
                return False
        for worker, old in zip(workers, historical):
            frames = stream_frames(Request(), SentimentQueryService(db, scope=worker.scope, cache_ttl=0),
                                   old, None, interval=0)
            await anext(frames)
            await worker.sync(trade_date=next_day)
            latest = await SentimentQueryService(db, scope=worker.scope).snapshot()
            assert latest["meta"]["trade_date"] == next_day
            assert not latest["meta"]["is_retained"]
            assert latest["meta"]["retention_reason"] == "no_same_day_usable_observation"
            assert all(s["price"] is None for s in latest["stocks"].values())
            assert await worker.repo.latest(DAY) == old
            frame = await anext(frames)
            assert "event: snapshot" in frame
            data = json.loads(frame.split("data: ")[1])
            assert data["meta"]["trade_date"] == next_day
            assert data["data"]["replace_all"] and not data["data"]["stocks_upsert"]
            await frames.aclose()
        assert {r["scope"] for r in db[RETAINED].rows} == {"all_a", "hs_mainboard"}
        assert all(r["trade_date"] == DAY for r in db[RETAINED].rows)
    asyncio.run(run())


def test_failed_retained_publication_leaves_public_and_processing_checkpoint_consistent(tmp_path, monkeypatch):
    async def run():
        db = WriteDatabase()
        worker = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        capture(db, "a", "14:56:03")
        await worker.sync(trade_date=DAY)
        original = await worker.repo.latest(DAY)
        original_update = db[SNAPSHOTS].update_one
        async def fail_commit(*args, **kwargs):
            raise OSError("simulated database write failure")
        monkeypatch.setattr(db[SNAPSHOTS], "update_one", fail_commit)
        capture(db, "b", "15:04:00", "outside_session")
        with pytest.raises(OSError):
            await worker.sync(trade_date=DAY)
        assert await worker.repo.latest(DAY) == original
        assert await worker.repo.checkpoint(DAY) == original
        assert len(await worker.repo.timeseries(DAY, DAY, "1m")) == 1
        monkeypatch.setattr(db[SNAPSHOTS], "update_one", original_update)
        restarted = SentimentWorker(db, catalog=CATALOG, lock_path=tmp_path / "lock")
        await restarted.sync(trade_date=DAY)
        public, raw = await restarted.repo.latest(DAY), await restarted.repo.checkpoint(DAY)
        assert public["meta"]["snapshot_id"] == raw["meta"]["snapshot_id"]
        assert public["meta"]["coverage"] == 1 and raw["meta"]["coverage"] == 0
        assert public["stocks"] == original["stocks"]
        assert len(await worker.repo.timeseries(DAY, DAY, "1m")) == 2
    asyncio.run(run())
