from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import (
    get_db,
    get_realtime_index_service,
    get_realtime_stock_crawler,
)
from app.api.serializers import serialize_mongo_value
from app.crawlers.realtime_market_crawler import RealtimeQuote
from app.quant import public as quant_public
from app.quant.runtime.daily_flow import (
    PreselectionItem,
    create_daily_flow,
    daily_flow_document,
)
from tests.api.conftest import sample_database


def make_client(database=None):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: database or sample_database()
    return TestClient(app)


def test_health_and_unavailable():
    database = sample_database()
    with make_client(database) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "mongodb": "ok"}
    database = sample_database()
    database.ping_ok = False
    with make_client(database) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "MongoDB 不可用"}


def test_news_filters_pagination_detail_and_404():
    with make_client() as client:
        response = client.get("/api/v1/news?page=1&page_size=1&source=cls&sector_name=半导体&company=甲公司")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["event_id"] == "e1"
        assert client.get("/api/v1/news/e1").json()["data"]["content"] == "半导体订单增长"
        assert client.get("/api/v1/news/missing").status_code == 404


def test_rankings_and_morning_analysis():
    with make_client() as client:
        assert client.get("/api/v1/news-rankings/latest").json()["data"]["snapshot_id"] == "s1"
        assert client.get("/api/v1/news-rankings/s1").status_code == 200
        report = client.get("/api/v1/morning-analyses/latest").json()["data"]
        assert report["analysis_date"] == "2026-08-05"
        assert "source_analysis_memos" in report
        listed = client.get("/api/v1/morning-analyses?page_size=1").json()["items"][0]
        assert "source_analysis_memos" not in listed


def test_stocks_and_sort_whitelist():
    with make_client() as client:
        stocks = client.get("/api/v1/stocks?page_size=1").json()
        assert stocks["total"] == 2
        assert stocks["items"][0]["latest_trade_date"] == "2026-08-05"
        daily = client.get("/api/v1/stocks/000001/daily/2026-08-05").json()["data"]
        assert daily["close"] == 10
        market = client.get("/api/v1/stock-daily/2026-08-05?sort_by=pct_chg&sort_order=desc").json()
        assert market["items"][0]["code"] == "000001"
        assert client.get("/api/v1/stock-daily/2026-08-05?sort_by=bad").status_code == 422


def test_latest_trade_date_and_empty_database():
    database = sample_database()
    database["daily_market_analysis"].rows.append(
        {
            "analysis_date": "2026-08-06",
            "status": "completed",
            "analysis": {"mainlines": []},
        }
    )
    with make_client(database) as client:
        response = client.get("/api/v1/market/latest-trade-date")
        assert response.status_code == 200
        assert response.json() == {
            "data": {
                "latest_trade_date": "2026-08-05",
                "latest_analysis_date": "2026-08-06",
            }
        }

    database["stock_daily_detail"].rows = []
    with make_client(database) as client:
        response = client.get("/api/v1/market/latest-trade-date")
        assert response.status_code == 200
        assert response.json() == {
            "data": {
                "latest_trade_date": None,
                "latest_analysis_date": "2026-08-06",
            }
        }


def test_realtime_indices_endpoint_uses_index_service() -> None:
    class FakeIndexService:
        async def fetch_latest(self):
            return {"market_status": "open", "items": [{"symbol": "000001.SH"}]}

    app = create_app()
    app.dependency_overrides[get_db] = lambda: sample_database()
    app.dependency_overrides[get_realtime_index_service] = lambda: FakeIndexService()
    with TestClient(app) as client:
        response = client.get("/api/v1/market/indices/realtime")
    assert response.status_code == 200
    assert response.json() == {
        "data": {"market_status": "open", "items": [{"symbol": "000001.SH"}]}
    }


