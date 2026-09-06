from __future__ import annotations

import asyncio
import math

import pytest

from app.quant.runtime.daily_flow import (
    HoldingItem,
    PreselectionItem,
    SellCandidateItem,
    apply_trade_signal,
    close_daily_flow,
    create_daily_flow,
    daily_flow_document,
    daily_price_limit,
    mark_holdings,
)
from app.repositories.quant_daily_result_repository import QuantDailyResultRepository
from app.quant.strategies.provisional_daily_macd_3m import STRATEGY_ID


def candidate(code: str = "600176") -> PreselectionItem:
    return PreselectionItem(
        code=code,
        name="中国巨石",
        reason="MACD 绿柱谷底确认",
        reference_price=10.0,
    )


def sell_candidate(code: str = "600176") -> SellCandidateItem:
    return SellCandidateItem(
        code=code,
        name="中国巨石",
        reason="日线 MACD 红柱峰顶确认",
        reference_price=11.0,
    )


def test_complete_daily_flow_moves_filled_signals_between_pools() -> None:
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[candidate()],
        sell_candidates=[sell_candidate()],
    )

    flow = apply_trade_signal(
        flow,
        action="buy",
        code="600176",
        signal_at="2026-08-06T09:45:00+08:00",
        signal_price=10.0,
        previous_close=10.0,
        execution_at="2026-08-06T09:48:00+08:00",
        execution_reference_price=10.1,
        execution_bar_low=10.05,
        execution_bar_high=10.103,
        execution_price_source="next_3m_bar_open",
        reason="分钟级买点确认",
    )
    buy = flow.executions[0]
    assert buy.signal_at == "2026-08-06T09:45:00+08:00"
    assert buy.execution_at == "2026-08-06T09:48:00+08:00"
    assert buy.execution_reference_price == 10.1
    assert buy.execution_price_source == "next_3m_bar_open"
    assert math.isclose(buy.execution_price, 10.103)
    assert buy.shares % 100 == 0
    assert flow.preselection[0].status == "bought"
    assert flow.holdings[0].entry_event_id == buy.event_id
    assert flow.holdings[0].entry_execution_at == buy.execution_at

    flow = mark_holdings(
        flow,
        prices={"600176": 10.8},
        marked_at="2026-08-06T13:30:00+08:00",
    )
    live_document = daily_flow_document(flow)
    assert live_document["status"] == "monitoring"
    assert live_document["holding_pool"]["count"] == 1
    assert live_document["holding_pool"]["items"][0]["mark_price"] == 10.8
    assert live_document["sell_candidate_pool"]["count"] == 1

    with pytest.raises(ValueError, match=r"T\+1"):
        apply_trade_signal(
            flow,
            action="sell",
            code="600176",
            signal_at="2026-08-06T14:20:00+08:00",
            signal_price=11.0,
            previous_close=10.0,
            reason="分钟级卖点确认",
        )
    flow = close_daily_flow(
        flow,
        closed_at="2026-08-06T15:00:00+08:00",
    )
    result = daily_flow_document(flow)

    assert result["status"] == "closed"
    assert result["schema_version"] == "1.5"
    assert result["strategy"]["id"] == STRATEGY_ID
    assert result["strategy"]["version"] == "2.0.0"
    assert result["strategy"]["intraday_interval"] == "3m"
    assert result["strategy"]["minimum_shrink_ratio"] == 0.01
    assert result["strategy"]["confirmation_bars"] == 3
    assert result["intraday_trading"]["interval"] == "3m"
    assert result["holding_pool"]["items"][0]["t1_locked"] is True
    assert result["holding_pool"]["items"][0]["sellable_today"] is False
    holding_result = result["holding_pool"]["items"][0]
    assert holding_result["gross_total_pnl"] == pytest.approx(
        (10.8 - buy.execution_price) * buy.shares,
        abs=0.01,
    )
    assert holding_result["total_pnl"] == holding_result["unrealized_pnl"]
    assert holding_result["total_return"] == pytest.approx(
        holding_result["total_pnl"]
        / (holding_result["entry_notional"] + holding_result["buy_commission"])
    )
    assert holding_result["market_day_pnl"] == pytest.approx(
        (10.8 - 10.0) * buy.shares,
        abs=0.01,
    )
    assert holding_result["account_day_pnl"] == holding_result["unrealized_pnl"]
    expected_summary = {
        "preselection_count": 1,
        "watching_count": 0,
        "not_triggered_count": 0,
        "sell_candidate_count": 1,
        "buy_count": 1,
        "sell_count": 0,
        "holding_count": 1,
        "t1_locked_holding_count": 1,
        "closed_trade_count": 0,
        "realized_pnl": 0,
        "gross_unrealized_pnl": holding_result["gross_total_pnl"],
        "unrealized_pnl": holding_result["unrealized_pnl"],
        "holding_market_day_pnl": holding_result["market_day_pnl"],
        "open_position_account_day_pnl": holding_result["account_day_pnl"],
        "closed_position_account_day_pnl": 0,
        "account_day_pnl": holding_result["account_day_pnl"],
        "total_pnl": holding_result["unrealized_pnl"],
    }
    assert {
        key: result["summary"][key] for key in expected_summary
    } == expected_summary
    assert result["summary"]["realized_return"] == 0
    assert result["summary"]["gross_unrealized_return"] == pytest.approx(
        holding_result["gross_total_return"]
    )
    assert result["summary"]["unrealized_return"] == pytest.approx(
        holding_result["unrealized_return"]
    )
    assert result["summary"]["holding_market_day_return"] == pytest.approx(
        holding_result["market_day_return"]
    )
    assert result["summary"][
        "open_position_account_day_return"
    ] == pytest.approx(holding_result["account_day_return"])
    assert result["summary"]["closed_position_account_day_return"] == 0
    assert result["summary"]["account_day_return"] == pytest.approx(
        holding_result["account_day_return"]
    )
    assert result["summary"]["total_return"] == pytest.approx(
        holding_result["total_return"]
    )


