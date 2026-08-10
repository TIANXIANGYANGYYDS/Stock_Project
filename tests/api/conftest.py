from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any


def _value(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if isinstance(current, list):
            return [_value(item, ".".join(path.split(".")[path.split(".").index(part):])) for item in current]
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _contains(value: Any, expected: Any) -> bool:
    if isinstance(value, list):
        return any(_contains(item, expected) for item in value)
    return value == expected


def matches(document: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        if key == "$and" and not all(matches(document, item) for item in expected):
            return False
        if key == "$or" and not any(matches(document, item) for item in expected):
            return False
        if key.startswith("$"):
            continue
        actual = _value(document, key)
        if isinstance(expected, dict):
            if "$elemMatch" in expected:
                values = actual if isinstance(actual, list) else []
                if not any(matches(item, expected["$elemMatch"]) for item in values if isinstance(item, dict)):
                    return False
            if "$regex" in expected:
                pattern = re.compile(expected["$regex"], re.IGNORECASE if expected.get("$options") == "i" else 0)
                values = actual if isinstance(actual, list) else [actual]
                if not any(pattern.search(str(item or "")) for item in values):
                    return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                return False
            if "$lte" in expected and (actual is None or actual > expected["$lte"]):
                return False
            if "$gt" in expected and (actual is None or actual <= expected["$gt"]):
                return False
            if "$lt" in expected and (actual is None or actual >= expected["$lt"]):
                return False
            if "$ne" in expected and _contains(actual, expected["$ne"]):
                return False
            if "$exists" in expected and ((actual is not None) != expected["$exists"]):
                return False
            continue
        if not _contains(actual, expected):
            return False
    return True


def _nested_set(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = document
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def project(document: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(document)
    if not projection:
        return result
    include = any(value == 1 for key, value in projection.items() if key != "_id")
    if include:
        result = {}
        for key, value in projection.items():
            if value != 1 or key == "_id":
                continue
            current = document
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    current = None
                    break
                current = current[part]
            if current is not None:
                _nested_set(result, key, copy.deepcopy(current))
        if projection.get("_id", 1) != 0 and "_id" in document:
            result["_id"] = document["_id"]
        return result
    for key, value in projection.items():
        if value != 0:
            continue
        current = result
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(current, dict):
                break
            current = current.get(part)
        if isinstance(current, dict):
            current.pop(parts[-1], None)
    return result


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    def sort(self, fields: list[tuple[str, int]]):
        for field, direction in reversed(fields):
            self.rows.sort(key=lambda row: (_value(row, field) is None, _value(row, field)), reverse=direction < 0)
        return self

    def skip(self, count: int):
        self.rows = self.rows[count:]
        return self

    def limit(self, count: int):
        self.rows = self.rows[:count]
        return self

    async def to_list(self, length: int | None = None):
        return copy.deepcopy(self.rows if length is None else self.rows[:length])


class FakeCollection:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []

    async def count_documents(self, filters):
        return sum(matches(row, filters) for row in self.rows)

    def find(self, filters=None, projection=None):
        filters = filters or {}
        return FakeCursor([project(row, projection) for row in self.rows if matches(row, filters)])

    async def find_one(self, filters=None, projection=None, sort=None):
        cursor = self.find(filters or {}, projection=projection)
        if sort:
            cursor.sort(sort)
        rows = await cursor.to_list(length=1)
        return rows[0] if rows else None

    def aggregate(self, pipeline):
        rows = copy.deepcopy(self.rows)
        for stage in pipeline:
            if "$match" in stage:
                rows = [row for row in rows if matches(row, stage["$match"])]
            elif "$sort" in stage:
                fields = [(field, direction) for field, direction in stage["$sort"].items()]
                FakeCursor(rows).sort(fields)
            elif "$group" in stage:
                group = stage["$group"]
                if group.get("_id") == "$code":
                    grouped = {}
                    for row in rows:
                        grouped.setdefault(row.get("code"), {"_id": row.get("code"), "latest": row})
                    rows = list(grouped.values())
                elif group.get("_id") in {"$status.status", "$status"}:
                    field = "status.status" if group.get("_id") == "$status.status" else "status"
                    counts = {}
                    for row in rows:
                        key = _value(row, field)
                        counts[key] = counts.get(key, 0) + 1
                    rows = [{"_id": key, "count": value} for key, value in counts.items()]
                elif group.get("_id") == "$code" and "$count" in group:
                    rows = [{"_id": None, "count": len({row.get("code") for row in rows})}]
            elif "$replaceRoot" in stage:
                rows = [row.get("latest", {}) for row in rows]
            elif "$count" in stage:
                rows = [{stage["$count"]: len(rows)}]
            elif "$facet" in stage:
                facet_result = {}
                for name, stages in stage["$facet"].items():
                    facet_rows = copy.deepcopy(rows)
                    for facet_stage in stages:
                        if "$skip" in facet_stage:
                            facet_rows = facet_rows[facet_stage["$skip"]:]
                        elif "$limit" in facet_stage:
                            facet_rows = facet_rows[:facet_stage["$limit"]]
                        elif "$project" in facet_stage:
                            projected = []
                            for row in facet_rows:
                                item = {}
                                for key, value in facet_stage["$project"].items():
                                    if value == 1 and key in row:
                                        item[key] = row[key]
                                    elif isinstance(value, str) and value.startswith("$"):
                                        item[key] = _value(row, value[1:])
                                projected.append(item)
                            facet_rows = projected
                        elif "$count" in facet_stage:
                            facet_rows = [{facet_stage["$count"]: len(facet_rows)}]
                    facet_result[name] = facet_rows
                rows = [facet_result]
        return FakeCursor(rows)


class FakeDatabase:
    def __init__(self, collections: dict[str, FakeCollection], *, ping_ok: bool = True):
        self.collections = collections
        self.ping_ok = ping_ok

    def __getitem__(self, name: str):
        return self.collections.setdefault(name, FakeCollection())

    async def command(self, name: str):
        if not self.ping_ok:
            raise RuntimeError("unavailable")
        return {"ok": 1}


def sample_database() -> FakeDatabase:
    return FakeDatabase(
        {
            "news_data": FakeCollection(
                [
                    {
                        "event_id": "e1",
                        "title": "芯片订单",
                        "content": "半导体订单增长",
                        "publish_ts": 20,
                        "source": "cls",
                        "status": {"status": "finished"},
                        "sector_llm_analysis": [
                            {"sector_name": "半导体", "sector_llm_analysis": {"companies": ["甲公司"]}}
                        ],
                    },
                    {
                        "event_id": "e2",
                        "title": "其他",
                        "content": "其他正文",
                        "publish_ts": 10,
                        "source": "jin10",
                        "status": {"status": "crawled"},
                        "sector_llm_analysis": [],
                    },
                ]
            ),
            "news_ranking_snapshots": FakeCollection(
                [{"snapshot_id": "s1", "biz_date": "2026-08-05", "generated_at": datetime(2026, 8, 5), "status": "completed"}]
            ),
            "daily_market_analysis": FakeCollection(
                [{"analysis_date": "2026-08-05", "status": "completed", "analysis": {"mainlines": []}, "source_analysis_memos": {"news": "large"}}]
            ),
            "stock_daily_detail": FakeCollection(
                [
                    {"code": "000001", "name": "平安银行", "trade_date": "2026-08-05", "trade_date_int": 20260805, "adjust": "qfq", "close": 10, "pct_chg": 1.2},
                    {"code": "000001", "name": "平安银行", "trade_date": "2026-08-04", "trade_date_int": 20260804, "adjust": "qfq", "close": 9, "pct_chg": -1.0},
                    {"code": "000002", "name": "万科A", "trade_date": "2026-08-05", "trade_date_int": 20260805, "adjust": "qfq", "close": 8, "pct_chg": -2.0},
                ]
            ),
            "stock_realtime_minute_bars": FakeCollection(
                [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "market": "SH",
                        "trade_date": "2026-08-05",
                        "interval": "1m",
                        "timestamp": "2026-08-05T14:59:00+08:00",
                        "open": 1308.0,
                        "high": 1310.0,
                        "low": 1307.0,
                        "close": 1309.22,
                        "volume": 1000.0,
                        "amount": 1309220.0,
                        "provider": "TENCENT",
                    },
                    {
                        "code": "000001",
                        "name": "平安银行",
                        "market": "SZ",
                        "trade_date": "2026-08-05",
                        "interval": "1m",
                        "timestamp": "2026-08-05T14:59:00+08:00",
                        "open": 11.2,
                        "high": 11.2,
                        "low": 11.18,
                        "close": 11.19,
                        "volume": 2000.0,
                        "amount": 22380.0,
                        "provider": "TENCENT",
                    },
                ]
            ),
            "creator_works": FakeCollection(
                [{"work_key": "douyin:w1", "creator_id": "c1", "account_id": "douyin:a1", "platform": "douyin", "content_type": "video", "title": "观点", "published_at": datetime(2026, 8, 5), "status": {"status": "finished"}, "source_text": "secret text", "is_a_share_relevant": True, "a_share_opinions": []}]
            ),
            "creator_opinion_analyses": FakeCollection(
                [{"_id": "c1", "creator_name": "作者", "accuracy_score": 80, "verified_opinions": [], "pending_opinions": []}]
            ),
        }
    )
