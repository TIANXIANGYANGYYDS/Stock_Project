"""只计算可观测事实。价格按分比较，缺行情不会生成开板/回封。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import math

from app.services.short_sentiment.universe import board_type

CN = timezone(timedelta(hours=8))
MAX_GAP_SECONDS = 15
STALE_SECONDS = 30
HISTORY_DAYS = 20


def instant(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def cents(value):
    try:
        n = Decimal(str(value)) * 100
        return int(n) if n.is_finite() and n > 0 and n == n.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def quote_fields(q):
    """Decode retained Tencent fields, including already archived v1/v2 batches.

    47/48 are the provider's daily upper/lower price, never a rounded return.
    Sina has no equivalent limits: leave them unknown rather than reconstruct.
    """
    raw = q.get("raw_fields") or []
    result = dict(q)
    if str(q.get("provider", "")).lower() == "tencent":
        for name, index in (("limit_up_price", 47), ("limit_down_price", 48),
                            ("high_price", 33), ("low_price", 34), ("turnover_pct", 38)):
            if result.get(name) is None and len(raw) > index:
                result[name] = number(raw[index])
    return result


def daily_history(raw_days, sessions):
    """Preserve missing sessions; only use completed dates preceding the snapshot."""
    from app.services.market_sentiment_metrics import observed_features
    by_date = {day["trade_date"]: day for day in raw_days}
    histories = {}
    previous = {}
    previous_expected = []
    prior_features = {}
    for day in sessions:
        raw = by_date.get(day)
        features = {}
        if raw and "envelope" in raw:
            for code, row in raw["features"].items():
                height = row.get("streak")
                if row["is_limit_up"] and height is None and code in prior_features:
                    last = prior_features[code]
                    height = last.get("streak") + 1 if last.get("streak") is not None else None
                feature = {**row, "streak": height if row["is_limit_up"] else 0 if row["is_limit_up"] is False else None}
                features[code] = feature
        elif raw:
            _, observed = observed_features(raw["market"])
            features = {code: {"is_limit_up": f.is_limit_up, "is_touch_up": f.is_touch_up,
                               "eligible": f.eligible, "streak": raw["limit_streaks"].get(code) if f.is_limit_up else 0 if f.is_limit_up is False else None}
                        for code, f in observed.items()}
        for code in set(histories) | set(features):
            feature = features.get(code)
            history = histories.setdefault(code, [{"trade_date": d, "is_limit_up": None} for d in sessions[:sessions.index(day)]])
            history.append({"trade_date": day, "is_limit_up": feature["is_limit_up"] if feature else None})
        prior_features = features
        if day == sessions[-1] and raw:
            previous_expected = raw.get("envelope", raw.get("market"))["expected_codes"]
            previous = features
    return histories, previous, previous_expected


def board_run(history, today_up, *, final=False):
    """最多20个交易日，向前连接仅隔一个非涨停日的板；未知立即停止。"""
    rows = history[-(HISTORY_DAYS - 1):] + [{"is_limit_up": today_up}]
    if today_up is not True:
        return {"window_days": None, "limit_up_days": None, "label": None, "is_final": final}
    start = len(rows) - 1
    gap = 0
    for i in range(len(rows) - 2, -1, -1):
        value = rows[i]["is_limit_up"]
        if value is None:
            break
        if value:
            start, gap = i, 0
        else:
            gap += 1
            if gap > 1:
                break
    days = len(rows) - start
    boards = sum(row["is_limit_up"] is True for row in rows[start:])
    return {"window_days": days, "limit_up_days": boards, "label": f"{days}天{boards}板",
            "is_final": final, "rule": "max20_sessions_max1_gap_v1"}


def stock_fact(code, quote, before, previous, history, catalog, observed_at, *, final=False):
    q = quote_fields(quote or {})
    at = instant(observed_at)
    source = instant(q.get("market_data_time"))
    name = q.get("name") or (before or {}).get("name")
    is_st = q.get("is_st") if q.get("is_st") is not None else bool(name and "ST" in name.upper())
    is_delisting = q.get("is_delisting") if q.get("is_delisting") is not None else bool(name and "退" in name)
    excluded = "st" if is_st else "delisting" if is_delisting else None
    if q.get("is_suspended") is True:
        excluded = "suspended"
    if q.get("has_price_limit") is False:
        excluded = excluded or "no_price_limit"
    price, upper, lower = (cents(q.get(key)) for key in ("price", "limit_up_price", "limit_down_price"))
    ref = number(q.get("previous_close"))
    error = q.get("quality_issue")
    if not q or source is None:
        error = "missing_quote"
    elif source.astimezone(CN).date() != at.astimezone(CN).date():
        error = "previous_trade_date"
    elif not final and not -5 <= (at - source).total_seconds() <= STALE_SECONDS:
        error = "stale_source_time"
    elif price is None or ref is None or ref <= 0:
        error = "invalid_price"
    elif not name:
        error = "unknown_security_status"
    limits_valid = upper is not None and lower is not None and 0 < lower < upper < 9999900
    if limits_valid and price is not None and not lower <= price <= upper:
        limits_valid = False
    if not limits_valid and not excluded:
        error = error or "unknown_price_limits"
    phase = q.get("phase", "unknown")
    continuous = phase in {"continuous", "session_close"} or final
    if not continuous:
        error = error or "auction_or_nontrading_quote"
    if q.get("is_suspended") is True:
        # A carried close on a suspended day is not an observed return for
        # yesterday's fixed cohort, even when its price/limits are well formed.
        error = error or "suspended"
    usable = error is None and price is not None and limits_valid
    up = price == upper if usable else None
    down = price == lower if usable else None
    change = float(Decimal(str(q["price"])) / Decimal(str(ref)) - 1) if price and ref and ref > 0 and not error else None
    prior = before or {}
    source_iso = source.isoformat() if source else None
    prev_source = instant(prior.get("source_time"))
    ordered = source is None or prev_source is None or source >= prev_source
    # Daily facts are reconciled at the market close (15:00), whereas quote
    # feeds may continue stamping observations after that time. Finalization
    # is not an intraday tick and must not fail the tick-order check.
    if not final and not ordered:
        usable, up, down, change, error = False, None, None, None, "out_of_order"
    adjacent = bool(not final and usable and prior.get("valid") and prev_source
                    and 0 < (source - prev_source).total_seconds() <= MAX_GAP_SECONDS)
    # A repeat of the same source timestamp cannot establish a new transition.
    new_observation = not final and usable and (prev_source is None or source > prev_source)
    bids, asks = q.get("bids") or [], q.get("asks") or []
    book_valid = bool(bids and asks and all(len(level) == 2 and number(level[0]) is not None and number(level[1]) is not None and level[0] >= 0 and level[1] >= 0 for level in list(bids) + list(asks)))
    sealed = bool(up and cents(bids[0][0]) == upper and bids[0][1] > 0 and all(v == 0 for _, v in asks)) if usable and book_valid and not final else None
    seal_amount = float(bids[0][0] * bids[0][1]) if sealed else None
    touched = prior.get("touched_up", False) or up is True
    high = cents(q.get("high_price"))
    touched = touched or bool(usable and high == upper)
    if final:
        touched = bool(up or usable and high == upper)
    drawdown = float(Decimal(price) / Decimal(high) - 1) if usable and high and high >= price else None
    times = {key: prior.get(key) for key in ("first_touch_at", "first_seal_at", "last_reseal_at", "last_open_at")}
    events = []
    if new_observation and up and not prior.get("first_touch_at"):
        times["first_touch_at"] = source_iso
        events.append("first_touch")
    if new_observation and sealed and not prior.get("first_seal_at"):
        times["first_seal_at"] = source_iso
        events.append("first_seal")
    opened = int(prior.get("observed_open_count", 0))
    if adjacent and prior.get("is_limit_up") is True and up is False:
        opened += 1
        times["last_open_at"] = source_iso
        events.append("open")
    if adjacent and prior.get("is_limit_up") is False and up and prior.get("touched_up"):
        times["last_reseal_at"] = source_iso
        events.append("reseal")
    if new_observation and down and (not prior or adjacent and prior.get("is_limit_down") is False):
        events.append("limit_down")
    if final:
        events = []  # Daily OHLC must not invent intraday transitions.
    streak_before = previous.get("streak") if previous else None
    streak = streak_before + 1 if up and streak_before is not None else (0 if up is False else None)
    if final and up and q.get("final_streak") is not None:
        streak = q["final_streak"]
    item = {"code": code, "name": name, "market": "BJ" if board_type(code) == "bse" else "SH" if code.startswith("6") else "SZ",
            "board_type": board_type(code),
            "price": q.get("price") if usable else None, "previous_close": ref, "change_ratio": change,
            "drawdown_from_high_ratio": drawdown,
            "amount_yuan": number(q.get("amount")) if usable else None,
            "turnover_ratio": number(q.get("turnover_pct")) / 100 if usable and number(q.get("turnover_pct")) is not None else None,
            "open_change_ratio": float(Decimal(str(q["open_price"])) / Decimal(str(ref)) - 1) if usable and number(q.get("open_price")) and ref else None,
            "limit_up_price": upper / 100 if limits_valid else None, "limit_down_price": lower / 100 if limits_valid else None,
            "limit_rule": q.get("limit_rule", "provider_daily_limits"), "is_limit_up": up, "is_limit_down": down,
            "limit_status": "unknown" if up is None else ("sealed" if sealed else "at_limit") if up else "opened" if touched else "not_touched",
            "is_sealed": sealed, "seal_amount_yuan": seal_amount, "seal_volume_shares": bids[0][1] if sealed else None,
            "touched_up": touched, "observed_open_count": opened, **times,
            "confirmed_streak_before_today": streak_before, "intraday_streak": streak,
            "target_streak": streak_before + 1 if streak_before is not None else None,
            "board_run": board_run(history, up, final=final),
            "theme_ids": catalog.ids(code), "attributed_theme_id": None,
            "attribution_basis": "membership_only", "source_time": source_iso,
            "received_at": q.get("received_at"), "provider": q.get("provider"),
            "valid": usable, "eligible": usable and excluded is None, "excluded_reason": excluded,
            "is_stale": error in {"missing_quote", "stale_source_time", "previous_trade_date"},
            "missing_reasons": {"quote": error} if error else {},
            "timing_basis": "snapshot_observation", "path_complete": False,
            "status_basis": "daily_observation" if final else "quote_name_and_provider_limits",
            "is_final": final}
    if not book_valid or final:
        item["missing_reasons"]["seal_amount_yuan"] = "no_verified_live_book"
    if streak_before is None:
        item["missing_reasons"]["confirmed_streak_before_today"] = "missing_previous_streak"
        if streak is None:
            item["missing_reasons"]["intraday_streak"] = "missing_previous_streak"
    if not excluded and not final and name and name.startswith(("N", "C")) and not limits_valid:
        item["missing_reasons"]["limit_up_price"] = "unverified_new_listing_limits"
    event_rows = [{"type": kind, "code": code, "theme_ids": item["theme_ids"],
                   "source_time": source_iso, "observed_at": observed_at,
                   "previous_observation_at": prior.get("source_time") if adjacent else None, "timing_basis": "snapshot_observation"}
                  for kind in events]
    return item, event_rows
