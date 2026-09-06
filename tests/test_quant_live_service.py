from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import app.services.quant_live_service as quant_live_module
from app.quant.runtime.daily_flow import (
    PreselectionItem,
    apply_trade_signal,
    create_daily_flow,
    daily_flow_document,
)
from app.quant.runtime.live import opening_flow_document
from app.services.quant_live_service import (
    LATEST_MARK_QUERY_BATCH_SIZE,
    MAX_DAILY_BARS_PER_CODE,
    MAX_MINUTE_ROWS_PER_CODE,
    MAX_TRACKED_CODES,
    MONGO_STREAM_BATCH_SIZE,
    QuantLiveService,
)


TRADE_DATE = "2026-09-03"


class AsyncCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.requested_batch_size = None

    def sort(self, *args, **kwargs):
        return self

    def batch_size(self, value):
        self.requested_batch_size = value
        return self

    def __aiter__(self):
        async def iterate():
            for row in self.rows:
                yield row

        return iterate()


class Collection:
    def __init__(self, rows=()):
        self.cursor = AsyncCursor(rows)
        self.find_called = False

    def find(self, *args, **kwargs):
        self.find_called = True
        return self.cursor

    async def find_one(self, query, *args, sort=None, **kwargs):
        rows = [
            row
            for row in self.cursor.rows
            if not query.get("code") or row.get("code") == query["code"]
        ]
        if sort:
            field, direction = sort[0]
            rows.sort(
                key=lambda row: row.get(field, ""),
                reverse=direction < 0,
            )
        return rows[0] if rows else None


class Database:
    def __init__(self, minute_rows=()):
        self.collections = {
            "quant_daily_results": Collection(),
            "stock_daily_detail": Collection(),
            "stock_realtime_minute_bars": Collection(minute_rows),
        }

    def __getitem__(self, name):
        return self.collections[name]


def minute_rows(code: str, start_minute: int, count: int):
    return [
        {
            "code": code,
            "timestamp": (
                f"{TRADE_DATE}T09:{start_minute + offset:02d}:00+08:00"
            ),
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "previous_close": 10.0,
        }
        for offset in range(count)
    ]


def test_minute_rows_are_streamed_and_released_per_stock() -> None:
    rows = minute_rows("000001", 30, 6) + minute_rows("000002", 30, 3)
    database = Database(rows)
    service = QuantLiveService(database)

    result = asyncio.run(
        service._load_three_minute_bars(
            trade_date=TRADE_DATE,
            codes=["000001", "000002"],
        )
    )

    cursor = database.collections["stock_realtime_minute_bars"].cursor
    assert cursor.requested_batch_size == MONGO_STREAM_BATCH_SIZE
    assert len(result["000001"]) == 2
    assert len(result["000002"]) == 1


def test_tracked_code_guard_fails_before_querying_mongodb() -> None:
    database = Database()
    service = QuantLiveService(database)

    with pytest.raises(RuntimeError, match="内存安全上限"):
        asyncio.run(
            service._load_three_minute_bars(
                trade_date=TRADE_DATE,
                codes=[f"{index:06d}" for index in range(MAX_TRACKED_CODES + 1)],
            )
        )

    assert not database.collections["stock_realtime_minute_bars"].find_called


def test_memory_limits_are_finite_and_above_one_normal_trade_day() -> None:
    assert MAX_MINUTE_ROWS_PER_CODE >= 240
    assert MAX_DAILY_BARS_PER_CODE >= 5_000
    assert MAX_TRACKED_CODES >= 1_000
    assert LATEST_MARK_QUERY_BATCH_SIZE <= 100


