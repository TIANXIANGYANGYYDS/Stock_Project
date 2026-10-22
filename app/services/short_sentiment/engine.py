from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from app.services.short_sentiment.facts import CN, daily_history, instant, stock_fact
from app.services.short_sentiment.metrics import aggregate
from app.services.short_sentiment.price_events import LABELS, price_events
from app.services.short_sentiment.environment_runtime import EnvironmentRuntime, VERSION as RULE_VERSION, empty_emotion, source_rows

from app.services.short_sentiment.universe import BOARDS, DEFAULT_SCOPE, in_scope, board_type, snapshot_identity

SCOPE = DEFAULT_SCOPE


class SentimentEngine:
    def __init__(self, catalog, raw_days, sessions, *, scope=DEFAULT_SCOPE):
        self.scope = scope
        self.catalog = catalog
        self.history_available_sessions = [d for d in sessions if any(r["trade_date"] == d and r.get("scope") == scope for r in raw_days)]
        self.history, self.previous, self.previous_expected = daily_history(raw_days, sessions)
        if scope == "all_a" and sessions and sessions[-1] not in self.history_available_sessions:
            self.previous, self.previous_expected = {}, []
        self.theme_cohorts = {theme_id: [c for c in self.previous_expected if theme_id in catalog.ids(c)]
                              for theme_id in catalog.themes}
        previous_raw = next((r for r in raw_days if sessions and r["trade_date"] == sessions[-1] and r.get("scope") == scope), None)
        previous_stocks = {}
        if previous_raw:
            daily = daily_envelope(previous_raw)
            for quote in daily["quotes"]:
                item, _ = stock_fact(quote["code"], quote, None, None, [], catalog, daily["observed_at"], final=True)
                previous_stocks[quote["code"]] = item
        self.environment = EnvironmentRuntime(catalog, source_rows(previous_stocks))
        self.board_cohorts = {board: [c for c in self.previous_expected if board_type(c) == board] for board in BOARDS}

    def process(self, envelope, checkpoint=None, *, final=False):
        before = checkpoint or {}
        if before and before["meta"]["scope"] != self.scope:
            raise ValueError("不能混用不同市场范围的检查点")
        at = instant(envelope["observed_at"]).astimezone(CN)
        day = at.date().isoformat()
        same_day = before.get("meta", {}).get("trade_date") == day
        prior_stocks = before.get("stocks", {}) if same_day else {}
        seq = before.get("sequence", 0) + 1 if same_day else 1
        identity = snapshot_identity(day, seq, self.scope)
        quotes = {}
        for q in envelope["quotes"]:
            code = q["code"]
            if not in_scope(code, self.scope):
                continue
            old = quotes.get(code)
            # Select newest valid observation, never let a stale fallback replace it.
            key = (not bool(q.get("quality_issue")), q.get("market_data_time") or "")
            if old is None or key > (not bool(old.get("quality_issue")), old.get("market_data_time") or ""):
                quotes[code] = q
        requested = {c for c in envelope["expected_codes"] if in_scope(c, self.scope)}
        if final:
            expected = sorted(requested)
        elif self.previous_expected:
            # The quote crawler also retains delisted historical codes. Freeze the
            # last daily universe and admit new codes only upon an actual fresh
            # trade, rather than treating every retained symbol as missing data.
            fresh_new = {code for code, q in quotes.items() if not q.get("quality_issue")
                         and q.get("phase") in {"continuous", "session_close"}
                         and instant(q.get("market_data_time")) is not None
                         and instant(q["market_data_time"]).astimezone(CN).date() == at.date()
                         and (q.get("amount") or 0) > 0}
            expected = sorted(set(self.previous_expected) | set(prior_stocks) | fresh_new)
        else:
            expected = sorted(requested | set(prior_stocks))
        stocks, events = {}, []
        observed = at.isoformat()
        for code in expected:
            item, changes = stock_fact(code, quotes.get(code), prior_stocks.get(code), self.previous.get(code),
                                       self.history.get(code, []), self.catalog, observed, final=final)
            stocks[code] = item
            if item["eligible"]:
                events.extend(changes)
        behavior_events, behavior_runtime = price_events(
            stocks, prior_stocks, before.get("price_event_runtime", {}) if same_day else {}, observed, final=final)
        events.extend(behavior_events)
        market = aggregate(stocks, self.previous, expected, self.previous_expected)
        previous_market = before.get("market", {}).get("emotion")
        market_emotion, theme_emotions, environment_runtime, environment_updated = self.environment.process(stocks, before, observed, final=final)
        market["emotion"] = market_emotion
        market["scope"] = self.scope
        boards = []
        for board, name in BOARDS.items():
            if self.scope == "hs_mainboard" and board not in {"sh_mainboard", "sz_mainboard"}:
                continue
            codes = [c for c in expected if board_type(c) == board]
            cohort = self.board_cohorts[board]
            stats = aggregate(stocks, self.previous, codes, cohort)
            stats["emotion"] = empty_emotion(observed, reason="board_is_not_theme_environment")
            stats["emotion"]["status"] = "unclassified"
            boards.append({"board_type": board, "name": name, "member_count": len(codes), **stats})
        themes = []
        grouped = {theme_id: [] for theme_id in self.catalog.themes}
        for code in expected:
            for theme_id in stocks[code]["theme_ids"]:
                grouped[theme_id].append(code)
        prior_themes = {row["theme_id"]: row for row in before.get("themes", [])}
        for theme_id, codes in grouped.items():
            cohort_codes = self.theme_cohorts[theme_id]
            stats = aggregate(stocks, self.previous, codes, cohort_codes)
            stats["emotion"] = theme_emotions[theme_id]
            themes.append({**self.catalog.themes[theme_id], **stats, "scope_member_count": len(codes),
                           "membership_basis": "fixed_catalog", "historical_membership_verified": False})
        for target, current, old in [(self.scope, market["emotion"], previous_market)] + [
            (theme["theme_id"], theme["emotion"], prior_themes.get(theme["theme_id"], {}).get("emotion")) for theme in themes
        ]:
            old = old if old and old.get("rule_version") == current["rule_version"] else None
            if environment_updated and current["status"] == "confirmed" and current["state"] != (old or {}).get("state"):
                events.append({"type": "emotion_changed", "scope_id": target,
                               "theme_ids": [] if target == self.scope else [target], "code": None,
                               "previous_state": (old or {}).get("state"), "state": current["state"],
                               "observed_at": observed, "source_time": current["evaluated_at"],
                               "timing_basis": "five_minute_environment", "rule_version": current["rule_version"],
                               "evidence": {"entry": current["entry"], "hold": current["hold"], "facts": current["facts"]}})
        for index, event in enumerate(events):
            event.setdefault("label", LABELS[event["type"]])
            event.setdefault("name", stocks[event["code"]]["name"] if event.get("code") else None)
            event.update(event_id=f"{identity}:{index:05d}", snapshot_id=identity, trade_date=day, sequence=seq, scope=self.scope)
        recent = [event for event in (before.get("activity_events", []) if same_day else []) + events
                  if event["code"] and 0 <= (at - instant(event["source_time"])).total_seconds() <= 300]
        for target, stats in [(None, market)] + [(t["theme_id"], t) for t in themes]:
            selected = [e for e in recent if target is None or target in e["theme_ids"]]
            stats["recent_activity"] = {"window_seconds": 300, "basis": "observed_events",
                **{kind: sum(e["type"] == kind for e in selected) for kind in LABELS if kind != "emotion_changed"}}
        meta = {"trade_date": day, "session": "closed" if final else envelope.get("session", "unknown"),
                "scope": self.scope, "snapshot_id": identity, "as_of": observed,
                "generated_at": datetime.now(CN).isoformat(), "is_final": final,
                "clock_offset_seconds": 0.0 if final else envelope.get("clock_offset_seconds", 0.0),
                "clock_basis": "calibrated_capture" if not final and "clock_offset_seconds" in envelope else "system",
                "is_stale": False, "coverage": market["values"]["coverage"],
                "classification_version": self.catalog.version, "rule_version": RULE_VERSION,
                "sample_interval_seconds": 5, "price_basis": "raw",
                "exclusions": ["st", "delisting", "suspended", "no_price_limit"],
                "classification_basis": "fixed_catalog", "data_mode": "daily_final" if final else "observed_snapshots"}
        meta.update(is_retained=False, retention_reason=None, retained_from_snapshot_id=None,
                    processed_as_of=observed,
                    finalization={"status": "finalized" if final else "not_due", "pending_reasons": []})
        meta["environment_interval_seconds"] = 300
        meta["event_rule_version"] = "price-behavior-v1"
        meta["history_available_sessions"] = self.history_available_sessions
        meta["included_boards"] = [b["board_type"] for b in boards]
        meta["universe_basis"] = "daily_observation_universe" if final else "previous_daily_plus_fresh_observations" if self.previous_expected else "quote_requested_unverified"
        if envelope.get("universe_basis"):
            meta["universe_basis"] = envelope["universe_basis"]
        return {"meta": meta, "sequence": seq, "observation_id": identity, "stocks": stocks, "market": market, "themes": themes, "boards": boards,
                "history": self.history, "events": events,
                "activity_events": recent, "price_event_runtime": behavior_runtime,
                "environment_runtime": environment_runtime, "environment_updated": environment_updated,
                "series_minute": before.get("series_minute") if same_day else None,
                "input_id": envelope.get("batch_id", observed),
                "source_cursor": envelope.get("source_cursor"),
                "final_input_hash": envelope.get("input_hash") if final else None}


