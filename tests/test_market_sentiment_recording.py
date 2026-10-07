from datetime import datetime
import json

import pytest
from app.quant.data.sentiment_inputs import TradingCalendar
from app.scheduler.sentiment_jobs import JOB_ID, register_sentiment_job
from app.services.market_sentiment_metrics import MarketSentimentCalculator, VERSION
from app.services.market_sentiment_service import CN, completed_sessions, normalize_daily

CALENDAR = TradingCalendar(("2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"), "test")


def stock(code="000001", close=10, high=None, low=None, **overrides):
    return dict(code=code, price_basis="raw", source="baostock", status_source="baostock.daily_observation",
                status_known_at=None, is_st=False, is_suspended=False, is_delisting=None,
                has_price_limit=True, raw_preclose=10, raw_open=10, raw_high=high or max(close, 10),
                raw_low=low or min(close, 10), raw_close=close, limit_up=11, limit_down=9,
                amount_yuan=10000000, volume_shares=1000000) | overrides


def market(day, stocks, expected=None):
    return dict(trade_date=day, expected_codes=expected or [s["code"] for s in stocks], stocks=stocks)


def test_missing_streak_never_becomes_first_board_and_daily_records_resume():
    calc = MarketSentimentCalculator(CALENDAR)
    first = calc.process(market("2026-09-04", [stock(close=11)]))
    assert first["values"]["limit_up"] == 1
    assert first["values"]["board_1"] == 0
    assert first["values"]["unknown_streak"] == 1
    assert first["values"]["height"] is None
    previous_market = market("2026-09-07", [stock()])
    calc.process(previous_market)
    previous_raw = dict(version=VERSION, trade_date="2026-09-07", market=previous_market,
                        limit_streaks=calc.limit_streaks)
    resumed = MarketSentimentCalculator(CALENDAR, json.loads(json.dumps(previous_raw)), list(calc.history))
    next_day = market("2026-09-08", [stock(close=11)])
    result = calc.process(next_day)
    assert resumed.process(next_day) == result
    assert result["values"]["board_1"] == 1
    assert "signal_usable" not in result and "model" not in result
    assert "emotion" not in result["values"] and "phase" not in result["values"]


def test_previous_cohort_is_frozen_and_missing_members_do_not_improve_returns():
    calc = MarketSentimentCalculator(CALENDAR)
    calc.process(market("2026-09-04", [stock("000001"), stock("000002")]))
    calc.process(market("2026-09-07", [stock("000001", 11), stock("000002", 11)]))
    result = calc.process(market("2026-09-08", [stock("000001", 11), stock("000002", 9, is_st=True)]))
    assert result["values"]["promotion"] == .5
    assert result["values"]["one_to_two"] == .5
    assert result["values"]["yesterday_up_return"] == 0
    assert result["values"]["eligible"] == 1
    # 缺昨日赢家会使分母不完整；不能只拿今日仍然有数据的赢家计算。
    calc2 = MarketSentimentCalculator(CALENDAR)
    calc2.process(market("2026-09-04", [stock("000001"), stock("000002")]))
    calc2.process(market("2026-09-07", [stock("000001", 11), stock("000002", 11)]))
    result2 = calc2.process(market("2026-09-08", [stock("000001", 11)], ["000001", "000002"]))
    assert result2["values"]["promotion"] is None
    assert result2["values"]["limit_up"] is None
    assert result2["values"]["coverage"] == .5


def test_boundaries_exclude_limits_and_empty_denominator_is_missing():
    calc = MarketSentimentCalculator(CALENDAR)
    rows = [stock("000001", 11), stock("000002", 10.5), stock("000003", 10.51),
            stock("000004", 9), stock("000005", 9.5), stock("000006", 9.49)]
    v = calc.process(market("2026-09-04", rows))["values"]
    assert (v["up_over_5"], v["down_over_5"]) == (1, 1)
    assert (v["advancing"], v["declining"], v["flat"]) == (3, 3, 0)
    assert v["seal_rate"] == 1
    v = calc.process(market("2026-09-07", [stock()]))["values"]
    assert v["limit_up"] == 0
    assert v["seal_rate"] is None


def test_price_and_suspension_conflicts_are_not_good_data():
    calc = MarketSentimentCalculator(CALENDAR)
    result = calc.process(market("2026-09-04", [stock(is_suspended=True)]))
    assert result["values"]["coverage"] == 0
    assert result["quality"]["price_errors"] == {"priced_suspension_conflict": 1}
    with pytest.raises(ValueError, match="逐交易日"):
        calc.process(market("2026-09-08", [stock()]))