def test_holdings_are_revalued_each_minute_without_new_strategy_bar() -> None:
    flow = create_daily_flow(
        trade_date=TRADE_DATE,
        selection_date="2026-09-02",
        generated_at="2026-09-03T09:20:00+08:00",
        candidates=[
            PreselectionItem(
                code="000001",
                name="平安银行",
                reason="测试买入观察",
                reference_price=10.0,
            )
        ],
    )
    flow = apply_trade_signal(
        flow,
        action="buy",
        code="000001",
        signal_at="2026-09-03T09:33:00+08:00",
        signal_price=10.0,
        previous_close=10.0,
        execution_at="2026-09-03T09:33:00+08:00",
        execution_reference_price=10.0,
        reason="测试信号",
    )
    document = daily_flow_document(flow)
    document["runtime"] = {
        "version": 1,
        "expected_complete_bar_count": 1,
        "data_status": "fresh",
    }
    document["_runtime_state"] = {
        "opening_flow": opening_flow_document(flow),
    }

    class Results:
        def __init__(self):
            self.document = document

        async def get(self, trade_date):
            return self.document

        async def save_document(self, value):
            self.document = value

    rows = [
        {
            "code": "000001",
            "timestamp": "2026-09-03T09:33:00+08:00",
            "close": 10.4,
            "previous_close": 10.0,
        }
    ]
    service = QuantLiveService.__new__(QuantLiveService)
    service.results = Results()
    service.minute_collection = Collection(rows)

    result = asyncio.run(
        service.process(
            now=datetime.fromisoformat("2026-09-03T09:34:20+08:00")
        )
    )

    holding = result["holding_pool"]["items"][0]
    assert result["runtime"]["expected_complete_bar_count"] == 1
    assert result["runtime"]["version"] == 2
    assert result["runtime"]["last_valuation_at"] == (
        "2026-09-03T09:34:00+08:00"
    )
    assert holding["mark_price"] == 10.4
    assert holding["market_day_pnl"] == pytest.approx(
        (10.4 - 10.0) * holding["shares"],
        abs=0.01,
    )
    assert holding["account_day_pnl"] == holding["unrealized_pnl"]


def test_fresh_last_bar_snapshot_is_still_finalized_after_close() -> None:
    opening_flow = create_daily_flow(
        trade_date=TRADE_DATE,
        selection_date="2026-09-02",
        generated_at="2026-09-03T09:20:00+08:00",
        candidates=[],
    )
    document = {
        "trade_date": TRADE_DATE,
        "selection_date": "2026-09-02",
            "strategy": {"version": "2.0.0"},
        "status": "monitoring",
        "runtime": {
            "version": 80,
            "expected_complete_bar_count": 80,
            "data_status": "fresh",
        },
        "_runtime_state": {
            "opening_flow": opening_flow_document(opening_flow),
            "observation_specs": [],
            "opening_pending_signals": [],
            "snapshot_key": "before-close",
        },
    }
    saved = []

    class Results:
        async def get(self, trade_date):
            return document

        async def save_document(self, value):
            saved.append(value)

    service = QuantLiveService.__new__(QuantLiveService)
    service.results = Results()

    async def load_bars(*, trade_date, codes):
        return {}

    service._load_three_minute_bars = load_bars

    result = asyncio.run(
        service.process(
            now=datetime.fromisoformat("2026-09-03T15:05:20+08:00")
        )
    )

    assert result["status"] == "closed"
    assert result["runtime"]["data_status"] == "closed"
    assert len(saved) == 1


def test_expired_embedded_calendar_falls_back_to_market_trade_dates(
    monkeypatch,
) -> None:
    class DailyCollection:
        async def find_one(self, query, *args, **kwargs):
            if query.get("trade_date") == TRADE_DATE:
                return None
            return {"trade_date": "2026-09-02"}

    def expired_calendar(target):
        raise RuntimeError("A 股交易日历不覆盖参考日期")

    async def market_calendar(reference_yyyymmdd):
        assert reference_yyyymmdd == "20260903"
        return SimpleNamespace(
            target_trade_date=TRADE_DATE,
            is_reference_trade_day=True,
        )

    monkeypatch.setattr(
        quant_live_module,
        "resolve_morning_trade_dates",
        expired_calendar,
    )
    monkeypatch.setattr(
        quant_live_module,
        "resolve_a_stock_target_trade_date",
        market_calendar,
    )
    service = QuantLiveService.__new__(QuantLiveService)
    service.daily_collection = DailyCollection()

    result = asyncio.run(
        service._resolve_trade_dates(datetime(2026, 9, 3).date())
    )

    assert result.analysis_date == TRADE_DATE
    assert result.prev_trade_date == "2026-09-02"
    assert result.is_current_trade_day is True


