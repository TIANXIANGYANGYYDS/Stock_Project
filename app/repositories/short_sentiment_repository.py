"""单调发布：事件/分钟数据先写，日快照最后提交；读者使用提交序号。"""
from __future__ import annotations

import asyncio
import gzip
import json

from bson import Binary
from pymongo import UpdateOne

from app.services.short_sentiment.engine import SCOPE, public_stats
from app.services.short_sentiment.retention import usable_observation

SNAPSHOTS = "short_sentiment_snapshots"
EVENTS = "short_sentiment_events"
TIMESERIES = "short_sentiment_timeseries"
RETAINED = "short_sentiment_retained"


def pack(snapshot):
    return Binary(gzip.compress(json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(), compresslevel=3, mtime=0))


def unpack(document):
    return json.loads(gzip.decompress(bytes(document["payload"])))


class ShortSentimentRepository:
    def __init__(self, db, *, scope=SCOPE):
        self.db = db
        self.scope = scope

    async def ensure_indexes(self):
        await self.db[SNAPSHOTS].create_index([("scope", 1), ("trade_date", -1)], unique=True)
        await self.db[RETAINED].create_index([("scope", 1), ("trade_date", -1)], unique=True)
        await self.db[EVENTS].create_index([("scope", 1), ("trade_date", 1), ("event_id", 1)])
        await self.db[EVENTS].create_index([("trade_date", 1), ("theme_ids", 1), ("event_id", 1)])
        await self.db[EVENTS].create_index([("trade_date", 1), ("code", 1), ("event_id", 1)])
        await self.db[TIMESERIES].create_index([("scope", 1), ("trade_date", 1), ("interval", 1), ("timestamp", 1)])

    async def latest(self, trade_date=None):
        query = {"scope": self.scope}
        if trade_date:
            query["trade_date"] = trade_date
        document = await self.db[SNAPSHOTS].find_one(query, sort=[("trade_date", -1)])
        return await asyncio.to_thread(unpack, document) if document else None

    async def checkpoint(self, day):
        document = await self.db[SNAPSHOTS].find_one({"scope": self.scope, "trade_date": day})
        if not document:
            return None
        payload = document.get("processing_payload") or document["payload"]
        return await asyncio.to_thread(unpack, {"payload": payload})

    async def retained(self, day):
        document = await self.db[RETAINED].find_one({"scope": self.scope, "trade_date": day})
        return await asyncio.to_thread(unpack, document) if document else None

    async def save_retained(self, snapshot, payload=None):
        if snapshot["meta"]["scope"] != self.scope or not usable_observation(snapshot):
            raise ValueError("只能保存同范围的有效盘中观察")
        meta = snapshot["meta"]
        payload = payload if payload is not None else await asyncio.to_thread(pack, snapshot)
        await self.db[RETAINED].update_one({"scope": self.scope, "trade_date": meta["trade_date"]},
            {"$set": {"sequence": snapshot["sequence"], "meta": meta, "payload": payload}}, upsert=True)

    async def publish(self, snapshot, *, observation=None, write_timeseries=True):
        meta = snapshot["meta"]
        observation = snapshot if observation is None else observation
        if any(observation["meta"][k] != meta[k] for k in ("scope", "trade_date", "snapshot_id")):
            raise ValueError("公开快照与处理观察的发布身份不一致")
        if meta["scope"] != self.scope:
            raise ValueError("发布范围与仓库范围不一致")
        day, seq = meta["trade_date"], snapshot["sequence"]
        committed = await self.db[SNAPSHOTS].find_one({"scope": self.scope, "trade_date": day},
                                                    {"sequence": 1, "input_id": 1})
        if committed and committed["sequence"] >= seq:
            if committed["sequence"] == seq and committed.get("input_id") == snapshot["input_id"]:
                return
            raise ValueError("拒绝覆盖已提交的情绪快照或倒退发布序号")
        # A failed publication may be retried after a late source batch arrived.
        # Clear only this still-unpublished sequence so orphan event IDs cannot
        # become visible when that sequence is reused for the earlier source.
        await self.db[EVENTS].delete_many({"scope": self.scope, "trade_date": day, "sequence": seq})
        await self.db[TIMESERIES].delete_many({"scope": self.scope, "trade_date": day, "sequence": seq})
        events = observation["events"]
        if events:
            await self.db[EVENTS].bulk_write([UpdateOne({"_id": e["event_id"]}, {"$setOnInsert": e}, upsert=True) for e in events], ordered=True)
        observed_meta = observation["meta"]
        interval = "1d" if observed_meta["is_final"] else "1m"
        # First processed observation of each minute, not a fabricated bar close.
        # Immutable rows make failed publications invisible to readers.
        timestamp = observed_meta["as_of"] if interval == "1d" else observed_meta["as_of"][:16] + ":00+08:00"
        series = {"scope": self.scope, "trade_date": day, "sequence": seq, "interval": interval,
                  "timestamp": timestamp, "as_of": observed_meta["as_of"], "is_final": observed_meta["is_final"],
                  "snapshot_id": meta["snapshot_id"], "rule_version": meta["rule_version"],
                  "classification_version": meta["classification_version"],
                  "market": public_stats(observation["market"]),
                  "themes": [public_stats(t) for t in observation["themes"]]}
        if write_timeseries and (interval == "1d" or observation.get("series_minute") != timestamp):
            await self.db[TIMESERIES].update_one({"_id": meta["snapshot_id"]}, {"$setOnInsert": series}, upsert=True)
            snapshot["series_minute"] = timestamp
            observation["series_minute"] = timestamp
        if write_timeseries and observation.get("environment_updated"):
            environment_at = observation["market"]["emotion"]["evaluated_at"]
            environment_series = {**series, "interval": "5m", "timestamp": environment_at,
                                  "is_final": False, "as_of": observed_meta["as_of"]}
            await self.db[TIMESERIES].update_one({"_id": meta["snapshot_id"] + ":5m"},
                {"$setOnInsert": environment_series}, upsert=True)
        # One compressed document per day contains the full restart checkpoint.
        payload = await asyncio.to_thread(pack, snapshot)
        processing_payload = await asyncio.to_thread(pack, observation) if observation is not snapshot else None
        await self.db[SNAPSHOTS].update_one({"scope": self.scope, "trade_date": day},
            {"$set": {"sequence": seq, "input_id": snapshot["input_id"], "snapshot_id": meta["snapshot_id"],
                      "meta": meta, "payload": payload, "processing_payload": processing_payload}}, upsert=True)
        if write_timeseries and usable_observation(observation):
            await self.save_retained(observation, processing_payload if processing_payload is not None else payload)

    async def events(self, snapshot, *, after=None, limit=100, theme_id=None, code=None, event_type=None):
        query = {"scope": self.scope, "trade_date": snapshot["meta"]["trade_date"], "sequence": {"$lte": snapshot["sequence"]}}
        if after:
            query["event_id"] = {"$gt": after}
        if theme_id:
            query["theme_ids"] = theme_id
        if code:
            query["code"] = code
        if event_type:
            query["type"] = event_type
        rows = await self.db[EVENTS].find(query, {"_id": 0}).sort([("event_id", 1)]).limit(limit + 1).to_list(length=limit + 1)
        return {"items": rows[:limit], "has_more": len(rows) > limit,
                "next_cursor": rows[min(limit, len(rows)) - 1]["event_id"] if rows else after}

    async def recent_events(self, snapshot, limit=20):
        return await self.db[EVENTS].find(
            {"scope": self.scope, "trade_date": snapshot["meta"]["trade_date"], "sequence": {"$lte": snapshot["sequence"]}},
            {"_id": 0}).sort([("event_id", -1)]).limit(limit).to_list(length=limit)

    async def timeseries(self, start, end, interval, theme_id=None, snapshot=None):
        commits = await self.db[SNAPSHOTS].find({"scope": self.scope, "trade_date": {"$gte": start, "$lte": end}}, {"trade_date": 1, "sequence": 1}).to_list(length=370)
        if not commits:
            return []
        if snapshot:
            for row in commits:
                if row["trade_date"] == snapshot["meta"]["trade_date"]:
                    row["sequence"] = min(row["sequence"], snapshot["sequence"])
        query = {"scope": self.scope, "interval": interval, "$or": [
            {"trade_date": row["trade_date"], "sequence": {"$lte": row["sequence"]}} for row in commits]}
        field = "themes" if theme_id else "market"
        projection = {"_id": 0, "timestamp": 1, "sequence": 1, "as_of": 1, "snapshot_id": 1,
                      "is_final": 1, "classification_version": 1, "rule_version": 1,
                      field: {"$filter": {"input": "$themes", "as": "theme",
                                          "cond": {"$eq": ["$$theme.theme_id", theme_id]}}} if theme_id else 1}
        pipeline = [{"$match": query}, {"$project": projection}, {"$sort": {"timestamp": 1, "sequence": -1}},
                    {"$group": {"_id": "$timestamp", "row": {"$first": "$$ROOT"}}},
                    {"$replaceRoot": {"newRoot": "$row"}}, {"$sort": {"timestamp": 1}},
                    {"$project": {"sequence": 0}}]
        rows = await self.db[TIMESERIES].aggregate(pipeline).to_list(length=10000)
        if theme_id:
            for row in rows:
                row["theme"] = next((t for t in row.pop("themes") if t["theme_id"] == theme_id), None)
        return rows
