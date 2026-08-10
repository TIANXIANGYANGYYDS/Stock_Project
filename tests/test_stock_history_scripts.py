from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.manually_execute_script.stock_history_common import (
    BaoStockProxySession,
    BaoStockTunnelSocket,
    StockTarget,
    baostock_bar_timestamp,
    fill_missing_target_names,
    five_years_before,
    insert_missing_documents,
    keep_targets_without_data,
    market_for_code,
    open_http_connect_tunnel,
)
from app.manually_execute_script.import_stock_daily_csv import (
    SOURCE_NAME,
    csv_daily_document,
    plan_operation,
    source_files,
)
from app.manually_execute_script.import_stock_15m_zip import (
    SOURCE_NAME as ZIP_15M_SOURCE_NAME,
    csv_15m_document,
    plan_insert,
    resolve_bj_codes,
)
from app.manually_execute_script.sync_a_stock_15m_bars import (
    build_argument_parser as build_minute_argument_parser,
    iter_baostock_15m_documents,
    iter_bse_15m_documents,
    load_database_targets,
    minute_document,
    plan_missing_ranges,
    shard_targets,
)
from app.manually_execute_script.sync_a_stock_daily_bars import daily_document


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600519", "SH"),
        ("000001", "SZ"),
        ("300750", "SZ"),
        ("920799", "BJ"),
        ("830799", "BJ"),
        ("430047", "BJ"),
    ],
)
def test_market_for_code_covers_current_a_share_markets(
    code: str,
    market: str,
) -> None:
    assert market_for_code(code) == market


def test_five_years_before_handles_leap_day() -> None:
    assert five_years_before(date(2024, 2, 29)) == date(2019, 2, 28)


def test_baostock_timestamp_uses_15_minute_bar_end_time() -> None:
    assert (
        baostock_bar_timestamp("20210802094500000")
        == "2021-08-02T09:45:00+08:00"
    )


