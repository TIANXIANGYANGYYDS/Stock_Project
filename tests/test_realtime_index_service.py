from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services.realtime_index_service import (
    CN_TZ,
    INDEX_DEFINITIONS,
    IndexQuote,
    RealtimeIndexService,
    RealtimeIndexUnavailable,
)


class FakeWallClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeProvider:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    async def fetch(self, definitions):
        self.calls += 1
        if self.fail:
            raise RuntimeError("test_failure")
        source_time = datetime.now(CN_TZ)
        return tuple(
            IndexQuote(
                code=definition.code,
                provider=self.name,
                price=3500.0 + index,
                previous_close=3490.0 + index,
                change=10.0,
                change_pct=0.2865,
                open_price=3495.0 + index,
                high=3510.0 + index,
                low=3480.0 + index,
                volume=100 + index,
                amount=1000 + index,
                market_data_time=source_time,
                received_at=source_time,
            )
            for index, definition in enumerate(definitions)
        )

    async def close(self):
        return None


def test_index_service_fetches_each_open_request_and_freezes_after_close() -> None:
    async def run() -> None:
        wall_clock = FakeWallClock(datetime(2026, 8, 10, 9, 30, tzinfo=CN_TZ))
        primary = FakeProvider("TENCENT")
        service = RealtimeIndexService(
            primary=primary,
            backup=FakeProvider("SINA", fail=True),
            wall_clock=wall_clock,
        )
        try:
            first = await service.fetch_latest()
            wall_clock.value += timedelta(seconds=1)
            second = await service.fetch_latest()
            wall_clock.value = datetime(2026, 8, 10, 15, 1, tzinfo=CN_TZ)
            closed = await service.fetch_latest()
        finally:
            await service.close()

        assert primary.calls == 2
        assert first["items"][0]["symbol"] == INDEX_DEFINITIONS[0].symbol
        assert second["updated_at"] != first["updated_at"]
        assert closed["updated_at"] == second["updated_at"]
        assert closed["market_status"] == "closed"

    asyncio.run(run())


def test_index_service_falls_back_to_sina() -> None:
    async def run() -> dict:
        service = RealtimeIndexService(
            primary=FakeProvider("TENCENT", fail=True),
            backup=FakeProvider("SINA"),
        )
        try:
            return await service.fetch_latest()
        finally:
            await service.close()

    result = asyncio.run(run())
    assert result["items"][0]["provider"] == "sina"


def test_index_service_reports_unavailable_without_cache() -> None:
    async def run() -> None:
        service = RealtimeIndexService(
            primary=FakeProvider("TENCENT", fail=True),
            backup=FakeProvider("SINA", fail=True),
        )
        try:
            with pytest.raises(RealtimeIndexUnavailable):
                await service.fetch_latest()
        finally:
            await service.close()

    asyncio.run(run())


def test_index_service_initializes_once_after_close() -> None:
    async def run() -> None:
        wall_clock = FakeWallClock(datetime(2026, 8, 10, 16, 0, tzinfo=CN_TZ))
        primary = FakeProvider("TENCENT")
        service = RealtimeIndexService(
            primary=primary,
            backup=FakeProvider("SINA", fail=True),
            wall_clock=wall_clock,
        )
        try:
            await service.fetch_latest()
            await service.fetch_latest()
        finally:
            await service.close()
        assert primary.calls == 1

    asyncio.run(run())


def test_index_service_refreshes_closed_snapshot_on_next_calendar_day() -> None:
    async def run() -> None:
        wall_clock = FakeWallClock(datetime(2026, 8, 10, 16, 0, tzinfo=CN_TZ))
        primary = FakeProvider("TENCENT")
        service = RealtimeIndexService(
            primary=primary,
            backup=FakeProvider("SINA", fail=True),
            wall_clock=wall_clock,
        )
        try:
            first = await service.fetch_latest()
            wall_clock.value = datetime(2026, 8, 11, 16, 0, tzinfo=CN_TZ)
            second = await service.fetch_latest()
        finally:
            await service.close()

        assert primary.calls == 2
        assert second["updated_at"] != first["updated_at"]

    asyncio.run(run())
