"""每日基础统计的数据结构和原始价格核验；无数据库或网络副作用。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any, Mapping

CN_TZ = timezone(timedelta(hours=8))
MAX_STOCKS_PER_DAY = 10_000


def iso_date(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期必须是YYYY-MM-DD")
    return value


def known_by_close(value: str | None, trade_date: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        instant = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return instant.tzinfo is not None and instant <= datetime.combine(
        date.fromisoformat(trade_date), time(15), CN_TZ,
    )


@dataclass(frozen=True)
class TradingCalendar:
    """显式交易日序列；缺交易日不能通过压缩价格行隐式跳过。"""
    trade_dates: tuple[str, ...]
    source: str

    def __post_init__(self) -> None:
        if not self.source or len(self.trade_dates) < 2:
            raise ValueError("需要有来源的交易日历，且包含下一执行日")
        for day in self.trade_dates:
            iso_date(day)
        if tuple(sorted(set(self.trade_dates))) != self.trade_dates:
            raise ValueError("交易日历必须严格递增且没有重复")

    def next_date(self, trade_date: str) -> str:
        index = self.trade_dates.index(trade_date)
        if index + 1 == len(self.trade_dates):
            raise ValueError(f"日历缺少{trade_date}之后的交易日")
        return self.trade_dates[index + 1]


@dataclass(frozen=True)
class StockInput:
    code: str
    price_basis: str
    source: str
    status_source: str
    status_known_at: str | None
    is_st: bool | None
    is_suspended: bool | None
    is_delisting: bool | None
    has_price_limit: bool | None
    raw_preclose: float | None = None
    raw_open: float | None = None
    raw_high: float | None = None
    raw_low: float | None = None
    raw_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    amount_yuan: float | None = None
    volume_shares: float | None = None
    tick_size: float = .01

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("股票代码必须是六位字符串")
        if not isinstance(self.price_basis, str) or not isinstance(self.source, str) or not isinstance(self.status_source, str):
            raise ValueError("价格口径与来源必须是字符串")
        for key in ("is_st", "is_suspended", "is_delisting", "has_price_limit"):
            if getattr(self, key) is not None and type(getattr(self, key)) is not bool:
                raise ValueError(f"{key}必须为布尔值或null")
        for key in ("raw_preclose", "raw_open", "raw_high", "raw_low", "raw_close",
                    "limit_up", "limit_down", "amount_yuan", "volume_shares", "tick_size"):
            value = getattr(self, key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"{key}必须为有限数或null")


@dataclass(frozen=True)
class MarketDay:
    trade_date: str
    expected_codes: tuple[str, ...]
    universe_source: str
    universe_known_at: str
    stocks: tuple[StockInput, ...]

    def __post_init__(self) -> None:
        iso_date(self.trade_date)
        if not self.universe_source or not known_by_close(self.universe_known_at, self.trade_date):
            raise ValueError("历史主板名单需要来源及不晚于当日收盘的已知时间")
        if len(self.expected_codes) > MAX_STOCKS_PER_DAY or len(self.stocks) > MAX_STOCKS_PER_DAY:
            raise ValueError("单日股票数量超过研究内存边界")
        codes = self.expected_codes
        if not codes or len(set(codes)) != len(codes):
            raise ValueError("expected_codes必须是非空且无重复的当日主板上市名单")
        if any(not isinstance(code, str) or len(code) != 6 or not code.isdigit() for code in codes):
            raise ValueError("历史名单代码格式错误")
        if len({stock.code for stock in self.stocks}) != len(self.stocks):
            raise ValueError("同日股票记录重复")

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> MarketDay:
        return cls(
            trade_date=row["trade_date"], expected_codes=tuple(row["expected_codes"]),
            universe_source=row["universe_source"], universe_known_at=row["universe_known_at"],
            stocks=tuple(StockInput(**stock) for stock in row["stocks"]),
        )


@dataclass(frozen=True)
class StockFeatures:
    code: str
    eligible: bool
    status_valid: bool
    price_valid: bool
    exclusion_reason: str | None
    error: str | None
    is_suspended: bool
    is_limit_up: bool | None
    is_touch_up: bool | None
    is_limit_down: bool | None
    is_one_word_up: bool | None
    is_one_word_down: bool | None
    limit_streak: int | None
    open_premium: float | None
    close_premium: float | None
    amount_yuan: float | None
    evidence_mode: str = "point_in_time"


def _ticks(value: float | None, tick: float) -> int:
    if value is None or value <= 0:
        raise ValueError("missing_or_nonpositive_price")
    try:
        units = Decimal(str(value)) / Decimal(str(tick))
        if units != units.to_integral_value():
            raise ValueError("price_not_on_tick")
        return int(units)
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError("invalid_tick_size") from exc


def stock_features(stock: StockInput, trade_date: str,
                   previous: StockFeatures | None = None, *,
                   evidence_mode: str = "point_in_time") -> StockFeatures:
    if evidence_mode not in {"point_in_time", "daily_observation"}:
        raise ValueError("未知特征证据口径")
    status_valid = (
        bool(stock.status_source)
        and not stock.status_source.startswith(("derived.", "local.security_name"))
        and known_by_close(stock.status_known_at, trade_date)
        and all(getattr(stock, key) is not None for key in
                ("is_st", "is_suspended", "is_delisting", "has_price_limit"))
    )
    if evidence_mode == "daily_observation":
        status_valid = (stock.status_source == "baostock.daily_observation"
                        and all(getattr(stock, key) is not None for key in
                                ("is_st", "is_suspended", "has_price_limit")))
    excluded = None
    if status_valid:
        for condition, reason in ((stock.is_suspended, "suspended"), (stock.is_st, "st"),
                                  (stock.is_delisting, "delisting"),
                                  (not stock.has_price_limit, "no_price_limit")):
            if condition:
                excluded = reason
                break
    error = None if status_valid else "unverified_status"
    up = touch = down = one_up = one_down = None
    streak = open_premium = close_premium = amount = None
    price_valid = False
    if excluded == "suspended":
        streak = 0
    else:
        try:
            if stock.price_basis != "raw" or not stock.source:
                raise ValueError("unverified_raw_price_basis")
            if stock.tick_size is None or stock.tick_size <= 0:
                raise ValueError("invalid_tick_size")
            o, h, l, c = [_ticks(getattr(stock, key), stock.tick_size)
                          for key in ("raw_open", "raw_high", "raw_low", "raw_close")]
            if l > min(o, c) or h < max(o, c) or l > h:
                raise ValueError("invalid_ohlc")
            if stock.raw_preclose is None or stock.raw_preclose <= 0:
                raise ValueError("missing_preclose")
            if stock.amount_yuan is None or stock.amount_yuan <= 0 or stock.volume_shares is None or stock.volume_shares <= 0:
                raise ValueError("missing_or_empty_turnover")
            if status_valid and stock.has_price_limit:
                upper = _ticks(stock.limit_up, stock.tick_size)
                lower = _ticks(stock.limit_down, stock.tick_size)
                if upper <= lower or h > upper or l < lower:
                    raise ValueError("price_outside_limits")
                up, touch, down = c == upper, h >= upper, c == lower
                one_up, one_down = o == h == l == c == upper, o == h == l == c == lower
                streak = (previous.limit_streak + 1 if previous and previous.limit_streak is not None
                          else None) if up else 0
            elif status_valid:
                streak = 0
            reference = Decimal(str(stock.raw_preclose))
            open_premium = float(Decimal(str(stock.raw_open)) / reference - 1)
            close_premium = float(Decimal(str(stock.raw_close)) / reference - 1)
            if not math.isfinite(open_premium) or not math.isfinite(close_premium):
                raise ValueError("invalid_premium")
            amount = stock.amount_yuan
            price_valid = True
        except ValueError as exc:
            up = touch = down = one_up = one_down = streak = None
            open_premium = close_premium = amount = None
            error = str(exc) if error is None else error
    return StockFeatures(
        code=stock.code, eligible=status_valid and price_valid and excluded is None,
        status_valid=status_valid, price_valid=price_valid, exclusion_reason=excluded, error=error,
        is_suspended=status_valid and stock.is_suspended is True,
        is_limit_up=up, is_touch_up=touch, is_limit_down=down,
        is_one_word_up=one_up, is_one_word_down=one_down, limit_streak=streak,
        open_premium=open_premium, close_premium=close_premium, amount_yuan=amount,
        evidence_mode=evidence_mode,
    )


def observation_stock(record, previous=None):
    day = record['trade_date']
    session = record.get('listing_session_observed')
    initial_listing = session is not None and 1 <= session <= 5 and record['listed_date_observed'] >= '2023-04-10'
    upper, lower = record.get('limit_up'), record.get('limit_down')
    provenance = record['limit_provenance']
    if initial_listing:
        upper, lower, provenance = None, None, 'observed_ipo_first_five_sessions'
    elif upper is None or lower is None:
        reference = record.get('raw_preclose')
        if reference is not None and reference > 0 and record['is_st'] is not None:
            rate = Decimal('.05') if record['is_st'] and day < '2026-07-06' else Decimal('.10')
            upper = float((Decimal(str(reference)) * (1 + rate)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
            lower = float((Decimal(str(reference)) * (1 - rate)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
            provenance = 'ordinary_rate_reconstruction_unverified_special_regime'
    stock = StockInput(code=record['symbol'][3:], price_basis='raw', source='joint_raw_observation',
        status_source='baostock.daily_observation', status_known_at=record.get('status_known_at'),
        is_st=record['is_st'], is_suspended=record['is_suspended'], is_delisting=record.get('is_delisting'),
        has_price_limit=False if initial_listing else (True if upper is not None and lower is not None else None),
        limit_up=upper, limit_down=lower, **{key: record.get(key) for key in (
            'raw_open', 'raw_high', 'raw_low', 'raw_close', 'raw_preclose', 'amount_yuan', 'volume_shares')})
    feature = stock_features(stock, day, previous, evidence_mode='daily_observation')
    if stock.is_suspended and (stock.volume_shares or 0) > 0 and (stock.amount_yuan or 0) > 0:
        feature = replace(feature, eligible=False, status_valid=False, is_suspended=False,
                          exclusion_reason=None, error='priced_suspension_conflict', limit_streak=None)
    return stock, feature, provenance
