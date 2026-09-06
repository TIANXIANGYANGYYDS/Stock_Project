from __future__ import annotations

from datetime import datetime

from app.quant.runtime.daily_flow import PreselectionItem, create_daily_flow
from app.quant.runtime.daily_macd import DailyMacdState
from app.quant.runtime.live import (
    MAX_STORED_EXECUTION_ATTEMPTS,
    LiveObservationSpec,
    _record_execution_attempt,
    aggregate_complete_three_minute_bars,
    expected_completed_bar_count,
    replay_live_day,
    three_minute_bar_ends,
)


TRADE_DATE = "2026-09-03"


def minute_rows(
    *,
    minutes: int,
    price: float = 10.0,
    previous_close: float | None = 10.0,
) -> list[dict[str, object]]:
    rows = []
    for minute in range(30, 30 + minutes):
        rows.append(
            {
                "timestamp": f"{TRADE_DATE}T09:{minute:02d}:00+08:00",
                "open": price,
                "high": price + 0.02,
                "low": price - 0.02,
                "close": price,
                "previous_close": previous_close,
            }
        )
    return rows


def opening_flow():
    return create_daily_flow(
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


def buy_spec() -> LiveObservationSpec:
    return LiveObservationSpec(
        code="000001",
        name="平安银行",
        action="buy",
        observation_before_date="2026-09-01",
        observation_date="2026-09-02",
        previous_close=10.0,
        reference_histogram=-1.5,
        previous_state=DailyMacdState(fast_ema=9.0, slow_ema=10.0, dea=-0.2),
        adx=30., adx_3_days_ago=25.,
        factor_completed_date="2026-09-02", factor_comparison_date="2026-08-28",
    )


def test_live_three_minute_schedule_respects_lunch_break() -> None:
    ends = three_minute_bar_ends(TRADE_DATE)

    assert len(ends) == 80
    assert ends[0] == "2026-09-03T09:33:00+08:00"
    assert ends[39] == "2026-09-03T11:30:00+08:00"
    assert ends[40] == "2026-09-03T13:03:00+08:00"
    assert ends[-1] == "2026-09-03T15:00:00+08:00"
    assert expected_completed_bar_count(
        datetime.fromisoformat("2026-09-03T11:45:00+08:00"), TRADE_DATE
    ) == 40


def test_aggregate_stops_at_first_missing_minute() -> None:
    rows = minute_rows(minutes=9)
    del rows[4]

    bars = aggregate_complete_three_minute_bars(rows, trade_date=TRADE_DATE)

    assert len(bars) == 1
    assert bars[0].start_at == "2026-09-03T09:30:00+08:00"
    assert bars[0].end_at == "2026-09-03T09:33:00+08:00"


def test_three_confirmations_fill_at_next_bar_open() -> None:
    bars = aggregate_complete_three_minute_bars(
        minute_rows(minutes=12), trade_date=TRADE_DATE
    )

    result = replay_live_day(
        opening_flow=opening_flow(),
        observation_specs=[buy_spec()],
        opening_pending_signals=[],
        bars_by_code={"000001": bars},
        expected_bar_count=4,
        close_market=False,
    )

    signal = result["signals"][0]
    execution = result["flow"].executions[0]
    observation = result["observations"][0]
    assert signal["signal_at"] == "2026-09-03T09:39:00+08:00"
    assert signal["status"] == "filled"
    assert execution.execution_at == "2026-09-03T09:39:00+08:00"
    assert execution.execution_reference_price == 10.0
    assert observation["state"] == "filled"
    assert result["pending_signals"] == ()


def test_missing_previous_close_blocks_signal() -> None:
    bars = aggregate_complete_three_minute_bars(
        minute_rows(minutes=12, previous_close=None), trade_date=TRADE_DATE
    )

    result = replay_live_day(
        opening_flow=opening_flow(),
        observation_specs=[buy_spec()],
        opening_pending_signals=[],
        bars_by_code={"000001": bars},
        expected_bar_count=4,
        close_market=False,
    )

    assert result["signals"] == ()
    assert result["observations"][0]["data_status"] == "missing_previous_close"


def test_carried_buy_is_cancelled_at_limit_up() -> None:
    bars = aggregate_complete_three_minute_bars(
        minute_rows(minutes=3, price=11.0), trade_date=TRADE_DATE
    )
    pending = {
        "signal_id": "000001-buy-previous",
        "code": "000001",
        "name": "平安银行",
        "action": "buy",
        "signal_at": "2026-09-02T15:00:00+08:00",
        "signal_price": 10.0,
        "status": "pending_execution",
        "attempts": [],
    }

    result = replay_live_day(
        opening_flow=opening_flow(),
        observation_specs=[],
        opening_pending_signals=[pending],
        bars_by_code={"000001": bars},
        expected_bar_count=1,
        close_market=False,
    )

    assert result["signals"][0]["status"] == "rejected_limit_up"
    assert result["flow"].executions == ()
    assert result["pending_signals"] == ()


def test_execution_attempt_history_has_a_hard_retention_limit() -> None:
    signal = {"attempt_count": 0, "attempts": []}

    for index in range(MAX_STORED_EXECUTION_ATTEMPTS + 5):
        _record_execution_attempt(signal, {"sequence": index})

    assert signal["attempt_count"] == MAX_STORED_EXECUTION_ATTEMPTS + 5
    assert len(signal["attempts"]) == MAX_STORED_EXECUTION_ATTEMPTS
    assert signal["attempts"][0]["sequence"] == 5
