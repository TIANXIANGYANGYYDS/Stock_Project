"""离线底稿补录与每日收盘采集；使用独立进程调用 BaoStock。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from app.quant.data.sentiment_inputs import TradingCalendar
from app.quant.data.sentiment_source_snapshot import BaoStockSource, download_plan, load_snapshot, mainboard_candidate
from app.quant.data.sentiment_inputs import observation_stock
from app.repositories.market_sentiment_repository import MarketSentimentRepository
from app.services.market_sentiment_metrics import MarketSentimentCalculator

CN = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[2]
ARCHIVES = ROOT / ".local/market_sentiment/sources"


def completed_sessions(calendar: TradingCalendar, now: datetime) -> list[str]:
    if now.tzinfo is None:
        raise ValueError("必须传入带时区的当前时间")
    local = now.astimezone(CN)
    cutoff = local.date() if (local.hour, local.minute) >= (15, 30) else local.date() - timedelta(days=1)
    if not calendar.trade_dates[0] <= cutoff.isoformat() < calendar.trade_dates[-1]:
        raise ValueError("交易日历未覆盖最近收盘日及其下一交易日")
    return [day for day in calendar.trade_dates if day <= cutoff.isoformat()]


def backfill(repo: MarketSentimentRepository, inputs: Path, calendar_path: Path, *, now=None):
    now = now or datetime.now(CN)
    calendar = TradingCalendar(tuple(json.loads(calendar_path.read_text())["trade_dates"]), str(calendar_path))
    allowed = set(completed_sessions(calendar, now))
    calculator = resume_calculator(repo, calendar)
    last = calculator.latest_date
    dataset_path = inputs.parent / "dataset.json"
    dataset = json.loads(dataset_path.read_text()) if dataset_path.exists() else {}
    provenance = {"kind": "historical_backfill", "provider": "baostock", "label": "历史研究底稿补录",
                  "path": str(inputs.resolve()), "dataset_id": dataset.get("dataset_id"),
                  "data_assumptions": dataset.get("data_assumptions", []),
                  "known_at": None, "historical_known_at_verified": False,
                  "limit_method": "研究底稿限价重建，特殊制度未经完整核验"}
    saved = 0
    with inputs.open() as stream:
        for line in stream:
            market = json.loads(line)["market"]
            day = market["trade_date"]
            if last and day <= last:
                continue
            if day not in allowed:
                raise ValueError(f"拒绝未收盘或非交易日输入：{day}")
            pending = repo.raw_for(day)
            if pending:
                market = pending["market"]
            result = calculator.process(market)
            repo.save(market, pending["provenance"] if pending else provenance, result, calculator.limit_streaks)
            saved += 1
            if saved % 20 == 0:
                print(json.dumps({"saved": saved, "trade_date": day}), flush=True)
    return {"saved": saved, "latest": calculator.latest_date}


def resume_calculator(repo, calendar):
    history = repo.history()
    previous_raw = repo.raw_for(history[-1]["trade_date"]) if history else None
    return MarketSentimentCalculator(calendar, previous_raw, history)


def capture(source, requests, folder):
    download_plan(requests, folder, source.fetch_with_retries, interval=.1)
    return [load_snapshot(folder, request) for request in requests]


def rows(snapshot):
    return [dict(zip(snapshot["fields"], row)) for row in snapshot["rows"]]


def normalize_daily(day, daily_rows, universe_rows, basic_rows, calendar):
    expected = {r["code"]: r for r in universe_rows if mainboard_candidate(r["code"])}
    if len(expected) != sum(mainboard_candidate(r["code"]) for r in universe_rows):
        raise ValueError("供应商当日名单重复")
    if not expected:
        raise ValueError("供应商未返回当日主板名单")
    basics = {r["code"]: r for r in basic_rows}
    observations = {}

    def number(value):
        if value in (None, ""):
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("供应商行情包含非有限数字")
        return parsed

    for row in daily_rows:
        symbol = row["code"]
        if not mainboard_candidate(symbol):
            continue
        if row["date"] != day or row["adjustflag"] != "3":
            raise ValueError("供应商日期或不复权口径不符")
        if symbol in observations or symbol not in expected:
            raise ValueError("供应商行情重复或名单不一致")
        listing = basics.get(symbol, {}).get("ipoDate")
        if not listing or listing > day:
            raise ValueError(f"缺少可靠的上市日期：{symbol}")
        sessions = 6 if listing < calendar.trade_dates[0] else bisect_right(calendar.trade_dates, day) - bisect_left(calendar.trade_dates, listing)
        record = {"trade_date": day, "symbol": symbol, "listed_date_observed": listing,
                  "listing_session_observed": sessions, "limit_provenance": "unavailable",
                  "is_st": {"0": False, "1": True}.get(row.get("isST")),
                  "is_suspended": {"0": True, "1": False}.get(row.get("tradestatus")),
                  "is_delisting": True if "退" in expected[symbol].get("code_name", "") else None,
                  "status_known_at": None,
                  **{f"raw_{key}": number(row.get(key)) for key in ("open", "high", "low", "close", "preclose")},
                  "amount_yuan": number(row.get("amount")), "volume_shares": number(row.get("volume"))}
        stock, _, _ = observation_stock(record)
        observations[symbol] = asdict(stock)
    if len(observations) / len(expected) < .98:
        raise ValueError("供应商当日行情未就绪或缺失超过 2%，稍后重试")
    return {"trade_date": day, "expected_codes": sorted(symbol[3:] for symbol in expected),
            "universe_source": "baostock.query_all_stock.daily_observation", "universe_known_at": None,
            "stocks": [observations[symbol] for symbol in sorted(observations)]}


def sync_daily(repo: MarketSentimentRepository, *, now=None, archive_root=ARCHIVES):
    now = now or datetime.now(CN)
    history = repo.history()
    last = history[-1]["trade_date"] if history else None
    start = last or (now.date() - timedelta(days=75)).isoformat()
    run_folder = archive_root / now.astimezone(CN).strftime("%Y%m%dT%H%M%S%f")
    with BaoStockSource(timeout_seconds=60) as source:
        calendar_request = {"method": "query_trade_dates", "params": {
            "start_date": min("2025-01-01", start), "end_date": (now.date() + timedelta(days=35)).isoformat()}}
        snapshots = capture(source, [calendar_request], run_folder / "calendar")
        calendar = TradingCalendar(tuple(r["calendar_date"] for r in rows(snapshots[0]) if r["is_trading_day"] == "1"),
                                   "baostock.query_trade_dates")
        sessions = completed_sessions(calendar, now)
        due = [day for day in sessions if day > start or not last and day == start]
        if not due:
            return {"saved": 0, "latest": last}
        basics = rows(capture(source, [{"method": "query_stock_basic", "params": {}}], run_folder / "basic")[0])
        calculator = MarketSentimentCalculator(calendar, repo.raw_for(last) if last else None, history)
        for day in due:
            pending = repo.raw_for(day)
            if pending:
                result = calculator.process(pending["market"])
                if result["values"]["coverage"] < .98:
                    raise ValueError("已存依据的有效行情覆盖不足 98%，等待核查")
                repo.save(pending["market"], pending["provenance"], result, calculator.limit_streaks)
                continue
            requests = [{"method": "query_daily_history_k_AStock", "params": {"date": day}},
                        {"method": "query_all_stock", "params": {"day": day}}]
            snapshots = capture(source, requests, run_folder / day)
            market = normalize_daily(day, rows(snapshots[0]), rows(snapshots[1]), basics, calendar)
            # 空响应或截断结果不能推动日历，也不能把尚未就绪的一天永久记成零。
            if len(market["expected_codes"]) < 2000:
                raise ValueError("主板名单异常缩小，保留原始响应等待核查")
            result = calculator.process(market)
            if result["values"]["coverage"] < .98:
                raise ValueError("有效行情覆盖不足 98%，保留原始响应等待补采")
            provenance = {"kind": "daily_observation", "provider": "baostock", "label": "每日收盘采集",
                          "observed_at": snapshots[0]["observed_at"], "known_at": None,
                          "path": str((run_folder / day).resolve()), "historical_known_at_verified": False,
                          "limit_method": "常规主板昨收±10%重建，IPO前五日排除；特殊制度待核验",
                          "data_assumptions": ["ST、停牌为日线观测状态", "退市名称只用于已识别排除，未识别仍保留未知", "日线不能验证封板路径及队列"]}
            repo.save(market, provenance, result, calculator.limit_streaks)
            print(json.dumps({"saved_trade_date": day, "coverage": result["values"]["coverage"]}), flush=True)
        return {"saved": len(due), "latest": due[-1]}
