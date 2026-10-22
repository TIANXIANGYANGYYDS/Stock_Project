"""All-A close facts from retained raw SH/SZ daily rows and BSE close quotes.

The legacy mainboard research input is left intact. Adjusted daily prices are
never used to infer a limit price. Days without BSE close evidence remain absent.
"""
from bisect import bisect_left, bisect_right
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from app.quant.data.sentiment_source_snapshot import load_snapshot
from app.repositories.realtime_snapshot_repository import SNAPSHOT_COLLECTION, decode_batch
from app.services.short_sentiment.facts import CN, cents, instant, number, quote_fields, stock_fact
from app.services.short_sentiment.metrics import MIN_COVERAGE
from app.services.short_sentiment.themes import ThemeCatalog
from app.services.short_sentiment.universe import BOARDS, board_type
from app.services.trading_calendar_service import get_a_share_calendar

DAILY_SOURCES = "short_sentiment_daily_sources"
SOURCE_VERSION = "all-a-daily-v1"

def snapshot_rows(snapshot):
    return [dict(zip(snapshot["fields"], row)) for row in snapshot["rows"]]


def expanded_daily_quotes(day, daily_rows, universe_rows, basic_rows, calendar):
    """Normalize retained ChiNext/STAR raw daily observations, with IPO exclusions."""
    expected = {r["code"]: r for r in universe_rows
                if board_type(r["code"][3:]) in {"chinext", "star"}}
    if not expected:
        raise ValueError("缺少创业板、科创板每日名单")
    basics = {r["code"]: r for r in basic_rows}
    quotes = {}
    for row in daily_rows:
        symbol = row["code"]
        if symbol not in expected:
            continue
        if row["date"] != day or row["adjustflag"] != "3" or symbol in quotes:
            raise ValueError("扩展板块日线日期、原始价口径或唯一性不符")
        listed = basics.get(symbol, {}).get("ipoDate")
        if not listed or listed > day:
            raise ValueError("缺少扩展板块上市日期")
        sessions = 6 if listed < calendar[0] else bisect_right(calendar, day) - bisect_left(calendar, listed)
        limited = sessions > 5
        ref = number(row.get("preclose"))
        upper = lower = None
        if limited and ref and ref > 0:
            upper = float((Decimal(str(ref)) * Decimal("1.2")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
            lower = float((Decimal(str(ref)) * Decimal(".8")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
        q = {"code": symbol[3:], "name": expected[symbol]["code_name"],
             "price": number(row.get("close")), "previous_close": ref,
             "open_price": number(row.get("open")), "high_price": number(row.get("high")),
             "low_price": number(row.get("low")), "amount": number(row.get("amount")),
             "volume": number(row.get("volume")), "turnover_pct": number(row.get("turn")),
             "is_st": {"0": False, "1": True}.get(row.get("isST")),
             "is_suspended": {"0": True, "1": False}.get(row.get("tradestatus")),
             "has_price_limit": limited, "limit_up_price": upper, "limit_down_price": lower,
             "limit_rule": "ordinary_20_percent_raw_preclose" if limited else "ipo_first_five_sessions",
             "provider": "baostock.daily_observation", "market_data_time": day + "T15:00:00+08:00",
             "phase": "session_close", "quality_issue": None}
        if q["is_st"] is None or q["is_suspended"] is None:
            q["quality_issue"] = "unverified_security_status"
        validate_daily_quote(q)
        quotes[symbol] = q
    return sorted(s[3:] for s in expected), list(quotes.values())


def validate_daily_quote(q):
    if q.get("is_suspended"):
        if (q.get("amount") or 0) > 0 or (q.get("volume") or 0) > 0:
            q["is_suspended"] = None
            q["quality_issue"] = "priced_suspension_conflict"
        return
    o, h, l, c = [cents(q.get(key)) for key in ("open_price", "high_price", "low_price", "price")]
    if None in (o, h, l, c) or l > min(o, c) or h < max(o, c) or l > h:
        q["quality_issue"] = "invalid_ohlc"
    elif (q.get("amount") or 0) <= 0 or (q.get("volume") or 0) <= 0:
        q["quality_issue"] = "missing_or_empty_turnover"
    elif q.get("has_price_limit") is not False:
        upper, lower = cents(q.get("limit_up_price")), cents(q.get("limit_down_price"))
        if upper and lower and (h > upper or l < lower):
            q["quality_issue"] = "price_outside_limits"


def build_daily_source(main_raw, daily_rows, universe_rows, basic_rows, close_batch, calendar):
    from app.services.short_sentiment.engine import daily_envelope
    day = main_raw["trade_date"]
    close_at = day + "T15:00:00+08:00"
    expanded_codes, expanded_quotes = expanded_daily_quotes(day, daily_rows, universe_rows, basic_rows, calendar)
    base = daily_envelope(main_raw)
    latest = {}
    for quote in close_batch["quotes"]:
        old = latest.get(quote["code"])
        key = (not bool(quote.get("quality_issue")), quote.get("market_data_time") or "")
        if old is None or key > (not bool(old.get("quality_issue")), old.get("market_data_time") or ""):
            latest[quote["code"]] = quote
    bse_codes = sorted(c for c in close_batch["expected_codes"] if board_type(c) == "bse")
    if not bse_codes or not any((q.get("market_data_time") or "") >= close_at for c, q in latest.items() if c in bse_codes):
        raise ValueError("缺少北交所当日收盘报价")
    quotes = base["quotes"] + expanded_quotes
    for q in quotes:
        if q["code"] in latest:
            q["name"] = latest[q["code"]].get("name") or q["name"]
    for code in bse_codes:
        original = latest.get(code)
        if not original:
            continue  # Remains in expected_codes and therefore in missing coverage.
        q = quote_fields(original)
        source = instant(q.get("market_data_time"))
        if source is None or source.astimezone(CN).date().isoformat() != day or source < instant(close_at):
            q["quality_issue"] = "missing_bse_close_quote"
        # N marks an IPO's first session; do not interpret its provider sentinel
        # limit values as a 30% board. Historical 43/83/87 aliases are excluded.
        if (q.get("name") or "").startswith("N"):
            q["has_price_limit"] = False
        q["close_source_time"] = q.get("market_data_time")
        q["market_data_time"] = close_at
        q["phase"] = "session_close"
        q["limit_rule"] = "provider_daily_limits"
        validate_daily_quote(q)
        quotes.append(q)
    expected = sorted(set(base["expected_codes"]) | set(expanded_codes) | set(bse_codes))
    payload = {"quotes": quotes, "expected_codes": expected, "observed_at": close_at,
               "universe_basis": "sh_sz_daily_universe_and_bse_quote_requested"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    payload.update(batch_id="all_a_daily:" + digest, input_hash=digest)
    by_code = {q["code"]: q for q in quotes}
    catalog = ThemeCatalog(groups={})
    features = {}
    for code in expected:
        fact, _ = stock_fact(code, by_code.get(code), None, None, [], catalog, close_at, final=True)
        features[code] = {"is_limit_up": fact["is_limit_up"], "is_touch_up": fact["touched_up"],
                          "eligible": fact["eligible"], "streak": fact["intraday_streak"],
                          "excluded_reason": fact["excluded_reason"]}
    return {"scope": "all_a", "source_version": SOURCE_VERSION, "trade_date": day, "input_hash": digest, "envelope": payload,
            "features": features, "provenance": {"main_raw_hash": main_raw["input_hash"],
                "bse_batch_id": close_batch["batch_id"], "price_basis": "raw",
                "limit_method": "mainboard_original_facts_chinext_star_20_percent_bse_provider_limits"}}


class AllADailySource:
    def __init__(self, db):
        self.db = db

    async def get(self, day):
        stored = await self.db[DAILY_SOURCES].find_one({"_id": day})
        if stored and stored.get("source_version") == SOURCE_VERSION:
            return stored
        done = await self.db["market_sentiment_daily"].find_one({"scope": "hs_mainboard", "trade_date": day})
        raw = await self.db["market_sentiment_raw_daily"].find_one({"scope": "hs_mainboard", "trade_date": day}) if done else None
        if not raw or not raw.get("provenance", {}).get("path"):
            return None
        document = await self.db[SNAPSHOT_COLLECTION].find_one({"trade_date": day}, sort=[("observed_at", -1)])
        if document is None or instant(document["observed_at"]).astimezone(CN).strftime("%H:%M:%S") < "15:00:00":
            return None
        # File verification and decompression stay off the scheduler event loop.
        import asyncio
        def build():
            folder = Path(raw["provenance"]["path"])
            daily = snapshot_rows(load_snapshot(folder, {"method": "query_daily_history_k_AStock", "params": {"date": day}}))
            universe = snapshot_rows(load_snapshot(folder, {"method": "query_all_stock", "params": {"day": day}}))
            basics = snapshot_rows(load_snapshot(folder.parent / "basic", {"method": "query_stock_basic", "params": {}}))
            calendar = get_a_share_calendar()
            sessions = [d.strftime("%Y-%m-%d") for d in calendar.sessions_in_range("2020-01-01", day)]
            return build_daily_source(raw, daily, universe, basics, decode_batch(document), sessions)
        try:
            result = await asyncio.to_thread(build)
        except FileNotFoundError:
            return None
        # Do not cache an incomplete source forever: a later daily retry or
        # repaired quote archive must still be able to complete this date.
        for board in BOARDS:
            expected = [f for code, f in result["features"].items()
                        if board_type(code) == board and f["excluded_reason"] is None]
            if not expected or sum(f["eligible"] for f in expected) / len(expected) < MIN_COVERAGE:
                raise ValueError(f"{day} {board} 收盘依据覆盖不足 98%，等待补齐")
        await self.db[DAILY_SOURCES].update_one({"_id": day}, {"$set": result}, upsert=True)
        return result
