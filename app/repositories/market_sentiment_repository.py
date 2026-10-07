"""情绪只保留每日依据和每日汇总；汇总发布成功即表示该日完成。"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

from app.services.market_sentiment_metrics import AMOUNT_WINDOW, SCOPE, VERSION

RAW_COLLECTION = "market_sentiment_raw_daily"
DAILY_COLLECTION = "market_sentiment_daily"


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class MarketSentimentRepository:
    def __init__(self, db):
        self.raw, self.daily = db[RAW_COLLECTION], db[DAILY_COLLECTION]

    def ensure_indexes(self):
        # 旧格式必须先显式迁移，避免新旧版本并存或误判为一个空库。
        for collection in (self.raw, self.daily):
            if collection.find_one({"scope": SCOPE, "version": {"$ne": VERSION}}, {"_id": 1}):
                raise ValueError("情绪存储版本不同，请先迁移")
        for collection in (self.raw, self.daily):
            collection.create_index([("scope", 1), ("trade_date", 1)], unique=True)

    def history(self):
        return list(reversed(list(self.daily.find({"scope": SCOPE}, {"_id": 0})
                                  .sort("trade_date", -1).limit(AMOUNT_WINDOW))))

    def raw_for(self, day):
        row = self.raw.find_one({"scope": SCOPE, "trade_date": day}, {"_id": 0})
        if row and row["version"] != VERSION:
            raise ValueError("情绪存储版本不同，请先迁移")
        return row

    def save(self, market, provenance, result, limit_streaks):
        day = market["trade_date"]
        identity = {"scope": SCOPE, "trade_date": day}
        if any(result.get(k) != v for k, v in {**identity, "version": VERSION}.items()):
            raise ValueError("情绪依据与汇总的日期、范围或版本不一致")
        now = datetime.now(timezone.utc)
        input_hash = digest(market)
        existing = self.raw_for(day)
        if existing and (existing["input_hash"] != input_hash or existing["limit_streaks"] != limit_streaks):
            raise ValueError("该日原始依据或连板高度不同，禁止静默改写历史")
        existing_daily = self.daily.find_one(identity)
        if existing_daily:
            stored_result = {k: v for k, v in existing_daily.items() if k not in ("_id", "recorded_at")}
            if stored_result != result:
                raise ValueError("该日统计结果不同，禁止静默改写历史")
        # 先写依据，再发布汇总。中断后复用已存依据，不依赖独立检查点。
        self.raw.update_one(identity, {"$setOnInsert": {**identity, "version": VERSION,
            "market": market, "provenance": provenance, "input_hash": input_hash,
            "limit_streaks": limit_streaks, "recorded_at": now}}, upsert=True)
        self.daily.update_one(identity, {"$setOnInsert": {**result, "recorded_at": now}}, upsert=True)
