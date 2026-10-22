from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import fcntl
import logging
from pathlib import Path
import time

from app.core.config import PROJECT_ROOT
from app.repositories.realtime_snapshot_repository import AUCTION_COLLECTION, SNAPSHOT_COLLECTION, decode_batch
from app.repositories.short_sentiment_repository import SNAPSHOTS, ShortSentimentRepository
from app.services.short_sentiment.engine import SentimentEngine, daily_envelope, public_stats
from app.services.short_sentiment.facts import CN, STALE_SECONDS, instant
from app.services.short_sentiment.metrics import MIN_COVERAGE
from app.services.short_sentiment.themes import ThemeCatalog
from app.services.short_sentiment.popularity import attach_popularity
from app.services.short_sentiment.universe import DEFAULT_SCOPE, in_scope, snapshot_identity
from app.services.short_sentiment.daily_source import AllADailySource
from app.services.short_sentiment.retention import public_snapshot, session_at, usable_observation
from app.services.trading_calendar_service import get_a_share_calendar

logger = logging.getLogger(__name__)


def preceding_sessions(day, count=20):
    calendar = get_a_share_calendar()
    sessions = calendar.sessions_in_range(date.fromisoformat(day) - timedelta(days=80), day)
    if not len(sessions) or sessions[-1].strftime("%Y-%m-%d") != day:
        raise ValueError("非交易日或交易日历未覆盖")
    return [s.strftime("%Y-%m-%d") for s in sessions[:-1]][-count:]