def test_partial_close_waits_for_stabilization_then_hard_finalizes() -> None:
    opening_flow = create_daily_flow(
        trade_date=TRADE_DATE,
        selection_date="2026-09-02",
        generated_at="2026-09-03T09:20:00+08:00",
        candidates=[],
    )
    pending = {
        "signal_id": "000001-buy-previous",
        "code": "000001",
        "name": "平安银行",
        "action": "buy",
        "signal_at": "2026-09-02T15:00:00+08:00",
        "signal_price": 10.0,
        "status": "pending_execution",
        "attempt_count": 0,
        "attempts": [],
    }

    class Results:
        def __init__(self):
            self.document = {
                "trade_date": TRADE_DATE,
                "selection_date": "2026-09-02",
            "strategy": {"version": "2.0.0"},
                "status": "monitoring",
                "recording": {"start_date": "2026-08-20"},
                "runtime": {
                    "version": 80,
                    "expected_complete_bar_count": 80,
                    "data_status": "partial",
                },
                "_runtime_state": {
                    "opening_flow": opening_flow_document(opening_flow),
                    "observation_specs": [],
                    "opening_pending_signals": [pending],
                    "snapshot_key": "before-close",
                },
            }

        async def get(self, trade_date):
            return self.document

        async def save_document(self, value):
            self.document = value

    service = QuantLiveService.__new__(QuantLiveService)
    service.results = Results()

    async def load_bars(*, trade_date, codes):
        return {"000001": ()}

    service._load_three_minute_bars = load_bars

    stabilizing = asyncio.run(
        service.process(
            now=datetime.fromisoformat("2026-09-03T15:05:20+08:00")
        )
    )
    finalized = asyncio.run(
        service.process(
            now=datetime.fromisoformat("2026-09-03T15:10:20+08:00")
        )
    )

    assert stabilizing["status"] != "closed"
    assert stabilizing["runtime"]["data_status"] == "partial"
    assert finalized["status"] == "closed"
    assert finalized["runtime"]["data_status"] == "closed_partial"
    assert finalized["runtime"]["incomplete_codes"] == ["000001"]


def test_initial_recording_resets_old_holdings_but_later_dates_require_continuity():
    from app.quant.runtime.daily_flow import IndependentAccount
    from dataclasses import asdict

    class Results:
        def __init__(self, doc):
            self.doc = doc

        async def latest_before(self, day):
            return self.doc

    service = QuantLiveService.__new__(QuantLiveService)
    service.results = Results({'trade_date':'2026-08-19', 'strategy':{'version':'1.0.0'}})
    assert asyncio.run(service._load_previous_state('2026-08-20')) == ([], [], [], {})
    with pytest.raises(RuntimeError, match='前序记录'):
        asyncio.run(service._load_previous_state('2026-09-04'))
    service.results.doc = {'trade_date':'2026-09-03','strategy':{'version':'2.0.0'}, 'status':'closed',
        'recording': {'start_date':'2026-08-20'},
        '_runtime_state':{'accounts':[asdict(IndependentAccount('000001','测试',cash=110000,realized_pnl=10000))],
                          'exit_states':{},'pending_signals':[]}}
    state = asyncio.run(service._load_previous_state('2026-09-04', expected_previous_date='2026-09-03'))
    assert state[2][0].cash == 110000
    with pytest.raises(RuntimeError, match='交易日缺口'):
        asyncio.run(service._load_previous_state('2026-09-07', expected_previous_date='2026-09-04'))


