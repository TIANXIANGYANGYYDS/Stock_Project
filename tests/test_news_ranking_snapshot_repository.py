from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.daily_market_analysis import SectorRankingItem
from app.models.news_ranking_snapshot import (
    NewsRankingFormulaVersions,
    NewsRankingSnapshot,
    NewsRankingSourceStats,
)
from app.repositories.news_ranking_snapshot_repository import (
    NewsRankingSnapshotRepository,
)


def build_snapshot(
    *,
    snapshot_id: str = "20260723_085800_test",
    generated_at: datetime = datetime(2026, 7, 23, 8, 58, tzinfo=timezone.utc),
    window_start_ts: int = 1_000,
    window_end_ts: int = 2_000,
) -> NewsRankingSnapshot:
    ranking = [
        SectorRankingItem(
            rank=1,
            sector_name="半导体",
            final_score=88.2,
            news_count=12,
        )
    ]
    return NewsRankingSnapshot(
        snapshot_id=snapshot_id,
        biz_date="2026-07-23",
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        generated_at=generated_at,
        source_stats=NewsRankingSourceStats(
            total_news_count=20,
            investment_eligible_count=12,
            heat_eligible_count=18,
            status_counts={"finished": 12, "sector_judged": 6, "crawled": 2},
        ),
        formula_versions=NewsRankingFormulaVersions(
            investment="investment_v2",
            heat="heat_v2",
        ),
        investment_ranking=ranking,
        heat_ranking=ranking,
    )


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.index_calls: list[dict[str, Any]] = []
        self.drop_index_calls: list[str] = []
        self.update_calls: list[dict[str, Any]] = []
        self.find_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.before_delete = None

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        self.index_calls.append({"keys": keys, **kwargs})
        return str(kwargs.get("name") or "index")

    async def index_information(self) -> dict[str, Any]:
        return {"uk_biz_date": {"key": [("biz_date", 1)], "unique": True}}

    async def drop_index(self, name: str) -> None:
        self.drop_index_calls.append(name)

    async def update_one(
        self,
        filters: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool,
    ) -> None:
        self.update_calls.append(
            {"filters": filters, "update": update, "upsert": upsert}
        )
        document = dict(update["$set"])
        self.documents = [
            row
            for row in self.documents
            if row.get("snapshot_id") != document.get("snapshot_id")
        ]
        self.documents.append(document)

    async def find_one(
        self,
        filters: dict[str, Any],
        *,
        projection: dict[str, Any] | None,
        sort: list[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        self.find_calls.append(
            {"filters": filters, "projection": projection, "sort": sort}
        )
        matches = [row for row in self.documents if self._matches(row, filters)]
        for key, direction in reversed(sort or []):
            matches.sort(key=lambda row: row.get(key, 0), reverse=direction < 0)
        return dict(matches[0]) if matches else None

    def find(
        self,
        filters: dict[str, Any],
        *,
        projection: dict[str, Any] | None,
        sort: list[tuple[str, int]] | None,
        skip: int,
    ) -> "FakeCursor":
        matches = [row for row in self.documents if self._matches(row, filters)]
        return FakeCursor(matches, projection=projection, sort=sort, skip=skip)

    async def delete_many(self, filters: dict[str, Any]) -> None:
        self.delete_calls.append(filters)
        if self.before_delete is not None:
            self.before_delete(self)
        self.documents = [
            row for row in self.documents if not self._matches(row, filters)
        ]

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, expected in filters.items():
            if key == "$or":
                if not any(FakeCollection._matches(row, item) for item in expected):
                    return False
                continue
            actual = row.get(key)
            if isinstance(expected, dict):
                for operator, value in expected.items():
                    if operator == "$lt" and not actual < value:
                        return False
                    if operator == "$lte" and not actual <= value:
                        return False
                    if operator == "$gt" and not actual > value:
                        return False
                    if operator == "$nin" and actual in value:
                        return False
            elif actual != expected:
                return False
        return True


class FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        projection: dict[str, Any] | None,
        sort: list[tuple[str, int]] | None,
        skip: int,
    ) -> None:
        self.rows = list(rows)
        for key, direction in reversed(sort or []):
            self.rows.sort(
                key=lambda row: row.get(key, 0),
                reverse=direction < 0,
            )
        self.rows = self.rows[skip:]
        if projection is not None:
            included = {key for key, value in projection.items() if value and key != "_id"}
            self.rows = [
                {key: value for key, value in row.items() if key in included}
                for row in self.rows
            ]

    def limit(self, value: int) -> "FakeCursor":
        self.rows = self.rows[:value]
        return self

    async def to_list(self, *, length: int | None) -> list[dict[str, Any]]:
        return self.rows if length is None else self.rows[:length]


class FakeDatabase:
    def __init__(self, collection: FakeCollection) -> None:
        self.collection = collection
        self.requested_collection_names: list[str] = []

    def __getitem__(self, collection_name: str) -> FakeCollection:
        self.requested_collection_names.append(collection_name)
        return self.collection


def test_snapshot_requires_aware_generated_at_and_valid_window() -> None:
    with pytest.raises(ValidationError, match="timezone_aware"):
        build_snapshot(generated_at=datetime(2026, 7, 23, 8, 58))

    with pytest.raises(ValidationError, match="window_end_ts"):
        build_snapshot(window_start_ts=2_001, window_end_ts=2_000)


def test_repository_keeps_history_and_returns_latest_snapshot_before_cutoff() -> None:
    collection = FakeCollection()
    database = FakeDatabase(collection)
    repository = NewsRankingSnapshotRepository(database=database)  # type: ignore[arg-type]
    first = build_snapshot(snapshot_id="first")
    second = build_snapshot(snapshot_id="second")

    async def run_repository_calls() -> NewsRankingSnapshot | None:
        await repository.create_indexes()
        await repository.upsert_snapshot(first)
        await repository.upsert_snapshot(second)
        collection.documents[0]["generated_at"] = datetime(2026, 7, 23, 0, 58)
        return await repository.find_latest_completed_by_biz_date(
            "2026-07-23",
            window_end_ts_lte=first.window_end_ts,
        )

    result = asyncio.run(run_repository_calls())

    assert database.requested_collection_names == ["news_ranking_snapshots"]
    assert [call["name"] for call in collection.index_calls] == [
        "uk_snapshot_id",
        "idx_biz_date_status_window_end_ts",
    ]
    assert collection.index_calls[0]["unique"] is True
    assert collection.drop_index_calls == ["uk_biz_date"]
    assert len(collection.update_calls) == 2
    assert [call["filters"] for call in collection.update_calls] == [
        {"snapshot_id": "first"},
        {"snapshot_id": "second"},
    ]
    assert all(call["upsert"] is True for call in collection.update_calls)
    assert collection.find_calls == [
        {
            "filters": {
                "biz_date": "2026-07-23",
                "status": "completed",
                "window_end_ts": {"$lte": first.window_end_ts},
            },
            "projection": {"_id": 0},
            "sort": [("window_end_ts", -1)],
        }
    ]
    assert isinstance(result, NewsRankingSnapshot)
    assert result is not None
    assert result.snapshot_id == "first"
    assert result.generated_at.utcoffset().total_seconds() == 8 * 3600


def test_repository_prunes_to_current_and_latest_premarket_snapshot() -> None:
    collection = FakeCollection()
    repository = NewsRankingSnapshotRepository(  # type: ignore[arg-type]
        database=FakeDatabase(collection)
    )
    collection.documents = [
        build_snapshot(snapshot_id="0845", window_end_ts=1_800).model_dump(),
        build_snapshot(snapshot_id="0855", window_end_ts=1_900).model_dump(),
        build_snapshot(snapshot_id="0905", window_end_ts=2_100).model_dump(),
        build_snapshot(snapshot_id="0910", window_end_ts=2_200).model_dump(),
    ]

    asyncio.run(
        repository.prune_redundant_day_snapshots(
            biz_date="2026-07-23",
            morning_cutoff_ts=2_000,
        )
    )

    assert {row["snapshot_id"] for row in collection.documents} == {"0855", "0910"}


def test_repository_pruning_does_not_delete_concurrent_better_snapshots() -> None:
    collection = FakeCollection()
    repository = NewsRankingSnapshotRepository(  # type: ignore[arg-type]
        database=FakeDatabase(collection)
    )
    collection.documents = [
        build_snapshot(snapshot_id="0845", window_end_ts=1_800).model_dump(),
        build_snapshot(snapshot_id="0855", window_end_ts=1_900).model_dump(),
        build_snapshot(snapshot_id="0905", window_end_ts=2_100).model_dump(),
        build_snapshot(snapshot_id="0910", window_end_ts=2_200).model_dump(),
    ]

    def insert_concurrent_snapshots(active_collection: FakeCollection) -> None:
        active_collection.documents.extend(
            [
                build_snapshot(
                    snapshot_id="0858-concurrent",
                    window_end_ts=1_950,
                ).model_dump(),
                build_snapshot(
                    snapshot_id="0915-concurrent",
                    window_end_ts=2_300,
                ).model_dump(),
            ]
        )

    collection.before_delete = insert_concurrent_snapshots
    asyncio.run(
        repository.prune_redundant_day_snapshots(
            biz_date="2026-07-23",
            morning_cutoff_ts=2_000,
        )
    )

    assert {row["snapshot_id"] for row in collection.documents} == {
        "0855",
        "0858-concurrent",
        "0910",
        "0915-concurrent",
    }


def test_repository_returns_none_when_only_snapshot_is_after_cutoff() -> None:
    collection = FakeCollection()
    collection.documents = [
        build_snapshot(snapshot_id="afternoon", window_end_ts=2_100).model_dump()
    ]
    repository = NewsRankingSnapshotRepository(  # type: ignore[arg-type]
        database=FakeDatabase(collection)
    )

    result = asyncio.run(
        repository.find_latest_completed_by_biz_date(
            "2026-07-23",
            window_end_ts_lte=2_000,
        )
    )

    assert result is None


def test_repository_returns_none_without_completed_snapshot() -> None:
    collection = FakeCollection()
    repository = NewsRankingSnapshotRepository(  # type: ignore[arg-type]
        database=FakeDatabase(collection)
    )

    result = asyncio.run(
        repository.find_latest_completed_by_biz_date("2026-07-23")
    )

    assert result is None