def test_http_connect_tunnel_requires_success_status(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.timeout = None
            self.sent = b""
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def sendall(self, value):
            self.sent += value

        @staticmethod
        def recv(_size):
            return b"HTTP/1.1 200 Connection established\r\n\r\n"

        def close(self):
            self.closed = True

    tunnel = FakeSocket()
    monkeypatch.setattr(
        "app.manually_execute_script.stock_history_common.socket.create_connection",
        lambda address, timeout: tunnel,
    )

    result = open_http_connect_tunnel(
        "http://127.0.0.1:8080",
        target_host="public-api.baostock.com",
        target_port=10030,
        connect_timeout=10,
        socket_timeout=90,
    )

    assert result is tunnel
    assert tunnel.timeout == 90
    assert b"CONNECT public-api.baostock.com:10030" in tunnel.sent


def test_baostock_proxy_session_installs_tunnel_for_login(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self):
            self.closed = True

    class FakeProvider:
        def __init__(self) -> None:
            self.successes = 0
            self.failures = 0

        @staticmethod
        def get_requests_proxies():
            return {"http": "http://127.0.0.1:8080"}

        def on_success(self):
            self.successes += 1

        def on_failure(self, _exc):
            self.failures += 1

    tunnel = FakeSocket()
    provider = FakeProvider()

    class FakeBaoStock:
        @staticmethod
        def login():
            import baostock.common.context as context
            import baostock.util.socketutil as socketutil

            socketutil.SocketUtil().connect()
            assert context.default_socket.raw_socket is tunnel
            return SimpleNamespace(error_code="0", error_msg="success")

    monkeypatch.setattr(
        "app.manually_execute_script.stock_history_common.open_http_connect_tunnel",
        lambda *args, **kwargs: tunnel,
    )
    session = BaoStockProxySession(
        FakeBaoStock(),
        proxy_provider=provider,
        login_attempts=1,
    )

    session.ensure_login()
    session.note_query()
    session.close()

    assert provider.successes == 1
    assert provider.failures == 0
    assert tunnel.closed is True


def test_baostock_tunnel_socket_raises_on_proxy_eof() -> None:
    class ClosedSocket:
        @staticmethod
        def recv(_size):
            return b""

    tunnel = BaoStockTunnelSocket(ClosedSocket())

    with pytest.raises(ConnectionError, match="代理连接已关闭"):
        tunnel.recv(8192)
    assert isinstance(tunnel.failure, ConnectionError)


def test_baostock_iterator_rejects_silent_pagination_disconnect(
    monkeypatch,
) -> None:
    import baostock.common.context as context

    class FakeResult:
        error_code = "0"
        error_msg = "success"

        @staticmethod
        def next():
            return False

    class FakeBaoStock:
        @staticmethod
        def query_history_k_data_plus(*args, **kwargs):
            return FakeResult()

    monkeypatch.setattr(
        context,
        "default_socket",
        SimpleNamespace(failure=ConnectionError("proxy eof")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="分页连接失败"):
        list(
            iter_baostock_15m_documents(
                FakeBaoStock(),
                StockTarget(code="000001", name="平安银行", market="SZ"),
                start_date=date(2025, 11, 19),
                end_date=date(2026, 8, 7),
            )
        )


def test_daily_document_normalizes_numeric_fields() -> None:
    target = StockTarget(code="600519", name="贵州茅台", market="SH")

    document = daily_document(
        target,
        {
            "date": "2026-08-07",
            "open": "1500.1",
            "high": "1510.2",
            "low": "1490.3",
            "close": "1505.4",
            "volume": "1200",
            "amount": "1800000",
        },
        source="test",
    )

    assert document["trade_date_int"] == 20260807
    assert document["close"] == 1505.4
    assert document["volume_unit"] == "share"
    assert document["adjust"] == ""


def test_minute_document_uses_timestamp_trade_date() -> None:
    target = StockTarget(code="000001", name="平安银行", market="SZ")

    document = minute_document(
        target,
        {
            "open": "10",
            "high": "11",
            "low": "9",
            "close": "10.5",
            "volume": "100",
            "amount": "1050",
        },
        timestamp="2026-08-07T09:45:00+08:00",
        source="test",
    )

    assert document["trade_date"] == "2026-08-07"
    assert document["timestamp"] == "2026-08-07T09:45:00+08:00"


def test_minute_parser_accepts_market_filter() -> None:
    args = build_minute_argument_parser().parse_args(["--market", "BJ"])

    assert args.market == "BJ"

    args = build_minute_argument_parser().parse_args(["--market", "HS"])

    assert args.market == "HS"


def test_bse_minute_keeps_source_earliest_instead_of_five_year_cutoff() -> None:
    class FakeSina:
        @staticmethod
        def fetch_rows(**kwargs):
            assert kwargs == {"code": "920799"}
            return [
                {
                    "day": "2024-10-29 14:45:00",
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "volume": "100",
                    "amount": "1050",
                },
                {
                    "day": "2026-08-08 09:45:00",
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "volume": "100",
                    "amount": "1050",
                },
            ]

    documents = list(
        iter_bse_15m_documents(
            FakeSina(),
            None,
            StockTarget(code="920799", name="艾融软件", market="BJ"),
            end_date=date(2026, 8, 7),
        )
    )

    assert [item["timestamp"] for item in documents] == [
        "2024-10-29T14:45:00+08:00"
    ]


def test_bse_minute_falls_back_to_eastmoney() -> None:
    class FailingSina:
        @staticmethod
        def fetch_rows(**kwargs):
            raise IndexError("list index out of range")

    class FakeEastMoney:
        @staticmethod
        def fetch_rows(**kwargs):
            assert kwargs["interval"] == "15m"
            return [
                {
                    "time": "2026-08-07 09:45",
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10.5",
                    "volume": "100",
                    "amount": "1050",
                }
            ]

    documents = list(
        iter_bse_15m_documents(
            FailingSina(),
            FakeEastMoney(),
            StockTarget(code="920821", name="凯添燃气", market="BJ"),
            end_date=date(2026, 8, 7),
        )
    )

    assert documents[0]["source"] == "eastmoney.quote_api.proxy"
    assert documents[0]["timestamp"] == "2026-08-07T09:45:00+08:00"


def test_keep_targets_without_data_filters_existing_codes() -> None:
    class FakeCollection:
        @staticmethod
        def distinct(field, filters):
            assert field == "code"
            assert set(filters["code"]["$in"]) == {"000001", "920821"}
            return ["000001"]

    targets = [
        StockTarget(code="000001", name="平安银行", market="SZ"),
        StockTarget(code="920821", name="凯添燃气", market="BJ"),
    ]

    assert keep_targets_without_data(FakeCollection(), targets) == [targets[1]]


def test_load_database_targets_uses_requested_daily_snapshot() -> None:
    class FakeCollection:
        @staticmethod
        def find(filters, projection):
            assert filters == {
                "trade_date": "2026-08-07",
                "adjust": "qfq",
            }
            assert projection == {"_id": 0, "code": 1, "name": 1}
            return [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "000001", "name": "平安银行"},
            ]

    targets = load_database_targets(
        {"stock_daily_detail": FakeCollection()},
        snapshot_date=date(2026, 8, 7),
        only_code=None,
        offset=0,
        limit=None,
    )

    assert [target.code for target in targets] == ["000001", "600519"]
    assert [target.market for target in targets] == ["SZ", "SH"]


def test_shard_targets_are_disjoint_and_complete() -> None:
    targets = [
        StockTarget(code=str(index).zfill(6), name=None, market="SZ")
        for index in range(10)
    ]

    shards = [
        shard_targets(targets, shard_count=3, shard_index=index)
        for index in range(3)
    ]

    assert [target.code for target in shards[0]] == [
        "000000",
        "000003",
        "000006",
        "000009",
    ]
    assert sorted(target.code for shard in shards for target in shard) == [
        target.code for target in targets
    ]


def test_plan_missing_ranges_resumes_from_latest_stored_day() -> None:
    class FakeCollection:
        @staticmethod
        def find_one(filters, projection, sort):
            assert projection == {"_id": 0, "timestamp": 1}
            assert sort == [("timestamp", -1)]
            if filters["code"] == "000001":
                return {"timestamp": "2025-11-19T15:00:00+08:00"}
            if filters["code"] == "600519":
                return {"timestamp": "2026-08-07T15:00:00+08:00"}
            return None

    targets = [
        StockTarget(code="000001", name="平安银行", market="SZ"),
        StockTarget(code="600519", name="贵州茅台", market="SH"),
        StockTarget(code="601112", name="华翔股份", market="SH"),
    ]

    planned = plan_missing_ranges(
        FakeCollection(),
        targets,
        default_start_date=date(2021, 8, 7),
        end_date=date(2026, 8, 7),
    )

    assert planned == [
        (targets[0], date(2025, 11, 19)),
        (targets[2], date(2021, 8, 7)),
    ]


def test_insert_missing_documents_only_uses_set_on_insert() -> None:
    class FakeResult:
        upserted_count = 1

    class FakeCollection:
        def __init__(self) -> None:
            self.operations = []

        def bulk_write(self, operations, ordered):
            assert ordered is False
            self.operations.extend(operations)
            return FakeResult()

    collection = FakeCollection()
    stats = insert_missing_documents(
        collection,
        [
            {
                "code": "000001",
                "timestamp": "2026-08-07T09:45:00+08:00",
                "close": 10.5,
            }
        ],
        key_fields=("code", "timestamp"),
        batch_size=1000,
    )

    operation = collection.operations[0]
    assert stats.rows == 1
    assert stats.affected == 1
    assert "$set" not in operation._doc
    assert operation._doc["$setOnInsert"]["close"] == 10.5


def test_fill_missing_target_names_uses_reference_collection() -> None:
    class FakeCollection:
        @staticmethod
        def find_one(filters, projection):
            assert filters == {
                "code": "600519",
                "name": {"$nin": [None, ""]},
            }
            assert projection == {"_id": 0, "name": 1}
            return {"name": "贵州茅台"}

    targets = [StockTarget(code="600519", name=None, market="SH")]

    assert fill_missing_target_names(FakeCollection(), targets) == [
        StockTarget(code="600519", name="贵州茅台", market="SH")
    ]


def test_csv_daily_document_converts_units_and_keeps_raw_prices() -> None:
    document = csv_daily_document(
        Path("000001.SZ.csv"),
        {
            "股票代码": "000001.SZ",
            "股票名称": "平安银行",
            "交易日": "20251231",
            "开盘价": "11.48",
            "最高价": "11.49",
            "最低价": "11.40",
            "收盘价": "11.41",
            "成交量（手）": "590620.37",
            "成交额（千元）": "675457.357",
            "复权因子": "134.5794",
        },
        start_date=date(2015, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert document is not None
    assert document["volume"] == 59062037.0
    assert document["amount"] == 675457357.0
    assert document["adj_factor"] == 134.5794
    assert document["adjust"] == ""


def test_csv_daily_document_skips_blank_or_out_of_range_rows() -> None:
    row = {
        "股票代码": "000001.SZ",
        "股票名称": "平安银行",
        "交易日": "20141231",
        "开盘价": "10",
        "最高价": "11",
        "最低价": "9",
        "收盘价": "10.5",
        "成交量（手）": "100",
        "成交额（千元）": "105",
        "复权因子": "1",
    }

    assert (
        csv_daily_document(
            Path("000001.SZ.csv"),
            row,
            start_date=date(2015, 1, 1),
            end_date=date(2025, 12, 31),
        )
        is None
    )
    row["交易日"] = "20150105"
    row["开盘价"] = ""
    assert (
        csv_daily_document(
            Path("000001.SZ.csv"),
            row,
            start_date=date(2015, 1, 1),
            end_date=date(2025, 12, 31),
        )
        is None
    )


def test_plan_operation_does_not_overwrite_existing_prices() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    document = {
        "code": "000001",
        "trade_date": "2025-12-31",
        "close": 11.41,
        "adj_factor": 134.5794,
        "adj_factor_source": SOURCE_NAME,
    }

    action, operation = plan_operation(
        document,
        {"2025-12-31": None},
        now=now,
    )

    assert action == "factor"
    assert operation is not None
    assert operation._doc == {
        "$set": {
            "adj_factor": 134.5794,
            "adj_factor_source": SOURCE_NAME,
        }
    }
    assert "close" not in operation._doc["$set"]


def test_source_files_skips_nonstandard_symbols(tmp_path: Path) -> None:
    (tmp_path / "000001.SZ.csv").touch()
    (tmp_path / "600519.SH.csv").touch()
    (tmp_path / "T00018.SH.csv").touch()

    files, skipped = source_files(tmp_path, market=None, only_code=None)

    assert [path.name for path in files] == ["000001.SZ.csv", "600519.SH.csv"]
    assert [path.name for path in skipped] == ["T00018.SH.csv"]


def test_csv_15m_document_converts_volume_and_bj_code() -> None:
    document = csv_15m_document(
        {
            "时间": "2025-07-17 09:45:00",
            "代码": "bj873806",
            "名称": "云星宇",
            "开盘价": "15.95",
            "收盘价": "15.95",
            "最高价": "15.96",
            "最低价": "15.80",
            "成交量": "2969",
            "成交额": "4717308",
        },
        source_code="873806",
        target_code="920806",
        market="BJ",
        year=2025,
    )

    assert document["code"] == "920806"
    assert document["source_symbol"] == "bj873806"
    assert document["timestamp"] == "2025-07-17T09:45:00+08:00"
    assert document["volume"] == 296900.0
    assert document["amount"] == 4717308.0
    assert document["adjust"] == ""
    assert document["source"] == ZIP_15M_SOURCE_NAME


def test_resolve_bj_codes_prefers_name_and_uses_safe_suffix_fallback() -> None:
    resolved = resolve_bj_codes(
        {
            "873152": "天宏锂电",
            "430090": "同辉信息",
        },
        {
            ("920252", "天宏锂电"),
            ("920090", "*ST同辉"),
        },
    )

    assert resolved == {
        "873152": "920252",
        "430090": "920090",
    }


def test_plan_15m_insert_never_overwrites_existing_timestamp() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    document = {
        "code": "000001",
        "timestamp": "2025-01-02T09:45:00+08:00",
        "close": 11.74,
    }
    existing = {"2025-01-02T09:45:00+08:00"}

    assert plan_insert(document, existing, now=now) is None

    document["timestamp"] = "2025-01-02T10:00:00+08:00"
    operation = plan_insert(document, existing, now=now)

    assert operation is not None
    assert operation._doc["close"] == 11.74
    assert operation._doc["created_at"] == now
    assert "2025-01-02T10:00:00+08:00" in existing