def test_build_pool_freezes_market_adx_endpoints_and_observes_non_sell_holdings(monkeypatch):
    from datetime import date, timedelta
    from dataclasses import asdict
    from app.quant.core.models import Bar
    from app.quant.runtime.daily_flow import HoldingItem, IndependentAccount
    from app.quant.strategies.provisional_daily_macd_3m.adx import daily_adx_snapshot

    dates = [(date(2025,11,5)+timedelta(days=i)).isoformat() for i in range(160)]
    rows = [{'code':'000001','name':'测试','trade_date':day,'open':10+i*.04,
             'high':10.5+i*.05,'low':9.5+i*.03,'close':10+i*.04} for i,day in enumerate(dates)]

    class Daily(Collection):
        async def distinct(self, *args):
            return dates

    service = QuantLiveService.__new__(QuantLiveService)
    service.daily_collection = Daily(rows)
    monkeypatch.setattr(quant_live_module, 'determine_observation_action', lambda *a: 'buy')
    kwargs = {'trade_date':'2026-09-03', 'selection_date':dates[-1], 'opening_pending':[], 'holdings':[]}
    candidates, _, specs, quality, accounts = asyncio.run(service._build_observation_pool(**kwargs))
    assert len(candidates) == len(accounts) == 1
    assert specs[0].factor_completed_date == dates[-1]
    assert specs[0].factor_comparison_date == dates[-4]
    b = [Bar(r['trade_date'],r['open'],r['high'],r['low'],r['close']) for r in rows]
    expected = daily_adx_snapshot(bars=b, trade_date='2026-09-03', completed_date=dates[-1], comparison_date=dates[-4])
    assert specs[0].adx == expected.value('adx_14')
    flow = create_daily_flow(trade_date='2026-09-01',selection_date='2026-08-31',generated_at='2026-09-01T09:20:00+08:00',
        candidates=[PreselectionItem('000001','测试','买入',10.)], accounts=accounts)
    flow = apply_trade_signal(flow,action='buy',code='000001',signal_at='2026-09-01T10:00:00+08:00',
        signal_price=10.,previous_close=10.,reason='entry')
    monkeypatch.setattr(quant_live_module, 'determine_observation_action', lambda *a: None)
    candidates, sellers, specs, _, _ = asyncio.run(service._build_observation_pool(
        **{**kwargs,'holdings':flow.holdings,'accounts':flow.accounts}))
    assert not candidates and not sellers
    assert specs[0].action == 'hold'
    assert specs[0].previous_state is not None


def test_catchup_records_missing_days_in_order_and_skips_closed_days():
    from datetime import date

    class Daily:
        async def distinct(self, *args):
            return ['2026-09-04','2026-09-03']

    class Minutes:
        async def find_one(self, *args):
            return {'_id':'observed'}

    class Results:
        async def get(self, day):
            return None

    service = QuantLiveService.__new__(QuantLiveService)
    service.daily_collection, service.minute_collection, service.results = Daily(), Minutes(), Results()
    processed = []

    async def process(*, now):
        processed.append(now.date().isoformat())
        return {'status':'closed'}

    service.process = process
    assert asyncio.run(service.catch_up_completed_days(before_date=date(2026,9,6))) == ['2026-09-03','2026-09-04']
    assert processed == ['2026-09-03','2026-09-04']


def test_rebased_accounts_cannot_continue_from_old_recording_origin():
    from dataclasses import asdict
    from app.quant.runtime.daily_flow import IndependentAccount

    class Results:
        async def latest_before(self, day):
            return {'trade_date': '2026-09-04', 'strategy': {'version': '2.0.0'}, 'status': 'closed',
                    'recording': {'start_date': '2026-09-03'},
                    '_runtime_state': {'accounts': [asdict(IndependentAccount('000001', '测试'))]}}

    service = QuantLiveService.__new__(QuantLiveService)
    service.results = Results()
    with pytest.raises(RuntimeError, match='起点不一致'):
        asyncio.run(service._load_previous_state('2026-09-07', expected_previous_date='2026-09-04'))
