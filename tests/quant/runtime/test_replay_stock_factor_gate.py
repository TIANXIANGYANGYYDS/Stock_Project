from __future__ import annotations

import pytest

from app.quant.cli.replay_stock import replay
from app.quant.core.models import BacktestConfig, Bar


def _fixture() -> tuple[list[Bar], dict[str, list[Bar]], BacktestConfig]:
    closes = (10.0, 9.0, 8.0, 8.01)
    daily_bars = [
        Bar(
            trade_date=f"2026-01-{index:02d}",
            open=close,
            high=close,
            low=close,
            close=close,
        )
        for index, close in enumerate(closes, start=1)
    ]
    minute_bars = {
        "2026-01-04": [
            Bar(
                trade_date=f"2026-01-04T09:{30 + index:02d}:00+08:00",
                open=8.01,
                high=8.01,
                low=8.01,
                close=8.01,
            )
            for index in range(80)
        ]
    }
    config = BacktestConfig(
        code="000001",
        fast_period=2,
        slow_period=4,
        signal_period=3,
        warmup_bars=3,
    )
    return daily_bars, minute_bars, config


def _replay(**kwargs: object) -> dict[str, object]:
    daily_bars, minute_bars, config = _fixture()
    return replay(
        code="000001",
        name="测试股票",
        daily_bars=daily_bars,
        minute_bars_by_date=minute_bars,
        start_date="2026-01-04",
        end_date="2026-01-04",
        config=config,
        **kwargs,
    )


def test_default_replay_keeps_official_macd_execution_unchanged() -> None:
    result = _replay()
    summary = result["summary"]
    signal = result["signal_rows"][0]

    assert summary["buy_signal_count"] == 1
    assert summary["filled_buy_count"] == 1
    assert summary["end_holding"] is True
    assert signal["final_status"] == "filled"
    assert "factor_gate_passed" not in signal


def test_research_gate_rejects_after_macd_signal_without_changing_signal() -> None:
    calls: list[tuple[str, str]] = []

    def reject(code: str, trade_date: str) -> tuple[bool, str]:
        calls.append((code, trade_date))
        return False, "RSRank60_5 < 70%"

    result = _replay(buy_signal_gate=reject)
    summary = result["summary"]
    signal = result["signal_rows"][0]

    assert calls == [("000001", "2026-01-04")]
    assert summary["buy_signal_count"] == 1
    assert summary["filled_buy_count"] == 0
    assert summary["end_holding"] is False
    assert signal["action"] == "buy"
    assert signal["confirmation_count"] == 3
    assert signal["factor_gate_passed"] is False
    assert signal["factor_gate_reason"] == "RSRank60_5 < 70%"
    assert signal["final_status"] == "rejected_factor"
    assert result["event_rows"] == []


def test_closed_trade_records_intraday_mae_mfe_and_holding_sessions() -> None:
    closes = (10.0, 9.0, 8.0, 8.01, 9.0, 10.0, 9.9)
    daily_bars = []
    minute_bars = {}
    ranges = {
        4: (7.8, 8.2),
        5: (8.5, 9.5),
        6: (9.0, 10.5),
        7: (9.5, 10.0),
    }
    for day, close in enumerate(closes, start=1):
        low, high = ranges.get(day, (close, close))
        trade_date = f"2026-01-{day:02d}"
        daily_bars.append(Bar(trade_date, close, high, low, close))
        if day >= 4:
            minute_bars[trade_date] = [
                Bar(
                    trade_date=f"{trade_date}Tbar{index:03d}",
                    open=close,
                    high=high,
                    low=low,
                    close=close,
                )
                for index in range(80)
            ]
    config = BacktestConfig(
        code="000001",
        fast_period=2,
        slow_period=4,
        signal_period=3,
        warmup_bars=3,
    )

    result = replay(
        code="000001",
        name="测试股票",
        daily_bars=daily_bars,
        minute_bars_by_date=minute_bars,
        start_date="2026-01-04",
        end_date="2026-01-07",
        config=config,
    )
    trade = result["closed_trade_rows"][0]

    assert result["summary"]["filled_buy_count"] == 1
    assert result["summary"]["filled_sell_count"] == 1
    assert trade["holding_trading_days"] == 4
    assert trade["mae_return"] == pytest.approx(
        7.8 / trade["entry_execution_price"] - 1.0
    )
    assert trade["mfe_return"] == pytest.approx(
        10.5 / trade["entry_execution_price"] - 1.0
    )
