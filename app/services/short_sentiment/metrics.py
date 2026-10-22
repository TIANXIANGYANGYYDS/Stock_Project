from __future__ import annotations

from collections import Counter
from statistics import median

MIN_COVERAGE = .98


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def feedback(stocks, previous, codes):
    result = {}
    predicates = {
        "yesterday_limit_up": lambda f: f["is_limit_up"] is True,
        "yesterday_first_board": lambda f: f["is_limit_up"] is True and f["streak"] == 1,
        "yesterday_chain": lambda f: f["is_limit_up"] is True and f["streak"] is not None and f["streak"] >= 2,
        "yesterday_high_board": lambda f: f["is_limit_up"] is True and f["streak"] is not None and f["streak"] >= 5,
        "yesterday_broken": lambda f: f["is_touch_up"] is True and f["is_limit_up"] is False,
    }
    for key, predicate in predicates.items():
        selected = [code for code in codes if code in previous and previous[code]["eligible"] and predicate(previous[code])]
        observed = [stocks[c] for c in selected if c in stocks and stocks[c]["valid"]]
        complete = bool(selected) and len(observed) == len(selected)
        result[key] = {"sample_count": len(selected), "observed_count": len(observed),
                       "coverage": ratio(len(observed), len(selected)), "codes": selected,
                       "return_median": median(s["change_ratio"] for s in observed) if complete else None,
                       "open_return_median": median(s["open_change_ratio"] for s in observed) if complete and all(s["open_change_ratio"] is not None for s in observed) else None,
                       "advancing_count": sum(s["change_ratio"] > 0 for s in observed) if complete else None,
                       "loss_over_5_count": sum(s["change_ratio"] < -.05 for s in observed) if complete else None,
                       "limit_down_count": sum(s["is_limit_down"] is True for s in observed) if complete else None,
                       "promotion": sum(s["is_limit_up"] is True for s in observed) / len(selected) if complete else None,
                       "missing_reason": None if complete else "missing_previous_cohort" if not previous else "empty_cohort" if not selected else "incomplete_current_cohort"}
    return result


def aggregate(stocks, previous, codes, cohort_codes=None):
    expected = [stocks[c] for c in codes if c in stocks and stocks[c]["excluded_reason"] is None]
    eligible = [s for s in expected if s["eligible"]]
    coverage = ratio(len(eligible), len(expected))
    up = [s for s in eligible if s["is_limit_up"]]
    touched = [s for s in eligible if s["touched_up"]]
    board_counts = Counter(s["intraday_streak"] for s in up)
    cohort_codes = codes if cohort_codes is None else cohort_codes
    cohorts = feedback(stocks, previous, cohort_codes)
    values = {
        "expected": len(expected), "eligible": len(eligible), "missing": len(expected) - len(eligible),
        "excluded": len(codes) - len(expected), "coverage": coverage,
        "limit_up": len(up), "limit_down": sum(s["is_limit_down"] is True for s in eligible),
        "limit_up_ratio": ratio(len(up), len(eligible)), "touch_up": len(touched),
        "broken": len(touched) - len(up), "seal_rate": ratio(len(up), len(touched)),
        "currently_open_ratio": ratio(len(touched) - len(up), len(touched)),
        "ever_open_ratio": ratio(sum(s["observed_open_count"] > 0 for s in eligible), sum(s["first_touch_at"] is not None for s in eligible)),
        "advancing": sum(s["change_ratio"] > 0 for s in eligible),
        "declining": sum(s["change_ratio"] < 0 for s in eligible),
        "flat": sum(s["change_ratio"] == 0 for s in eligible),
        "up_over_5": sum(s["change_ratio"] > .05 and not s["is_limit_up"] for s in eligible),
        "down_over_5": sum(s["change_ratio"] < -.05 and not s["is_limit_down"] for s in eligible),
        "drawdown_8": sum(s["drawdown_from_high_ratio"] <= -.08 for s in eligible) if all(s["drawdown_from_high_ratio"] is not None for s in eligible) else None,
        "advancing_ratio": ratio(sum(s["change_ratio"] > 0 for s in eligible), len(eligible)),
        "return_median": median(s["change_ratio"] for s in eligible) if eligible else None,
        "amount_yuan": sum(s["amount_yuan"] for s in eligible) if eligible and all(s["amount_yuan"] is not None for s in eligible) else None,
        "height": max((s["intraday_streak"] for s in up), default=0) if all(s["intraday_streak"] is not None for s in up) else None,
        "chain_count": sum(count for height, count in board_counts.items() if height is not None and height >= 2),
        "unknown_streak": board_counts[None],
        "board_counts": {str(n): board_counts[n] for n in range(1, 10)} | {"10_plus": sum(v for k, v in board_counts.items() if k is not None and k >= 10)},
    }
    for name, height in (("one_to_two", 1), ("two_to_three", 2)):
        group = [c for c in cohort_codes if c in previous and previous[c]["eligible"] and previous[c]["is_limit_up"] and previous[c]["streak"] == height]
        observed = [stocks[c] for c in group if c in stocks and stocks[c]["valid"]]
        values[name] = ratio(sum(s["is_limit_up"] for s in observed), len(group)) if len(observed) == len(group) else None
    reasons = {}
    if coverage is None or coverage < MIN_COVERAGE:
        for key in values.keys() - {"expected", "eligible", "missing", "excluded", "coverage"}:
            values[key] = None
            reasons[key] = "coverage_below_98_percent"
    return {"values": values, "feedback": cohorts, "missing_reasons": reasons,
            "count_basis": "unique_codes", "touch_basis": "observed_path_or_provider_daily_high"}