def public_emotion(value):
    return {k: v for k, v in value.items() if k not in {"comparison", "candidate_since"}}


def public_stats(value, *, include_codes=False):
    result = deepcopy(value)
    result["emotion"] = public_emotion(result["emotion"])
    if not include_codes:
        for cohort in result["feedback"].values():
            cohort.pop("codes", None)
    return result


def daily_envelope(raw, checkpoint=None):
    """Finalize from immutable raw daily facts; never manufacture a minute path."""
    if "envelope" in raw:
        result = deepcopy(raw["envelope"])
        result["source_cursor"] = (checkpoint or {}).get("source_cursor")
        result["finalized_at"] = datetime.now(CN).isoformat()
        return result
    from app.services.market_sentiment_metrics import observed_features
    _, features = observed_features(raw["market"])
    day = raw["trade_date"]
    close_at = day + "T15:00:00+08:00"
    quotes = []
    old = (checkpoint or {}).get("stocks", {})
    for row in raw["market"]["stocks"]:
        code = row["code"]
        quotes.append({"code": code, "name": old.get(code, {}).get("name") or row.get("name") or code,
                       "price": row.get("raw_close"), "previous_close": row.get("raw_preclose"),
                       "open_price": row.get("raw_open"), "high_price": row.get("raw_high"),
                       "low_price": row.get("raw_low"), "amount": row.get("amount_yuan"),
                       "limit_up_price": row.get("limit_up"), "limit_down_price": row.get("limit_down"),
                       "is_suspended": row.get("is_suspended"), "has_price_limit": row.get("has_price_limit"),
                       "is_st": row.get("is_st"), "is_delisting": row.get("is_delisting"),
                       "final_streak": raw["limit_streaks"].get(code),
                       "quality_issue": features[code].error,
                       "market_data_time": close_at, "received_at": None, "phase": "session_close",
                       "provider": row.get("source"), "limit_rule": raw.get("provenance", {}).get("limit_method", "daily_observation")})
    return {"batch_id": "daily:" + raw["input_hash"], "input_hash": raw["input_hash"],
            "quotes": quotes, "expected_codes": raw["market"]["expected_codes"],
            "observed_at": close_at, "finalized_at": datetime.now(CN).isoformat(),
            "source_cursor": (checkpoint or {}).get("source_cursor")}
