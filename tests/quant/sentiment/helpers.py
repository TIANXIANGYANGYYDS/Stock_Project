from __future__ import annotations

from app.quant.data.sentiment_inputs import MarketDay, StockInput


def stock(code: str = "600001", **kwargs) -> StockInput:
    values = dict(code=code, price_basis="raw", source="synthetic_fixture",
                  status_source="verified_fixture", status_known_at="2019-01-01T00:00:00+08:00",
                  is_st=False, is_suspended=False, is_delisting=False, has_price_limit=True,
                  raw_preclose=10.0, raw_open=10.0, raw_high=10.5, raw_low=9.8, raw_close=10.0,
                  limit_up=11.0, limit_down=9.0, amount_yuan=100_000_000.0, volume_shares=10_000_000.0)
    values.update(kwargs)
    return StockInput(**values)


def day(trade_date: str, stocks=(), expected=None) -> MarketDay:
    return MarketDay(trade_date, tuple(expected if expected is not None else [row.code for row in stocks]),
                     "historical_fixture_master", "2019-01-01T00:00:00+08:00", tuple(stocks))