class SentimentWorker:
    """Only the scheduler/CLI writes. Readers never capture or recompute quotes."""
    def __init__(self, db, *, catalog=None, lock_path: Path | None = None, scope=DEFAULT_SCOPE):
        self.db = db
        self.scope = scope
        self.repo = ShortSentimentRepository(db, scope=scope)
        self._auto_catalog = catalog is None
        self.catalog = catalog or ThemeCatalog()
        if self._auto_catalog:
            self.catalog.archive()
        lock_name = "worker.lock" if scope == "hs_mainboard" else "all_a.worker.lock"
        self.lock_path = lock_path or PROJECT_ROOT / ".local/short_sentiment" / lock_name
        self._engine = None
        self._engine_day = None
        self._prior_day_snapshot = None
        self._indexed = False
        self.daily_source = AllADailySource(db) if scope == "all_a" else None

    async def sync(self, *, trade_date=None, max_batches=12, replay=False):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "processed": 0}
            try:
                return await self._sync(trade_date, max_batches, replay)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def finalization_input(self, day):
        """Read existing dependencies only; collection is owned by the daily job."""
        done, raw = await asyncio.gather(
            self.db["market_sentiment_daily"].find_one({"scope": "hs_mainboard", "trade_date": day}, {"trade_date": 1}),
            self.db["market_sentiment_raw_daily"].find_one({"scope": "hs_mainboard", "trade_date": day}))
        if self.daily_source:
            # A previously validated combined source is independently durable.
            from app.services.short_sentiment.daily_source import DAILY_SOURCES, SOURCE_VERSION
            stored = await self.db[DAILY_SOURCES].find_one({"_id": day, "source_version": SOURCE_VERSION})
            if stored:
                return stored, []
        missing = ([] if done else ["daily_summary_missing"]) + ([] if raw else ["daily_raw_missing"])
        if missing:
            return None, missing
        if not self.daily_source:
            return raw, []
        try:
            source = await self.daily_source.get(day)
        except (ValueError, FileNotFoundError) as exc:
            logger.warning("short_sentiment_daily_source_invalid scope=%s day=%s error=%s", self.scope, day, exc)
            return None, ["daily_source_invalid"]
        if source:
            return source, []
        if not raw.get("provenance", {}).get("path"):
            return None, ["daily_archive_missing"]
        quote = await self.db[SNAPSHOT_COLLECTION].find_one({"trade_date": day}, {"observed_at": 1}, sort=[("observed_at", -1)])
        if quote is None or instant(quote["observed_at"]).astimezone(CN).strftime("%H:%M:%S") < "15:00:00":
            return None, ["bse_close_archive_missing"]
        return None, ["daily_archive_missing"]

    @staticmethod
    def finalization_state(session, reasons):
        return {"status": "pending" if session == "closed_pending_final" else "not_due",
                "pending_reasons": reasons if session == "closed_pending_final" else []}

    async def _sync(self, day, max_batches, replay):
        if not self._indexed:
            await self.repo.ensure_indexes()
            for collection in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION):
                await self.db[collection].create_index(
                    [("trade_date", 1), ("observed_at", 1), ("_id", 1)],
                    name="idx_short_sentiment_source_cursor")
            self._indexed = True
        if day is None:
            rows = await asyncio.gather(*[self.db[name].find_one({}, {"trade_date": 1}, sort=[("trade_date", -1), ("observed_at", -1)]) for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)],
                                       self.db["market_sentiment_daily"].find_one({"scope": "hs_mainboard"}, {"trade_date": 1}, sort=[("trade_date", -1)]))
            dates = [r["trade_date"] for r in rows if r]
            if not dates:
                return {"status": "no_sources", "processed": 0}
            day = max(dates)
        checkpoint = await self.repo.checkpoint(day)
        if checkpoint and checkpoint["meta"]["is_final"]:
            return {"status": "final", "processed": 0, "trade_date": day}
        if self._auto_catalog and self._engine_day != day:
            self.catalog = ThemeCatalog()
            self.catalog.archive()
        if checkpoint and checkpoint["meta"]["classification_version"] != self.catalog.version:
            if not self._auto_catalog:
                raise ValueError("当天题材库已变更，请下一交易日启用，不能静默改写当日分类")
            self.catalog = ThemeCatalog.restore(checkpoint["meta"]["classification_version"])
        if self._engine_day != day:
            sessions = preceding_sessions(day)
            if self.daily_source:
                raw_days = [raw for session in sessions if (raw := await self.daily_source.get(session)) is not None]
                missing = [s for s in sessions if not any(r["trade_date"] == s for r in raw_days)]
                raw_days += await self.db["market_sentiment_raw_daily"].find({"scope": "hs_mainboard", "trade_date": {"$in": missing}}).to_list(length=20)
            else:
                raw_days = await self.db["market_sentiment_raw_daily"].find({"scope": self.scope, "trade_date": {"$in": sessions}}).to_list(length=20)
            self._engine = await asyncio.to_thread(SentimentEngine, self.catalog, raw_days, sessions, scope=self.scope)
            self._engine_day = day
            self._prior_day_snapshot = await self.repo.checkpoint(sessions[-1]) if sessions else None
        query = {"trade_date": day}
        cursor = (checkpoint or {}).get("source_cursor")
        if cursor:
            # Keep a direct time bound so Mongo can seek into the compound
            # index instead of fetching every earlier batch to evaluate $or.
            query["observed_at"] = {"$gte": instant(cursor["observed_at"])}
            query["$or"] = [{"observed_at": {"$gt": instant(cursor["observed_at"])}},
                            {"observed_at": instant(cursor["observed_at"]), "_id": {"$gt": cursor["batch_id"]}}]
        raw, reasons = (None, ["replay_in_progress"]) if replay else await self.finalization_input(day)
        pending = False
        if raw and cursor:
            pending = any(await asyncio.gather(*[
                self.db[name].find_one(query, {"_id": 1}) for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)]))
        if raw and not pending:
            envelope = daily_envelope(raw, checkpoint)
            for quote in envelope["quotes"]:
                if quote["name"] == quote["code"]:
                    quote["name"] = self.catalog.names.get(quote["code"], quote["code"])
            result = await asyncio.to_thread(self._engine.process, envelope, checkpoint or self._prior_day_snapshot, final=True)
            coverage = result["market"]["values"]["coverage"]
            if coverage is None or coverage < MIN_COVERAGE:
                reasons = ["final_coverage_below_threshold"]
            elif self.scope == "all_a" and any(b["values"]["coverage"] is None or b["values"]["coverage"] < MIN_COVERAGE for b in result["boards"]):
                reasons = ["final_board_coverage_below_threshold"]
            else:
                await attach_popularity(self.db, result)
                await self.repo.publish(result)
                return {"status": "finalized", "processed": 1, "trade_date": day}
        elif pending:
            reasons = ["archive_backlog"]
        batches = await asyncio.gather(*[
            self.db[name].find(query).sort([("observed_at", 1), ("_id", 1)]).limit(max_batches).to_list(length=max_batches)
            for name in (SNAPSHOT_COLLECTION, AUCTION_COLLECTION)])
        ordered = sorted(batches[0] + batches[1], key=lambda r: (instant(r["observed_at"]), r["_id"]))[:max_batches]
        retained = await self.repo.retained(day)
        if usable_observation(checkpoint) and (not retained or checkpoint["meta"]["as_of"] > retained["meta"]["as_of"]):
            # Also closes a crash window between main publication and retention.
            await self.repo.save_retained(checkpoint)
            retained = checkpoint
        for document in ordered:
            envelope = await asyncio.to_thread(decode_batch, document)
            envelope["source_cursor"] = {"observed_at": instant(document["observed_at"]).isoformat(), "batch_id": document["_id"]}
            envelope["session"] = session_at(envelope["observed_at"])
            checkpoint = await asyncio.to_thread(self._engine.process, envelope, checkpoint or self._prior_day_snapshot)
            await attach_popularity(self.db, checkpoint)
            checkpoint["meta"]["finalization"] = self.finalization_state(envelope["session"], reasons)
            visible = public_snapshot(checkpoint, retained)
            await self.repo.publish(visible, observation=checkpoint)
            if usable_observation(checkpoint):
                retained = checkpoint
        if not ordered and checkpoint:
            # Session/status changes must also be publishable when capture stops.
            session = checkpoint["meta"]["session"]
            if not replay:
                now = datetime.now(CN) + timedelta(seconds=checkpoint["meta"].get("clock_offset_seconds", 0))
                if day < now.date().isoformat():
                    session = "closed_pending_final"
                elif day == now.date().isoformat():
                    session = session_at(now)
            checkpoint.setdefault("observation_id", checkpoint["meta"]["snapshot_id"])
            checkpoint["meta"].setdefault("is_retained", False)
            checkpoint["meta"].setdefault("processed_as_of", checkpoint["meta"]["as_of"])
            checkpoint["meta"]["finalization"] = self.finalization_state(session, reasons)
            visible = public_snapshot(checkpoint, retained, session=session)
            published = await self.repo.latest(day)
            keys = ("session", "as_of", "is_retained", "retention_reason", "retained_from_snapshot_id", "finalization")
            changed = any(visible["meta"].get(k) != published["meta"].get(k) for k in keys)
            if changed:
                checkpoint["sequence"] += 1
                checkpoint["meta"]["snapshot_id"] = snapshot_identity(day, checkpoint["sequence"], self.scope)
                checkpoint["meta"]["generated_at"] = datetime.now(CN).isoformat()
                checkpoint["input_id"] = "status:" + checkpoint["meta"]["snapshot_id"]
                checkpoint["events"] = []
                checkpoint["environment_updated"] = False
                visible = public_snapshot(checkpoint, retained, session=session)
                await self.repo.publish(visible, observation=checkpoint, write_timeseries=False)
            return {"status": "waiting_finalization" if session == "closed_pending_final" and not replay else "waiting",
                    "processed": int(changed), "trade_date": day, "pending_reasons": reasons}
        if not ordered and reasons and not replay:
            return {"status": "waiting_finalization", "processed": 0, "trade_date": day, "pending_reasons": reasons}
        return {"status": "updated" if ordered else "waiting", "processed": len(ordered), "trade_date": day}