def test_carried_holding_can_be_sold_when_daily_sell_candidate_is_present() -> None:
    holding = HoldingItem(
        code="000001",
        name="平安银行",
        shares=9000,
        entry_event_id="2026-08-05-0001",
        entry_signal_at="2026-08-05T10:00:00+08:00",
        entry_signal_price=10.0,
        entry_execution_at="2026-08-05T10:01:00+08:00",
        entry_reference_price=10.01,
        entry_execution_price=10.02,
        entry_notional=90180.0,
        buy_commission=9.02,
        marked_at="2026-08-05T15:00:00+08:00",
        mark_price=10.1,
    )
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[],
        sell_candidates=[
            SellCandidateItem(
                code="000001",
                name="平安银行",
                reason="日线 MACD 红柱峰顶确认",
                reference_price=10.4,
            )
        ],
        holdings=[holding],
    )

    result = apply_trade_signal(
        flow,
        action="sell",
        code="000001",
        signal_at="2026-08-06T10:30:00+08:00",
        signal_price=10.5,
        previous_close=10.0,
        reason="红柱峰顶确认",
    )

    assert result.holdings == ()
    assert result.closed_trades[0].entry_event_id == "2026-08-05-0001"


def test_carried_holding_separates_total_and_current_day_pnl() -> None:
    holding = HoldingItem(
        code="000001",
        name="平安银行",
        shares=1000,
        entry_event_id="2026-08-01-0001",
        entry_signal_at="2026-08-01T10:00:00+08:00",
        entry_signal_price=9.9,
        entry_execution_at="2026-08-01T10:03:00+08:00",
        entry_reference_price=10.0,
        entry_execution_price=10.0,
        entry_notional=10000.0,
        buy_commission=1.0,
        marked_at="2026-08-05T15:00:00+08:00",
        mark_price=10.5,
        previous_close=10.5,
    )
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-06T09:20:00+08:00",
        candidates=[],
        holdings=[holding],
    )
    flow = mark_holdings(
        flow,
        prices={"000001": 10.8},
        previous_closes={"000001": 10.5},
        marked_at="2026-08-06T10:01:00+08:00",
    )

    result = daily_flow_document(flow)
    item = result["holding_pool"]["items"][0]

    assert item["total_pnl"] == 799.0
    assert item["total_return"] == pytest.approx(799.0 / 10001.0)
    assert item["market_day_pnl"] == 300.0
    assert item["market_day_return"] == pytest.approx(10.8 / 10.5 - 1)
    assert item["account_day_pnl"] == 300.0
    assert item["account_day_return"] == pytest.approx(300.0 / 10500.0)
    assert result["summary"]["unrealized_pnl"] == 799.0
    assert result["summary"]["unrealized_return"] == pytest.approx(
        799.0 / 10001.0
    )
    assert result["summary"]["account_day_pnl"] == 300.0
    assert result["summary"]["account_day_return"] == pytest.approx(
        300.0 / 10500.0
    )


