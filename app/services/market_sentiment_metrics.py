"""每日市场事实统计；只依赖逐股观测和此前 20 个交易日，不运行研究模型。"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import replace
from decimal import Decimal
import math
from statistics import median

from app.quant.data.sentiment_inputs import StockInput, TradingCalendar, stock_features

SCOPE = "hs_mainboard"
VERSION = "daily-observation-v2"
MIN_COVERAGE = .98
AMOUNT_WINDOW = 20


def observed_features(market, previous=None):
    expected = market["expected_codes"]
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("统计名单不能为空或重复")
    stocks = {row["code"]: StockInput(**row) for row in market["stocks"]}
    if len(stocks) != len(market["stocks"]) or set(stocks) - set(expected):
        raise ValueError("行情重复或包含名单之外的股票")
    features = {}
    for code, stock in stocks.items():
        observed = replace(stock, status_source="baostock.daily_observation", status_known_at=None)
        feature = stock_features(observed, market["trade_date"], (previous or {}).get(code),
                                 evidence_mode="daily_observation")
        if stock.is_suspended and (stock.volume_shares or 0) > 0 and (stock.amount_yuan or 0) > 0:
            feature = replace(feature, eligible=False, status_valid=False, is_suspended=False,
                              exclusion_reason=None, error="priced_suspension_conflict", limit_streak=None)
        features[code] = feature
    return stocks, features


class MarketSentimentCalculator:
    def __init__(self, calendar: TradingCalendar, previous_raw=None, history=()):
        self.calendar = calendar
        self.history = deque(history, maxlen=AMOUNT_WINDOW)
        self.previous = self.history[-1] if self.history else None
        self.features = {}
        if bool(previous_raw) != bool(self.previous):
            raise ValueError("上一日原始依据与汇总必须同时存在")
        if previous_raw:
            if previous_raw["version"] != VERSION or any(row["version"] != VERSION for row in self.history):
                raise ValueError("情绪存储版本不同，请先迁移")
            if previous_raw["trade_date"] != self.previous["trade_date"]:
                raise ValueError("上一日原始依据与汇总日期不一致")
            for left, right in zip(self.history, list(self.history)[1:]):
                if calendar.next_date(left["trade_date"]) != right["trade_date"]:
                    raise ValueError("历史汇总必须逐交易日连续")
            _, self.features = observed_features(previous_raw["market"])
            streaks = previous_raw["limit_streaks"]
            if set(streaks) != {code for code, f in self.features.items() if f.is_limit_up}:
                raise ValueError("上一日涨停名单与连板高度不一致")
            for code, height in streaks.items():
                if height is not None and (type(height) is not int or height < 1):
                    raise ValueError("连板高度必须为正整数或 null")
                self.features[code] = replace(self.features[code], limit_streak=height)

    @property
    def latest_date(self):
        return self.previous["trade_date"] if self.previous else None

    @property
    def limit_streaks(self):
        return {code: f.limit_streak for code, f in self.features.items() if f.is_limit_up}

    def process(self, market: dict) -> dict:
        day = market["trade_date"]
        self.calendar.next_date(day)
        if self.previous and self.calendar.next_date(self.latest_date) != day:
            raise ValueError("输入必须逐交易日递增")
        stocks, features = observed_features(market, self.features)
        valid = [features[c] for c in sorted(features) if features[c].eligible]
        excluded = Counter(f.exclusion_reason for f in features.values() if f.exclusion_reason)
        expected = len(market["expected_codes"]) - sum(excluded.values())
        coverage = len(valid) / expected if expected else 0.0
        up = sum(f.is_limit_up is True for f in valid)
        touch = sum(f.is_touch_up is True for f in valid)
        amount = sum(f.amount_yuan for f in valid) if valid else None
        if amount is not None and not math.isfinite(amount):
            raise ValueError("成交额合计超出有效数值范围")
        # 历史值和当日值均用亿元；窗口严格不包含当日。
        amount_yi = amount / 1e8 if amount is not None else None
        amounts = [row["values"]["amount"] for row in self.history]
        amount_ratio = (amount_yi / median(amounts) if amount_yi is not None
                        and len(amounts) == AMOUNT_WINDOW and all(v is not None and v > 0 for v in amounts)
                        else None)
        streaks = Counter(f.limit_streak for f in valid if f.is_limit_up)
        height = max(f.limit_streak for f in valid) if valid and all(f.limit_streak is not None for f in valid) else None
        values = dict(limit_up=up, limit_down=sum(f.is_limit_down is True for f in valid),
                      seal_rate=up / touch if touch else None, broken=touch - up,
                      height=height, previous_height=self.previous["values"]["height"] if self.previous else None,
                      coverage=coverage, eligible=len(valid), missing=expected - len(valid),
                      amount=amount_yi, amount_ratio=amount_ratio)
        values.update({f"board_{n}": streaks[n] for n in range(1, 10)})
        values.update(board_10_plus=sum(v for k, v in streaks.items() if k is not None and k >= 10),
                      unknown_streak=streaks[None],
                      chain_count=sum(v for k, v in streaks.items() if k is not None and k >= 2),
                      one_word=sum(f.is_one_word_up is True for f in valid),
                      up_over_5=sum(f.close_premium > .05 and not f.is_limit_up for f in valid),
                      down_over_5=sum(f.close_premium < -.05 and not f.is_limit_down for f in valid),
                      advancing=sum(f.close_premium > 0 for f in valid),
                      declining=sum(f.close_premium < 0 for f in valid),
                      flat=sum(f.close_premium == 0 for f in valid),
                      drawdown_8=sum(Decimal(str(stocks[f.code].raw_close)) <= Decimal(str(stocks[f.code].raw_high)) * Decimal(".92") for f in valid),
                      floor_to_ceiling=sum(f.is_limit_up and stocks[f.code].raw_low == stocks[f.code].limit_down for f in valid),
                      ceiling_to_floor=sum(f.is_limit_down and stocks[f.code].raw_high == stocks[f.code].limit_up for f in valid))
        reasons = {}

        def cohort(key, predicate, reduce, *, needs_streak=False, needs_limit=False):
            values[key] = None
            if not self.previous or self.previous["values"]["coverage"] < MIN_COVERAGE:
                reasons[key] = "缺少完整的上一交易日股票池"
                return
            if needs_streak and self.previous["values"]["height"] is None:
                reasons[key] = "昨日连板高度尚不完整"
                return
            codes = [c for c, f in self.features.items() if f.eligible and predicate(f)]
            observed = [features[c] for c in codes if c in features and features[c].price_valid
                        and features[c].status_valid and not features[c].is_suspended
                        and (not needs_limit or features[c].is_limit_up is not None)]
            if len(observed) != len(codes):
                reasons[key] = f"昨日样本今日仅可观测 {len(observed)}/{len(codes)} 家"
            elif not codes:
                reasons[key] = "昨日无对应样本"
            else:
                values[key] = reduce(observed)

        cohort("yesterday_up_return", lambda f: f.is_limit_up, lambda fs: median(f.close_premium for f in fs))
        cohort("yesterday_chain_return", lambda f: f.is_limit_up and f.limit_streak >= 2,
               lambda fs: median(f.close_premium for f in fs), needs_streak=True)
        for key, level in (("promotion", 0), ("chain_promotion", 2), ("high_promotion", 3),
                           ("one_to_two", 1), ("two_to_three", -2)):
            def selected(f, level=level):
                return f.is_limit_up and (level == 0 or
                    (f.limit_streak == abs(level) if level in (1, -2) else f.limit_streak >= level))
            cohort(key, selected, lambda fs: sum(f.is_limit_up for f in fs) / len(fs),
                   needs_streak=level != 0, needs_limit=True)
        cohort("consecutive_down", lambda f: f.is_limit_down,
               lambda fs: sum(f.is_limit_down is True for f in fs), needs_limit=True)
        cohort("high_loss", lambda f: f.is_limit_up and f.limit_streak >= 4,
               lambda fs: sum(f.close_premium < -.05 for f in fs), needs_streak=True)
        if coverage < MIN_COVERAGE:
            for key in values.keys() - {"coverage", "eligible", "missing"}:
                values[key] = None
                reasons[key] = "行情覆盖不足 98%，不展示可能失真的统计值"
        for key, value in values.items():
            if value is None and key not in reasons:
                reasons[key] = "无对应样本或历史预热不足"
        result = {"scope": SCOPE, "version": VERSION, "trade_date": day, "values": values,
                  "missing_reasons": reasons, "quality": {
                      "expected_mainboard": len(market["expected_codes"]), "expected_eligible": expected,
                      "excluded": dict(excluded),
                      "price_errors": dict(Counter(f.error for f in features.values() if f.error)),
                      "unknown_delisting": sum(s.is_delisting is None for s in stocks.values())}}
        self.features, self.previous = features, result
        self.history.append(result)
        return result