def test_stock_intraday_returns_all_bars_in_time_order() -> None:
    with make_client() as client:
        response = client.get(
            "/api/v1/stocks/600519/intraday?trade_date=2026-08-05&interval=1m"
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["count"] == 2
    assert [item["timestamp"] for item in data["items"]] == [
        "2026-08-05T09:30:00+08:00",
        "2026-08-05T14:59:00+08:00",
    ]


def test_realtime_stock_endpoints_use_existing_crawler() -> None:
    cn_tz = timezone(timedelta(hours=8))

    class FakeStockCrawler:
        def __init__(self) -> None:
            self.calls = []

        async def fetch_quotes(self, codes):
            self.calls.append(list(codes))
            now = datetime(2026, 8, 11, 10, 0, tzinfo=cn_tz)
            quotes = [
                RealtimeQuote(
                    code=code,
                    name="贵州茅台" if code == "600519" else "平安银行",
                    market="SH" if code.startswith("6") else "SZ",
                    provider="TENCENT",
                    price=1346.48 if code == "600519" else 11.29,
                    volume=1000.0,
                    amount=10000.0,
                    market_data_time=now,
                    received_at=now,
                )
                for code in codes
            ]
            return quotes, {"requested": len(codes), "returned": len(quotes)}

    crawler = FakeStockCrawler()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: sample_database()
    app.dependency_overrides[get_realtime_stock_crawler] = lambda: crawler
    with TestClient(app) as client:
        batch = client.get("/api/v1/stocks/realtime?codes=600519,000001")
        single = client.get("/api/v1/stocks/600519/realtime")
        invalid = client.get("/api/v1/stocks/realtime?codes=not-a-code")
    assert batch.status_code == 200
    assert single.status_code == 200
    assert invalid.status_code == 422
    assert [item["code"] for item in batch.json()["data"]["items"]] == [
        "600519",
        "000001",
    ]
    assert single.json()["data"]["items"][0]["price"] == 1346.48
    assert crawler.calls == [["600519", "000001"], ["600519"]]


def test_creator_accounts_works_and_opinions():
    with make_client() as client:
        account = client.get("/api/v1/creator-accounts/douyin%3A203775400")
        assert account.status_code == 200
        assert "sec_uid" not in account.json()["data"]
        work = client.get("/api/v1/creator-works/douyin%3Aw1").json()["data"]
        assert work["work_key"] == "douyin:w1"
        assert "source_text" in work
        listed_work = client.get("/api/v1/creator-works?page_size=1").json()["items"][0]
        assert "source_text" not in listed_work
        opinion = client.get("/api/v1/creator-opinion-analyses/c1").json()["data"]
        assert opinion["creator_id"] == "c1"


def test_stats_and_serialization():
    with make_client() as client:
        stats = client.get("/api/v1/stats").json()
        assert stats["news"]["total"] == 2
        assert stats["stocks"]["stock_count"] == 2
    value = serialize_mongo_value({"_id": "hidden", "nested": {"when": __import__("datetime").datetime(2026, 1, 1)}})
    assert "_id" not in value
    assert value["nested"]["when"] == "2026-01-01T00:00:00"


def test_quant_daily_result_latest_date_and_404() -> None:
    database = sample_database()
    flow = create_daily_flow(
        trade_date="2026-08-06",
        selection_date="2026-08-05",
        generated_at="2026-08-05T15:30:00+08:00",
        candidates=[
            PreselectionItem(
                code="600176",
                name="中国巨石",
                reason="MACD 绿柱谷底确认",
                reference_price=10.0,
            )
        ],
    )
    database["quant_daily_results"].rows.append(daily_flow_document(flow))

    with make_client(database) as client:
        latest = client.get("/api/v1/quant/daily-results/latest")
        by_date = client.get("/api/v1/quant/daily-results/2026-08-06")
        missing = client.get("/api/v1/quant/daily-results/2026-08-07")

    assert latest.status_code == 200
    assert latest.json()["data"]["summary"]["preselection_count"] == 1
    assert by_date.status_code == 200
    assert by_date.json()["data"]["trade_date"] == "2026-08-06"
    assert missing.status_code == 404


def test_quant_live_summary_signals_and_observations() -> None:
    database = sample_database()
    database["quant_daily_results"].rows.append(
        {
            "schema_version": "2.0",
            "strategy_id": "provisional_daily_macd_3m_v1",
            "trade_date": "2026-09-03",
            "selection_date": "2026-09-02",
            "status": "monitoring",
            "strategy": {"id": "provisional_daily_macd_3m_v1"},
            "runtime": {
                "version": 3,
                "data_status": "fresh",
                "observation_state_counts": {
                    "confirming": 1,
                    "watching": 1,
                },
                "recent_signals": [
                    {
                        "signal_id": "buy-1",
                        "code": "000001",
                        "action": "buy",
                        "status": "filled",
                    },
                    {
                        "signal_id": "sell-1",
                        "code": "000002",
                        "action": "sell",
                        "status": "pending_execution",
                    },
                ],
            },
            "summary": {"signal_count": 2},
            "observation_pool": {
                "count": 2,
                "items": [
                    {
                        "code": "000001",
                        "action": "buy",
                        "state": "confirming",
                    },
                    {
                        "code": "000002",
                        "action": "sell",
                        "state": "watching",
                    },
                ],
            },
            "signals": {
                "count": 2,
                "items": [
                    {
                        "signal_id": "buy-1",
                        "code": "000001",
                        "action": "buy",
                        "status": "filled",
                    },
                    {
                        "signal_id": "sell-1",
                        "code": "000002",
                        "action": "sell",
                        "status": "pending_execution",
                    },
                ],
            },
            "_runtime_state": {"opening_flow": {"private": True}},
        }
    )

    with make_client(database) as client:
        summary = client.get("/api/v1/quant/intraday/latest")
        signals = client.get(
            "/api/v1/quant/signals?trade_date=2026-09-03&action=buy"
        )
        observations = client.get(
            "/api/v1/quant/observations?trade_date=2026-09-03&state=watching&action=buy"
        )
        daily = client.get("/api/v1/quant/daily-results/2026-09-03")

    assert summary.status_code == 200
    assert summary.json()["data"]["runtime"]["version"] == 3
    assert summary.json()["data"]["observation_summary"]["state_counts"] == {
        "watching": 2,
    }
    assert signals.json()["total"] == 1
    assert signals.json()["items"][0]["signal_id"].startswith("sig_")
    assert observations.json()["total"] == 1
    assert observations.json()["items"][0]["code"] == "000001"
    assert "_runtime_state" not in daily.json()["data"]


def test_quant_strategy_routes_keep_same_day_pools_isolated(monkeypatch) -> None:
    monkeypatch.setattr(quant_public, "PUBLIC_STRATEGIES", {
        "strategy_1": ("strategy_a", "策略1"), "strategy_2": ("strategy_b", "策略2"),
    })
    database = sample_database()
    for strategy_id, code in (("strategy_a", "000001"), ("strategy_b", "000002")):
        database["quant_daily_results"].rows.append(
            {
                "schema_version": "2.1",
                "strategy_id": strategy_id,
                "trade_date": "2026-09-03",
                "selection_date": "2026-09-02",
                "status": "monitoring",
                "strategy": {"id": strategy_id, "name": strategy_id},
                "runtime": {
                    "version": 1,
                    "data_status": "fresh",
                    "observation_state_counts": {"watching": 1},
                    "recent_signals": [],
                },
                "summary": {"holding_count": 1},
                "observation_pool": {
                    "count": 1,
                    "items": [{"code": code, "state": "watching"}],
                },
                "signals": {"count": 0, "items": []},
                "intraday_trading": {
                    "count": 1,
                    "items": [{"code": code, "action": "buy", "status": "filled"}],
                },
                "holding_pool": {
                    "count": 1,
                    "items": [{"code": code, "total_pnl": 100.0}],
                },
                "closed_trades": {"count": 0, "items": []},
                "_runtime_state": {"private": True},
            }
        )

    base = "/api/v1/quant/strategies/strategy_2"
    with make_client(database) as client:
        intraday = client.get(f"{base}/intraday/latest")
        observations = client.get(f"{base}/observations")
        executions = client.get(f"{base}/executions")
        holdings = client.get(f"{base}/holdings")
        daily = client.get(f"{base}/daily-results/2026-09-03")

    assert intraday.status_code == 200
    assert intraday.json()["data"]["strategy_id"] == "strategy_2"
    assert observations.json()["strategy_id"] == "strategy_2"
    assert observations.json()["items"][0]["code"] == "000002"
    assert executions.json()["items"][0]["code"] == "000002"
    assert holdings.json()["items"][0]["code"] == "000002"
    assert holdings.json()["items"][0]["total_pnl"] == 100.0
    assert daily.json()["data"]["strategy"]["id"] == "strategy_2"
    assert "_runtime_state" not in daily.json()["data"]