class SentimentQueryService:
    def __init__(self, db, *, cache_ttl=1.0, scope=DEFAULT_SCOPE):
        self.scope = scope
        self.repo = ShortSentimentRepository(db, scope=scope)
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._read_lock = asyncio.Lock()

    async def snapshot(self, trade_date=None, snapshot_id=None):
        async with self._read_lock:
            now = time.monotonic()
            cached = self._cache.get(trade_date)
            if cached and now - cached[0] < self.cache_ttl:
                snapshot = cached[1]
            else:
                query = {"scope": self.scope}
                if trade_date:
                    query["trade_date"] = trade_date
                header = await self.repo.db[SNAPSHOTS].find_one(query, {"snapshot_id": 1}, sort=[("trade_date", -1)])
                if cached and header and header["snapshot_id"] == cached[1]["meta"]["snapshot_id"]:
                    snapshot = cached[1]
                else:
                    snapshot = await self.repo.latest(trade_date) if header else None
                if snapshot:
                    if len(self._cache) >= 8:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[trade_date] = (now, snapshot)
        if snapshot is None:
            raise LookupError("暂无情绪快照，等待短线情绪调度任务处理行情")
        if snapshot_id and snapshot["meta"]["snapshot_id"] != snapshot_id:
            raise SnapshotConflict("快照已更新，请重新获取总览")
        return snapshot

    @staticmethod
    def meta(snapshot, *, now=None):
        result = deepcopy(snapshot["meta"])
        # Capture timestamps already use the collector's calibrated clock.
        # Compare on that same clock, while still aging frozen observations.
        now = (now or datetime.now(CN)) + timedelta(seconds=result.get("clock_offset_seconds", 0.0))
        as_of = instant(result["as_of"])
        age = max(0, (now - as_of).total_seconds())
        clock = now.astimezone(CN).strftime("%H:%M:%S")
        active = "09:15:00" <= clock <= "11:30:00" or "13:00:00" <= clock <= "15:00:00"
        result["age_seconds"] = age
        result.setdefault("is_retained", False)
        result.setdefault("retention_reason", None)
        result.setdefault("retained_from_snapshot_id", None)
        result.setdefault("processed_as_of", result["as_of"])
        result.setdefault("finalization", {"status": "finalized" if result["is_final"] else "not_due", "pending_reasons": []})
        expected_close = as_of.replace(hour=11, minute=30, second=0) if "11:30:00" < clock < "13:00:00" else as_of.replace(hour=15, minute=0, second=0)
        missed_session_end = not active and clock > "11:30:00" and (expected_close - as_of).total_seconds() > STALE_SECONDS
        result["is_stale"] = not result["is_final"] and age > STALE_SECONDS and (active or missed_session_end or result["trade_date"] < now.astimezone(CN).date().isoformat())
        return result

    def dashboard(self, snapshot):
        themes = [public_stats(t) for t in snapshot["themes"]]
        for theme in themes:
            theme["top_stocks"] = self.theme_members(snapshot, theme["theme_id"], page_size=5)["items"]
        themes.sort(key=lambda t: (-(t["values"].get("limit_up") or 0), -(t["values"].get("limit_up_ratio") or 0), t["theme_id"]))
        return {"market": public_stats(snapshot["market"]), "themes": themes,
                "boards": [public_stats(b) for b in snapshot.get("boards", [])],
                "recent_events": snapshot["events"][-20:], "unique_stock_count": len(snapshot["stocks"])}

    @staticmethod
    def theme(snapshot, theme_id):
        theme = next((t for t in snapshot["themes"] if t["theme_id"] == theme_id), None)
        if theme is None:
            raise LookupError("题材不存在")
        return public_stats(theme, include_codes=True)

    @staticmethod
    def theme_members(snapshot, theme_id, *, page=1, page_size=50, sort="board_strength"):
        members = [s for s in snapshot["stocks"].values() if theme_id in s["theme_ids"]]
        def descending(value):
            return (value is None, -value if value is not None else 0)
        def key(stock):
            ranking = () if sort == "change_ratio" else (
                *descending(stock["intraday_streak"]),
                *descending(stock["board_run"]["limit_up_days"]),
                (stock.get("popularity") or {}).get("rank") is None,
                (stock.get("popularity") or {}).get("rank") or 0)
            return (*ranking, *descending(stock["change_ratio"]), stock["code"])
        members.sort(key=key)
        return {"total": len(members), "page": page, "page_size": page_size, "sort": sort,
                "sort_fields": (["intraday_streak:desc", "board_run.limit_up_days:desc", "popularity.rank:asc:nulls_last"] if sort == "board_strength" else [])
                               + ["change_ratio:desc", "code:asc"],
                "items": members[(page - 1) * page_size:page * page_size]}

    def ladder(self, snapshot, *, theme_id=None, status="limit_up", min_streak=None, board_type=None, group_by="theme"):
        if theme_id:
            self.theme(snapshot, theme_id)
        selected = []
        for stock in snapshot["stocks"].values():
            if not stock["eligible"]:
                continue
            if theme_id and theme_id not in stock["theme_ids"] or board_type and stock["board_type"] != board_type:
                continue
            if status == "limit_up" and stock["is_limit_up"] is not True or status == "opened" and stock["limit_status"] != "opened" or status == "touched" and not stock["touched_up"]:
                continue
            height = stock["intraday_streak"] if stock["is_limit_up"] else stock["target_streak"]
            if min_streak is not None and (height is None or height < min_streak):
                continue
            selected.append((height, stock))
        groups = {}
        names = {t["theme_id"]: t["name"] for t in snapshot["themes"]}
        for height, stock in selected:
            keys = [theme_id] if theme_id else stock["theme_ids"] if group_by == "theme" else [snapshot["meta"]["scope"]]
            for key in keys:
                levels = groups.setdefault(key, {})
                levels.setdefault(height, []).append(stock)
        result = []
        for key, levels in groups.items():
            items = []
            for height in sorted(levels, key=lambda h: -h if h is not None else 1):
                stocks = sorted(levels[height], key=lambda s: (s["first_touch_at"] or "9999", s["code"]))
                items.append({"height": height, "height_basis": "intraday_or_target", "count": len(stocks), "stocks": stocks})
            result.append({"theme_id": key if key != snapshot["meta"]["scope"] else None, "name": names.get(key, "全市场"), "levels": items})
        result.sort(key=lambda g: (-sum(level["count"] for level in g["levels"]), g["name"]))
        return {"groups": result, "unique_count": len(selected), "membership_count": sum(len(v) for levels in groups.values() for v in levels.values()), "status": status}

    async def stock(self, snapshot, code):
        if not in_scope(code, snapshot["meta"]["scope"]):
            raise LookupError("该股票不属于当前观测市场范围")
        stock = snapshot["stocks"].get(code)
        if stock is None:
            raise LookupError("该股票不在此快照的观测名单内")
        history = deepcopy(snapshot["history"].get(code, []))
        history.append({"trade_date": snapshot["meta"]["trade_date"], "is_limit_up": stock["is_limit_up"], "is_final": snapshot["meta"]["is_final"]})
        events = await self.repo.events(snapshot, code=code, limit=200)
        return {**stock, "board_history": history[-20:], "events": events,
                "themes": [{"theme_id": t["theme_id"], "name": t["name"]} for t in snapshot["themes"] if t["theme_id"] in stock["theme_ids"]]}


class SnapshotConflict(ValueError):
    pass
