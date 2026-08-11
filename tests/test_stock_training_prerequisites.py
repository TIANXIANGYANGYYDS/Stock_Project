from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.manually_execute_script.import_stock_daily_status_csv import (
    csv_status_document,
)
from app.manually_execute_script.stock_history_common import StockTarget
from app.manually_execute_script.sync_stock_adjust_factors import (
    FactorAnchor,
    effective_sina_factor,
    extend_adjustment_factor,
    parse_sina_adjustment_events,
    preclose_factor_groups,
)
from app.manually_execute_script.sync_stock_daily_status import (
    baostock_status_document,
    bse_price_limits,
    bse_st_state,
    resolve_missing_st_rows,
    reference_price_from_pct,
    sh_sz_price_limits,
)


def _csv_row(**overrides: str) -> dict[str, str]:
    row = {
        "股票代码": "000001.SZ",
        "股票名称": "平安银行",
        "交易日": "20251231",
        "开盘价": "11.48",
        "最高价": "11.49",
        "最低价": "11.40",
        "收盘价": "11.41",
        "当日涨停价": "12.63",
        "当日跌停价": "10.33",
    }
    row.update(overrides)
    return row


def test_csv_status_keeps_direct_limits() -> None:
    document = csv_status_document(
        Path("000001.SZ.csv"),
        _csv_row(),
        start_date=date(2015, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert document is not None
    assert document["is_suspended"] is False
    assert document["is_st"] is None
    assert document["limit_up"] == 12.63
    assert document["limit_down"] == 10.33
    assert document["has_price_limit"] is True


def test_csv_status_keeps_blank_ohlc_as_suspension() -> None:
    document = csv_status_document(
        Path("000001.SZ.csv"),
        _csv_row(开盘价="", 最高价="", 最低价="", 收盘价=""),
        start_date=date(2015, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert document is not None
    assert document["is_suspended"] is True
    assert document["limit_up"] == 12.63


def test_csv_status_normalizes_no_limit_sentinels() -> None:
    document = csv_status_document(
        Path("000001.SZ.csv"),
        _csv_row(当日涨停价="99999.99", 当日跌停价="0"),
        start_date=date(2015, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert document is not None
    assert document["limit_up"] is None
    assert document["limit_down"] is None
    assert document["has_price_limit"] is False


def test_sina_factor_parser_and_local_anchor_extension() -> None:
    events = parse_sina_adjustment_events(
        'var sh600000qfq={"data":['
        '{"d":"2026-07-16","f":"1.0"},'
        '{"d":"2025-07-16","f":"1.0472440944882"},'
        '{"d":"1900-01-01","f":"2.0"}]}; /* comment */'
    )
    anchor = FactorAnchor(
        anchor_date=date(2025, 12, 31),
        factor=Decimal("16.5935"),
        method="local_history_anchor",
    )

    assert effective_sina_factor(events, date(2026, 7, 15)) == Decimal(
        "1.0472440944882"
    )
    assert extend_adjustment_factor(
        anchor,
        date(2026, 7, 15),
        events,
    ) == Decimal("16.5935")
    assert extend_adjustment_factor(
        anchor,
        date(2026, 7, 16),
        events,
    ) > Decimal("16.5935")


def test_preclose_fallback_chains_exchange_reference_prices() -> None:
    class Cursor(list):
        def sort(self, *_args):
            return self

    class DailyCollection:
        @staticmethod
        def find_one(filters, projection):
            assert filters == {"code": "689009", "trade_date": "2025-12-31"}
            assert projection == {"_id": 0, "close": 1}
            return {"close": 10.0}

        @staticmethod
        def find(filters, projection):
            assert filters["code"] == "689009"
            assert projection == {"_id": 0, "trade_date": 1, "close": 1}
            return Cursor(
                [
                    {"trade_date": "2026-01-02", "close": 9.0},
                    {"trade_date": "2026-01-05", "close": 9.5},
                ]
            )

    class StatusCollection:
        @staticmethod
        def find_one(filters, projection):
            assert projection == {"_id": 0, "preclose": 1}
            return {
                "preclose": {
                    "2026-01-02": 8.0,
                    "2026-01-05": 9.0,
                }[filters["trade_date"]]
            }

    groups = preclose_factor_groups(
        DailyCollection(),
        StatusCollection(),
        code="689009",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 5),
        anchor=FactorAnchor(
            anchor_date=date(2025, 12, 31),
            factor=Decimal("2"),
            method="local_history_anchor",
        ),
    )

    assert groups == [("2026-01-02", "2026-01-05", 2.5)]


def test_sh_sz_price_limits_use_half_up_and_2026_st_rule_change() -> None:
    assert sh_sz_price_limits(
        Decimal("10.00"),
        code="600000",
        trade_date=date(2026, 7, 3),
        is_st=True,
    ) == (10.5, 9.5)
    assert sh_sz_price_limits(
        Decimal("10.00"),
        code="600000",
        trade_date=date(2026, 7, 6),
        is_st=True,
    ) == (11.0, 9.0)
    assert sh_sz_price_limits(
        Decimal("11.48"),
        code="000001",
        trade_date=date(2026, 1, 5),
        is_st=False,
    ) == (12.63, 10.33)


def test_bse_limits_floor_the_symmetric_limit_amount() -> None:
    assert bse_price_limits(Decimal("12.16")) == (15.8, 8.52)


def test_reference_price_is_recovered_from_four_decimal_pct_change() -> None:
    assert reference_price_from_pct(12.07, -0.7401) == Decimal("12.16")


def test_bse_st_period_boundaries() -> None:
    assert bse_st_state("920305", date(2025, 5, 5))[0] is False
    assert bse_st_state("920305", date(2025, 5, 6))[0] is True
    assert bse_st_state("920680", date(2025, 12, 10))[0] is True
    assert bse_st_state("920680", date(2025, 12, 11))[0] is False
    assert bse_st_state("920090", date(2026, 4, 24))[0] is True


def test_missing_st_rows_use_previous_then_following_known_state() -> None:
    rows = [
        {"trade_date": "2025-01-02", "is_st": None, "name": "测试"},
        {"trade_date": "2025-01-03", "is_st": True, "name": "*ST测试"},
        {"trade_date": "2025-01-06", "is_st": None, "name": "*ST测试"},
    ]

    resolved = resolve_missing_st_rows(rows, previous_state=None)

    assert [
        (row["trade_date"], row["is_st"], row["anchor_date"])
        for row in resolved
    ] == [
        ("2025-01-02", True, "2025-01-03"),
        ("2025-01-06", True, "2025-01-03"),
    ]


def test_missing_st_rows_fall_back_to_explicit_name_prefix() -> None:
    resolved = resolve_missing_st_rows(
        [{"trade_date": "2018-08-16", "is_st": None, "name": "退长油(退)"}],
        previous_state=None,
    )

    assert resolved[0]["is_st"] is False
    assert resolved[0]["anchor_date"] is None


def test_baostock_status_calculates_limit_and_preserves_suspension() -> None:
    target = StockTarget(code="600000", name="浦发银行", market="SH")
    traded = baostock_status_document(
        target,
        {
            "date": "2026-07-06",
            "preclose": "10.00",
            "tradestatus": "1",
            "isST": "1",
        },
        first_trade_date=date(1999, 11, 10),
        listed_trade_number=999,
    )
    suspended = baostock_status_document(
        target,
        {
            "date": "2026-07-07",
            "preclose": "10.00",
            "tradestatus": "0",
            "isST": "1",
        },
        first_trade_date=date(1999, 11, 10),
        listed_trade_number=999,
    )

    assert traded["limit_up"] == 11.0
    assert traded["is_st"] is True
    assert suspended["is_suspended"] is True
    assert suspended["has_price_limit"] is False


def test_baostock_status_marks_first_five_trades_as_no_limit() -> None:
    document = baostock_status_document(
        StockTarget(code="001400", name="测试新股", market="SZ"),
        {
            "date": "2026-08-03",
            "preclose": "10.00",
            "tradestatus": "1",
            "isST": "0",
        },
        first_trade_date=date(2026, 8, 3),
        listed_trade_number=1,
    )

    assert document["has_price_limit"] is False
    assert document["limit_up"] is None
