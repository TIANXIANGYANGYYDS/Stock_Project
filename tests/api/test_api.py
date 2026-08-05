from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_db
from app.api.serializers import serialize_mongo_value
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