def test_intraday_sell_is_rejected_without_daily_sell_candidate() -> None:
    holding = HoldingItem(
        code="000001",
        name="平安银行",
        shares=9000,
        entry_event_id="2026-08-05-0001",
        entry_signal_at="2026-08-05T10:00:00+08:00",
        entry_signal_price=10.0,
        entry_execution_at="2026-08-05T10:01:00+08:00",
        entry_reference_price=10.01,
        entry_execution_price=10.02,
        entry_notional=90180.0,
        buy_commission=9.02,
        marked_at="2026-08-05T15:00:00+08:00",
        mark_price=10.1,
    )
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[],
        holdings=[holding],
    )

    with pytest.raises(ValueError, match="不在当日日线卖出候选池"):
        apply_trade_signal(
            flow,
            action="sell",
            code="000001",
            signal_at="2026-08-06T10:30:00+08:00",
            signal_price=10.5,
            previous_close=10.0,
            reason="分钟 MACD 红柱峰顶确认",
        )


def test_previous_close_signal_executes_on_current_day_and_t1_uses_fill_day() -> None:
    """15:00 信号应在下一交易日撮合，T+1 从实际买入日开始计算。"""

    flow = create_daily_flow(
        trade_date="2026-08-04",
        selection_date="2026-08-03",
        generated_at="2026-08-03T15:30:00+08:00",
        candidates=[candidate()],
        sell_candidates=[sell_candidate()],
    )
    flow = apply_trade_signal(
        flow,
        action="buy",
        code="600176",
        signal_at="2026-08-03T15:00:00+08:00",
        signal_price=10.0,
        previous_close=10.0,
        execution_at="2026-08-04T10:00:00+08:00",
        execution_reference_price=10.2,
        execution_bar_low=10.18,
        execution_bar_high=10.21,
        execution_price_source="next_3m_bar_open",
        reason="分钟级买点确认",
    )

    assert flow.executions[0].execution_price == pytest.approx(10.2051)
    assert flow.holdings[0].entry_signal_at.startswith("2026-08-03")
    assert flow.holdings[0].entry_execution_at.startswith("2026-08-04")
    with pytest.raises(ValueError, match=r"T\+1"):
        apply_trade_signal(
            flow,
            action="sell",
            code="600176",
            signal_at="2026-08-04T10:30:00+08:00",
            signal_price=10.4,
            previous_close=10.2,
            execution_at="2026-08-04T11:00:00+08:00",
            execution_reference_price=10.38,
            reason="分钟级卖点确认",
        )


def test_invalid_pool_transitions_are_rejected() -> None:
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[candidate()],
    )

    with pytest.raises(ValueError, match="不在当日预选池"):
        apply_trade_signal(
            flow,
            action="buy",
            code="000001",
            signal_at="2026-08-06T09:45:00+08:00",
            signal_price=10.0,
            previous_close=10.0,
            reason="测试",
        )
    with pytest.raises(ValueError, match="不在持有池"):
        apply_trade_signal(
            flow,
            action="sell",
            code="600176",
            signal_at="2026-08-06T09:45:00+08:00",
            signal_price=10.0,
            previous_close=10.0,
            reason="测试",
        )


