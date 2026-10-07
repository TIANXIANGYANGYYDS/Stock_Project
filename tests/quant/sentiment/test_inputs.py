from __future__ import annotations

from dataclasses import replace

import pytest

from app.quant.data.sentiment_inputs import TradingCalendar, stock_features
from tests.quant.sentiment.helpers import day, stock


def test_real_limit_prices_and_ex_right_reference_are_used():
    row = stock(raw_preclose=8.0, raw_open=8.0, raw_high=8.8, raw_low=8.0,
                raw_close=8.8, limit_up=8.8, limit_down=7.2)
    feature = stock_features(row, "2026-07-06")
    assert feature.is_limit_up and feature.is_touch_up
    assert feature.close_premium == .1
    assert feature.limit_streak is None  # 左侧历史未知，不能虚构首板。
    limited = replace(row, raw_preclose=8.0, raw_high=8.4, raw_close=8.4, limit_up=8.4, limit_down=7.6)
    assert stock_features(limited, "2026-07-06").is_limit_up  # 不硬编码日期或百分比。


def test_streak_suspension_missing_and_nonlimit_days():
    zero = stock_features(stock(), "2026-01-05")
    limit = stock(raw_high=11.0, raw_close=11.0)
    first = stock_features(limit, "2026-01-06", zero)
    second = stock_features(limit, "2026-01-07", first)
    assert (first.limit_streak, second.limit_streak) == (1, 2)
    suspended = stock_features(stock(is_suspended=True, raw_close=None), "2026-01-08", second)
    assert suspended.limit_streak == 0
    assert stock_features(limit, "2026-01-09", suspended).limit_streak == 1
    assert stock_features(limit, "2026-01-09", None).limit_streak is None
    unlimited = stock_features(stock(has_price_limit=False, limit_up=None, limit_down=None), "2026-01-08", second)
    assert unlimited.is_limit_up is None and unlimited.limit_streak == 0
    assert unlimited.exclusion_reason == "no_price_limit"


def test_one_word_flags_only_describe_completed_day():
    row = stock(raw_open=11.0, raw_high=11.0, raw_low=11.0, raw_close=11.0)
    assert stock_features(row, "2026-01-05").is_one_word_up
    row = replace(row, raw_open=9.0, raw_high=9.0, raw_low=9.0, raw_close=9.0)
    assert stock_features(row, "2026-01-05").is_one_word_down


@pytest.mark.parametrize("change,reason", [
    ({"price_basis": "qfq"}, "unverified_raw_price_basis"),
    ({"raw_high": 11.01}, "price_outside_limits"),
    ({"raw_close": 10.001}, "price_not_on_tick"),
    ({"raw_low": 10.1}, "invalid_ohlc"),
    ({"raw_preclose": None}, "missing_preclose"),
    ({"amount_yuan": None}, "missing_or_empty_turnover"),
    ({"status_source": "derived.nearest_known_st_state"}, "unverified_status"),
    ({"status_known_at": "2026-01-06T00:00:00+08:00"}, "unverified_status"),
    ({"status_known_at": "2026-01-05T10:00:00"}, "unverified_status"),
    ({"is_st": None}, "unverified_status"),
])
def test_bad_input_is_not_eligible(change, reason):
    feature = stock_features(stock(**change), "2026-01-05")
    assert not feature.eligible
    assert feature.error == reason


def test_bad_prices_do_not_leave_usable_flags():
    feature = stock_features(stock(raw_high=11.01, raw_close=11.0), "2026-01-05")
    assert feature.is_limit_up is None and feature.limit_streak is None


def test_input_contract_requires_verified_calendar_universe_and_unique_rows():
    with pytest.raises(ValueError):
        TradingCalendar(("2026-01-06", "2026-01-05"), "test")
    with pytest.raises(ValueError):
        replace(day("2026-01-05", [stock()]), universe_known_at="2026-01-06T00:00:00+08:00")
    with pytest.raises(ValueError):
        day("2026-01-05", [stock(), stock()])
    with pytest.raises(ValueError):
        stock(is_st="false")
    with pytest.raises(ValueError):
        stock(raw_close=float("nan"))