def test_calendar_skips_weekends_and_unfinished_today():
    assert completed_sessions(CALENDAR, datetime(2026, 9, 7, 10, tzinfo=CN)) == ["2026-09-04"]
    assert completed_sessions(CALENDAR, datetime(2026, 9, 7, 16, tzinfo=CN))[-1] == "2026-09-07"
    assert completed_sessions(CALENDAR, datetime(2026, 9, 6, 18, tzinfo=CN)) == ["2026-09-04"]
    with pytest.raises(ValueError, match="时区"):
        completed_sessions(CALENDAR, datetime(2026, 9, 7, 10))


def provider_row(**overrides):
    return dict(code="sz.000001", date="2026-09-08", adjustflag="3", isST="0", tradestatus="1",
                preclose="10", open="10", high="11", low="10", close="11", amount="10000000", volume="1000000") | overrides


def test_provider_requires_raw_prices_and_respects_ipo_sessions():
    universe = [{"code": "sz.000001", "code_name": "测试"}]
    basic = [{"code": "sz.000001", "ipoDate": "2026-09-04"}]
    market = normalize_daily("2026-09-08", [provider_row()], universe, basic, CALENDAR)
    assert market["stocks"][0]["has_price_limit"] is False
    assert market["stocks"][0]["status_known_at"] is None
    with pytest.raises(ValueError, match="不复权"):
        normalize_daily("2026-09-08", [provider_row(adjustflag="2")], universe, basic, CALENDAR)
    with pytest.raises(ValueError, match="未就绪"):
        normalize_daily("2026-09-08", [], universe, basic, CALENDAR)
    with pytest.raises(ValueError, match="重复"):
        normalize_daily("2026-09-08", [provider_row(), provider_row()], universe, basic, CALENDAR)


def test_schedule_uses_shanghai_close_hours_and_stable_job_id():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    register_sentiment_job(scheduler)
    job = scheduler.get_job(JOB_ID)
    next_time = job.trigger.get_next_fire_time(None, datetime(2026, 9, 7, 10, tzinfo=CN))
    assert next_time == datetime(2026, 9, 7, 18, 30, tzinfo=CN)
    assert job.max_instances == 1


def test_daily_recording_does_not_import_offline_trading_or_connect_on_import():
    import subprocess
    import sys
    from pathlib import Path

    script = '''
import socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError("import attempted a network connection")
socket.socket.connect = forbidden
from app.services.market_sentiment_service import normalize_daily
assert callable(normalize_daily)
for name in (
    "app.quant.strategies.sentiment_cycle_daily.emotion",
    "app.quant.strategies.sentiment_cycle_daily.state",
    "app.quant.strategies.sentiment_cycle_daily.strategy",
    "app.quant.strategies.sentiment_cycle_daily.roles",
    "app.quant.runtime.sentiment_daily",
    "app.quant.research.sentiment_cycle.backtest",
):
    assert name not in sys.modules, name
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_resume_keeps_unknown_streak_and_rejects_repeated_dates():
    calc = MarketSentimentCalculator(CALENDAR)
    previous_market = market("2026-09-04", [stock(close=11)])
    daily = calc.process(previous_market)
    previous_raw = dict(version=VERSION, trade_date=daily["trade_date"], market=previous_market,
                        limit_streaks=calc.limit_streaks)
    resumed = MarketSentimentCalculator(CALENDAR, previous_raw, [daily])
    assert resumed.process(market("2026-09-07", [stock(close=11)]))["values"]["unknown_streak"] == 1
    # 同一日重算和跳日都不能推进窗口。
    with pytest.raises(ValueError, match="逐交易日"):
        resumed.process(market("2026-09-07", [stock()]))
    assert resumed.latest_date == "2026-09-07"


def test_twenty_day_amount_window_resumes_without_model_checkpoint():
    from datetime import timedelta
    dates = tuple((datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat() for i in range(27))
    calendar = TradingCalendar(dates, "synthetic_test_calendar")
    calc = MarketSentimentCalculator(calendar)
    for day in dates[:24]:
        previous_market = market(day, [stock()])
        calc.process(previous_market)
    previous_raw = dict(version=VERSION, trade_date=dates[23], market=previous_market,
                        limit_streaks=calc.limit_streaks)
    resumed = MarketSentimentCalculator(calendar, previous_raw, list(calc.history))
    today = market(dates[24], [stock(amount_yuan=20000000)])
    assert resumed.process(today) == calc.process(today)
    assert resumed.previous["values"]["amount_ratio"] == 2
    assert len(resumed.history) == 20


def test_recording_lock_excludes_other_process_and_releases():
    import subprocess
    import sys
    from app.quant.cli.record_market_sentiment import recording_lock
    script = 'from app.quant.cli.record_market_sentiment import recording_lock\nwith recording_lock(): pass'
    with recording_lock():
        blocked = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        assert blocked.returncode != 0 and "已有情绪记录任务" in blocked.stderr
    released = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert released.returncode == 0, released.stderr