def test_price_limits_block_buy_and_sell_execution() -> None:
    assert daily_price_limit(
        action="buy",
        code="002354",
        name="天娱数科",
        trade_date="2026-08-03",
        previous_close=6.51,
    ) == 7.16
    assert daily_price_limit(
        action="buy",
        code="300001",
        name="特锐德",
        trade_date="2026-08-03",
        previous_close=10.0,
    ) == 12.0
    assert daily_price_limit(
        action="buy",
        code="600001",
        name="*ST示例",
        trade_date="2026-07-03",
        previous_close=10.0,
    ) == 10.5
    assert daily_price_limit(
        action="buy",
        code="600001",
        name="*ST示例",
        trade_date="2026-07-06",
        previous_close=10.0,
    ) == 11.0

    buy_flow = create_daily_flow(
        trade_date="2026-08-03",
        selection_date="2026-07-31",
        generated_at="2026-07-31T15:30:00+08:00",
        candidates=[
            PreselectionItem(
                code="002354",
                name="天娱数科",
                reason="日线买入候选",
                reference_price=6.51,
            )
        ],
    )
    with pytest.raises(ValueError, match="涨停价.*不能买入"):
        apply_trade_signal(
            buy_flow,
            action="buy",
            code="002354",
            signal_at="2026-08-03T14:15:00+08:00",
            signal_price=7.16,
            previous_close=6.51,
            execution_at="2026-08-03T14:18:00+08:00",
            execution_reference_price=7.16,
            execution_bar_low=7.16,
            execution_bar_high=7.16,
            reason="分钟买点",
        )

    holding = HoldingItem(
        code="000001",
        name="平安银行",
        shares=1000,
        entry_event_id="2026-08-03-0001",
        entry_signal_at="2026-08-03T10:00:00+08:00",
        entry_signal_price=10.0,
        entry_execution_at="2026-08-03T10:03:00+08:00",
        entry_reference_price=10.0,
        entry_execution_price=10.005,
        entry_notional=10005.0,
        buy_commission=1.0,
        marked_at="2026-08-03T15:00:00+08:00",
        mark_price=10.0,
    )
    sell_flow = create_daily_flow(
        trade_date="2026-08-04",
        selection_date="2026-08-03",
        generated_at="2026-08-03T15:30:00+08:00",
        candidates=[],
        sell_candidates=[
            SellCandidateItem(
                code="000001",
                name="平安银行",
                reason="日线卖出候选",
                reference_price=10.0,
            )
        ],
        holdings=[holding],
    )
    with pytest.raises(ValueError, match="跌停价.*不能卖出"):
        apply_trade_signal(
            sell_flow,
            action="sell",
            code="000001",
            signal_at="2026-08-04T10:00:00+08:00",
            signal_price=9.0,
            previous_close=10.0,
            execution_at="2026-08-04T10:03:00+08:00",
            execution_reference_price=9.0,
            execution_bar_low=9.0,
            execution_bar_high=9.0,
            reason="分钟卖点",
        )


def test_close_marks_untriggered_candidates_and_completes_timeline() -> None:
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[candidate()],
    )

    result = daily_flow_document(
        close_daily_flow(flow, closed_at="2026-08-06T15:00:00+08:00")
    )

    assert result["preselection_pool"]["items"][0]["status"] == "not_triggered"
    assert result["summary"]["not_triggered_count"] == 1
    assert [stage["status"] for stage in result["timeline"]] == [
        "completed",
        "completed",
        "completed",
    ]


def test_repository_saves_one_snapshot_per_strategy_and_trade_date() -> None:
    class Collection:
        def __init__(self) -> None:
            self.saved = None

        async def replace_one(self, filters, document, upsert):
            self.saved = (filters, document, upsert)

    class Database:
        def __init__(self) -> None:
            self.collection = Collection()

        def __getitem__(self, name):
            assert name == "quant_daily_results"
            return self.collection

    database = Database()
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[candidate()],
    )

    saved = asyncio.run(QuantDailyResultRepository(database).save(flow))

    assert database.collection.saved == (
        {
            "strategy_id": STRATEGY_ID,
            "trade_date": "2026-08-06",
        },
        saved,
        True,
    )
