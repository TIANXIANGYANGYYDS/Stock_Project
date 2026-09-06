from app.quant.data.market_data import (
    DAILY_HISTORY_COLLECTION,
    THREE_MINUTE_HISTORY_COLLECTION,
)


def test_official_strategy_uses_only_isolated_daily_and_three_minute_history() -> None:
    assert DAILY_HISTORY_COLLECTION == "stock_history_daily_bars_ths_forward_stage"
    assert THREE_MINUTE_HISTORY_COLLECTION == (
        "stock_history_3m_bars_ths_forward_stage"
    )
    assert "stock_daily_detail" not in {
        DAILY_HISTORY_COLLECTION,
        THREE_MINUTE_HISTORY_COLLECTION,
    }
    assert "stock_realtime_minute_bars" not in {
        DAILY_HISTORY_COLLECTION,
        THREE_MINUTE_HISTORY_COLLECTION,
    }
