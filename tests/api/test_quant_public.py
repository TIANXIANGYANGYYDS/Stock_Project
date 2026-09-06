from __future__ import annotations

import copy
import json

import pytest

from app.quant import public as contract
from app.services.quant_live_service import public_quant_document
from tests.api.conftest import sample_database
from tests.api.test_api import make_client


PRIVATE = "SECRET_MACD_ADX14_E2_THRESHOLD"
BASE = "/api/v1/quant"


def private_document():
    def record(**fields):
        return {"code": "000001", "name": "平安银行", "reason": PRIVATE,
                "adx_14": 35, "provisional_histogram": 0.12,
                "future_internal_field": {"secret": PRIVATE}, **fields}
    signal = record(signal_id=PRIVATE, action="buy", status="filled",
                    signal_at="2026-09-04T10:03:00+08:00", signal_price=10,
                    execution_at="2026-09-04T10:03:00+08:00", execution_price=10, shares=1000,
                    attempts=[{"reason": PRIVATE}], confirmation_count=3)
    signals = [signal,
               record(signal_id="rejected1", action="buy", status="rejected_adx", signal_at="2026-09-04T10:06:00+08:00"),
               record(signal_id="rejected2", action="buy", status="rejected_limit_up", signal_at="2026-09-04T10:09:00+08:00"),
               record(signal_id="pending", action="sell", status="deferred_limit_down")]
    buy = record(event_id=PRIVATE, action="buy", status="filled", notional=10000,
                 shares=1000, execution_price=10, commission=5, stamp_duty=0,
                 execution_at="2026-09-04T10:03:00+08:00", execution_price_source=PRIVATE)
    sell = record(event_id="sell", action="sell", status="filled", notional=12000,
                  shares=1000, execution_price=12, commission=5, stamp_duty=6,
                  execution_at="2026-09-04T10:30:00+08:00")
    holding = record(entry_event_id=PRIVATE, shares=1000, entry_notional=10000,
                     buy_commission=5, mark_price=11, market_value=11000,
                     total_pnl=995, total_return=995/10005, t1_locked=True, sellable_today=False)
    closed = record(entry_event_id=PRIVATE, exit_event_id="sell", entry_notional=10000,
                    exit_notional=12000, buy_commission=5, sell_commission=5, stamp_duty=6,
                    gross_pnl=2000, net_pnl=1984, net_return=1984/10005)
    observations = [record(state="confirming", action="buy", data_status="fresh"),
                    record(state="deferred_exit", action="hold", data_status="fresh"),
                    record(state="filled", action="buy", signal_id=PRIVATE, data_status="fresh")]
    return {
        "strategy_id": contract.PUBLIC_STRATEGIES["strategy_1"][0],
        "trade_date": "2026-09-04", "updated_at": "2026-09-04T15:00:00+08:00", "status": "closed",
        "strategy": {"name": PRIVATE, "version": PRIVATE, "macd_parameters": [20, 100, 30],
                     "buy_filter": {"indicator": "ADX", "period": 14, "minimum": 20, "note": PRIVATE}},
        "execution_rule": {"commission_rate": .0001, "settlement": "T+1", "secret": PRIVATE}, "exit_decisions": {"count": 1, "items": [record(action="hold", reason="持有中："+PRIVATE, adx=35, original_sell=False)]},
        "preselection_pool": {"count": 1, "items": [record(reference_price=10, status="watching")]}, "sell_candidate_pool": {"count": 1, "items": [record(reference_price=11)]},
        "_runtime_state": {"secret": PRIVATE, "accounts": [
            {"code": "000001", "name": "平安银行", "initial_cash": 100000, "cash": 89995, "realized_pnl": 0, "first_buy_at": "2026-09-04T10:03:00+08:00"},
            {"code": "000002", "name": "万科A", "initial_cash": 100000, "cash": 100100, "realized_pnl": 100, "first_buy_at": "2026-09-03T10:03:00+08:00"},
            {"code": "000003", "name": "未买入", "initial_cash": 100000, "cash": 100000, "realized_pnl": 0, "first_buy_at": None},
        ]}, "future_field": PRIVATE,
        "summary": {"initial_capital": 100000, "total_assets": 100123.45, "total_pnl": 123.45,
                    "total_return": .0012345, "holding_count": 1, "secret": PRIVATE},
        "recording": {"start_date": "2026-09-03", "mode": "historical_replay",
                      "computed_at": "2026-09-06T14:00:00+08:00", "strategy_version": PRIVATE},
        "runtime": {"version": 7, "evaluated_at": "2026-09-04T15:00:00+08:00",
                    "data_status": "closed_partial", "incomplete_code_count": 1, "incomplete_codes": ["000002"],
                    "observation_state_counts": {"confirming": 1, "deferred_exit": 1, "filled": 1},
                    "recent_signals": signals, "last_error": PRIVATE,
                    "source": {"note": PRIVATE}, "resource_limits": {"secret": PRIVATE}},
        **{key: {"count": len(items), "items": items} for key, items in (
            ("signals", signals), ("observation_pool", observations), ("intraday_trading", [buy, sell]),
            ("holding_pool", [holding]), ("closed_trades", [closed]),
        )},
    }


def database_with_document():
    database = sample_database()
    database["quant_daily_results"].rows.append(private_document())
    return database


def assert_public(payload):
    text = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in (PRIVATE.lower(), contract.STRATEGY_LABEL.lower(), contract.STRATEGY_ID,
                      "_runtime_state", "future_internal_field", "future_field"):
        assert forbidden not in text, forbidden



def test_every_quant_route_and_openapi_hide_real_identity_but_keep_details():
    db = database_with_document()
    before = copy.deepcopy(db["quant_daily_results"].rows)
    with make_client(db) as client:
        paths = [path for path in client.app.openapi()["paths"] if path.startswith(BASE)]
        assert len(paths) >= 20
        for path in paths:
            url = path.replace("{strategy_id}", "strategy_1").replace("{trade_date}", "2026-09-04")
            response = client.get(url)
            assert response.status_code == 200, (url, response.text)
            assert_public(response.json())
        assert_public(client.get("/openapi.json").json())
        assert client.get(f"{BASE}/strategies").json()["items"] == [
            {"id": "strategy_1", "name": "策略1", "execution_kind": "shadow_simulation"}]
    assert db["quant_daily_results"].rows == before
    full = public_quant_document(before[0])
    assert_public(full)
    assert full["signals"]["items"][0]["adx_14"] == 35
    assert full["signals"]["items"][0]["provisional_histogram"] == .12
    assert full["signals"]["items"][0]["confirmation_count"] == 3
    assert full["signals"]["items"][0]["attempts"][0]["reason"] == "策略1"
    assert full["signals"]["items"][0]["reason"] == "策略1"
    assert full["exit_decisions"]["items"][0]["reason"] == "持有中：策略1"
    assert full["runtime"]["last_error"] == "策略1"
    assert full["strategy"]["version"] == "策略1"
    assert full["strategy"]["macd_parameters"] == [20, 100, 30]
    assert full["strategy"]["buy_filter"] == {"indicator": "ADX", "period": 14, "minimum": 20, "note": "策略1"}
    assert full["execution_rule"]["commission_rate"] == .0001
    assert full["execution_rule"]["settlement"] == "T+1"
    assert full["preselection_pool"]["count"] == 1
    assert full["sell_candidate_pool"]["count"] == 1
    assert full["accounts"]["count"] == 2


def test_money_fees_and_ids_are_consistent_across_views():
    with make_client(database_with_document()) as client:
        overview = client.get(f"{BASE}/overview").json()["data"]
        full = client.get(f"{BASE}/daily-results/latest").json()["data"]
        executions = client.get(f"{BASE}/executions").json()
        holdings = client.get(f"{BASE}/holdings").json()
        signals = client.get(f"{BASE}/signals?status=filled").json()
        closed = client.get(f"{BASE}/closed-trades").json()
    summary = overview["summary"]
    assert summary["buy_notional"] == 10000
    assert summary["sell_notional"] == 12000
    assert summary["turnover"] == 22000
    assert summary["total_fees"] == 16
    assert summary["net_cash_flow"] == 1984
    assert summary["total_pnl"] == 123.45
    assert summary["total_return"] == .0012345
    assert overview["recording"]["mode"] == "historical_replay"
    assert overview["recording"]["execution_kind"] == "shadow_simulation"
    assert overview["recording"]["computed_at"] == "2026-09-06T14:00:00+08:00"
    assert executions["items"][0]["cash_flow"] == 11989
    buy = executions["items"][1]
    assert buy["cash_flow"] == -10005
    holding = holdings["items"][0]
    assert holding["cost_basis"] == 10005
    assert holding["entry_event_id"] == buy["event_id"] == closed["items"][0]["entry_event_id"]
    assert closed["items"][0]["total_fees"] == 16
    signal_id = signals["items"][0]["signal_id"]
    assert signal_id == full["observation_pool"]["items"][2]["signal_id"]
    assert signal_id == overview["signal_summary"]["recent_items"][0]["signal_id"]
    for response in (full, executions, holdings, signals, closed):
        assert response["snapshot_id"] == overview["snapshot_id"]
    assert full["summary"] == overview["summary"]


def test_public_status_filters_pagination_and_counts():
    with make_client(database_with_document()) as client:
        page = client.get(f"{BASE}/signals?status=rejected&page_size=1&page=2&code=000001").json()
        assert page["total"] == 2
        assert len(page["items"]) == 1
        assert page["items"][0]["status"] == "rejected"
        assert page["items"][0]["signal_at"] == "2026-09-04T10:06:00+08:00"
        pending = client.get(f"{BASE}/signals?status=pending_execution&action=sell").json()
        assert pending["total"] == 1
        counts = client.get(f"{BASE}/overview").json()["data"]["observation_summary"]["state_counts"]
        assert counts == {"watching": 1, "holding": 1, "filled": 1}
        holding = client.get(f"{BASE}/observations?state=holding&action=hold").json()
        assert holding["total"] == 1
        assert client.get(f"{BASE}/signals?status=rejected_adx").status_code == 422
        assert client.get(f"{BASE}/observations?state=confirming").status_code == 422
        assert client.get(f"{BASE}/signals?code=bad-code").status_code == 422
        assert client.get(f"{BASE}/signals?page_size=201").status_code == 422
        assert client.get(f"{BASE}/signals?page=0").status_code == 422
        assert client.get(f"{BASE}/signals?trade_date=2026-09-05").status_code == 404


@pytest.mark.parametrize("section,key", [("runtime", "last_valuation_at"), ("recording", "computed_at")])
def test_stale_snapshot_rejected_even_for_valuation_or_replay_update(section, key):
    db = database_with_document()
    with make_client(db) as client:
        old = client.get(f"{BASE}/overview").json()["data"]["snapshot_id"]
        assert client.get(f"{BASE}/holdings?snapshot_id={old}").status_code == 200
        db["quant_daily_results"].rows[0][section][key] = "2026-09-06T16:01:00+08:00"
        rejected = client.get(f"{BASE}/holdings?snapshot_id={old}")
        assert rejected.status_code == 409
        assert_public(rejected.json())
        new = client.get(f"{BASE}/overview").json()["data"]["snapshot_id"]
        assert new != old
        assert client.get(f"{BASE}/holdings?snapshot_id={new}").status_code == 200


def test_history_date_range_order_pagination_and_strategy_isolation(monkeypatch):
    monkeypatch.setitem(contract.PUBLIC_STRATEGIES, "strategy_2", ("private_other", "策略2"))
    db = database_with_document()
    for day in ("2026-09-03", "2026-09-06"):
        row = private_document()
        row["trade_date"] = day
        db["quant_daily_results"].rows.append(row)
    other = private_document()
    other["strategy_id"] = "private_other"
    other["summary"]["total_pnl"] = 999
    other["_runtime_state"]["accounts"][0]["cash"] = 42
    db["quant_daily_results"].rows.append(other)
    with make_client(db) as client:
        first = client.get(f"{BASE}/performance?start_date=2026-09-03&end_date=2026-09-04&page_size=1").json()
        second = client.get(f"{BASE}/performance?start_date=2026-09-03&end_date=2026-09-04&page_size=1&page=2").json()
        assert first["total"] == second["total"] == 2
        assert first["items"][0]["trade_date"] == "2026-09-03"
        assert second["items"][0]["trade_date"] == "2026-09-04"
        assert client.get(f"{BASE}/performance?start_date=2026-09-05&end_date=2026-09-05").json()["items"] == []
        assert client.get(f"{BASE}/performance?start_date=2026-09-05&end_date=2026-09-03").status_code == 422
        other = client.get(f"{BASE}/strategies/strategy_2/performance").json()
        assert other["total"] == 1
        assert other["items"][0]["summary"]["total_pnl"] == 999
        assert client.get(f"{BASE}/strategies/strategy_2/accounts?code=000001").json()["items"][0]["cash_balance"] == 42
        assert client.get(f"{BASE}/accounts?code=000001").json()["items"][0]["cash_balance"] == 89995
        assert first["items"][0]["summary"]["total_pnl"] == 123.45
        for path in ("does_not_exist", "private_other", contract.PUBLIC_STRATEGIES["strategy_1"][0]):
            assert client.get(f"{BASE}/strategies/{path}/overview").status_code == 404


def test_error_snapshot_and_unknown_states_do_not_invent_money_or_expose_details():
    db = database_with_document()
    db["quant_daily_results"].rows[0] = {
        "trade_date": "2026-09-04", "strategy_id": contract.PUBLIC_STRATEGIES["strategy_1"][0],
        "status": "error", "runtime": {"data_status": "error", "last_error": contract.STRATEGY_LABEL},
    }
    with make_client(db) as client:
        data = client.get(f"{BASE}/overview").json()["data"]
        assert data["runtime"]["data_status"] == "error"
        assert data["summary"]["total_assets"] is None
        assert data["summary"]["turnover"] is None
        assert data["recording"]["mode"] == "unknown"
        assert_public(data)
    assert contract.public_signal({"code": "000001", "status": PRIVATE}, "strategy_1").status == "unknown"
    assert contract.public_observation({"code": "000001", "state": PRIVATE}, "strategy_1").state == "unknown"


@pytest.mark.parametrize("change", ["missing_items", "incomplete_items", "missing_fee"])
def test_incomplete_execution_data_is_not_shown_as_zero(change):
    row = private_document()
    source = row["intraday_trading"]
    if change == "missing_items":
        source.pop("items")
    elif change == "incomplete_items":
        source["count"] += 1
    else:
        source["items"][0].pop("commission")
    summary = contract.public_summary(row, "strategy_1")
    assert summary.total_fees is None
    assert summary.net_cash_flow is None


def test_detailed_filters_augment_existing_generic_statuses():
    with make_client(database_with_document()) as client:
        rejected = client.get(f"{BASE}/signals?status_detail=rejected_adx").json()
        assert rejected["total"] == 1
        assert rejected["items"][0]["status"] == "rejected"
        assert rejected["items"][0]["status_detail"] == "rejected_adx"
        assert rejected["items"][0]["adx_14"] == 35
        assert client.get(f"{BASE}/signals?status=filled&status_detail=rejected_adx").json()["total"] == 0
        observations = client.get(f"{BASE}/observations?state_detail=deferred_exit").json()
        assert observations["total"] == 1
        assert observations["items"][0]["state"] == "holding"
        assert observations["items"][0]["state_detail"] == "deferred_exit"
        counts = client.get(f"{BASE}/overview").json()["data"]["observation_summary"]
        assert counts["detail_state_counts"] == {"confirming": 1, "deferred_exit": 1, "filled": 1}
        assert counts["state_counts"] == {"watching": 1, "holding": 1, "filled": 1}
        assert client.get(f"{BASE}/preselections?code=000001&status=watching").json()["total"] == 1
        assert client.get(f"{BASE}/sell-candidates?code=000002").json()["total"] == 0
        assert client.get(f"{BASE}/exit-decisions?action=hold").json()["items"][0]["adx"] == 35


def test_only_traded_accounts_including_closed_positions_are_valued_and_paginated():
    with make_client(database_with_document()) as client:
        overview = client.get(f"{BASE}/overview").json()["data"]
        page = client.get(f"{BASE}/accounts?page=1&page_size=1&snapshot_id={overview['snapshot_id']}").json()
        assert page["available"] is True
        assert page["total"] == 2
        row = page["items"][0]
        assert row["initial_capital"] == 100000
        assert row["cash_balance"] == 89995
        assert row["market_value"] == 11000
        assert row["total_assets"] == 100995
        assert row["total_pnl"] == 995
        assert row["unrealized_pnl"] == 995
        assert row["total_return"] == .00995
        assert row["has_position"] is True
        assert row["has_traded"] is True
        assert row["first_buy_at"] == "2026-09-04T10:03:00+08:00"
        assert client.get(f"{BASE}/accounts?code=000003").json()["total"] == 0
        empty_position = client.get(f"{BASE}/accounts?has_position=false").json()
        assert empty_position["total"] == 1
        row = empty_position["items"][0]
        assert row["code"] == "000002"
        assert row["market_value"] == row["unrealized_pnl"] == row["shares"] == 0
        assert row["realized_pnl"] == row["total_pnl"] == 100
        assert row["total_assets"] == 100100
        full = client.get(f"{BASE}/daily-results/latest").json()["data"]
        assert full["accounts"]["items"][1] == row
        assert client.get(f"{BASE}/accounts?snapshot_id=expired").status_code == 409


def test_accounts_missing_ledger_or_incomplete_holdings_do_not_invent_zeros():
    db = database_with_document()
    source = db["quant_daily_results"].rows[0]
    source["holding_pool"]["count"] = 2
    with make_client(db) as client:
        row = client.get(f"{BASE}/accounts?code=000002").json()["items"][0]
        assert row["cash_balance"] == 100100
        assert row["market_value"] is None
        assert row["has_position"] is None
        assert row["total_pnl"] is None
        source.pop("_runtime_state")
        response = client.get(f"{BASE}/accounts").json()
        assert response["available"] is False
        assert response["items"] == []


def test_anonymization_replaces_names_in_nested_text_without_erasing_reason_or_indicators():
    raw = private_document()
    raw["strategy"]["name"] = contract.STRATEGY_LABEL
    raw["signals"]["items"][0]["reason"] = f"{contract.STRATEGY_LABEL}：ADX14=35，连续3次满足；{contract.STRATEGY_ID}"
    raw["signals"]["items"][0]["attempts"] = [{"status": "deferred_t1", "reason": f"{contract.STRATEGY_LABEL}：T+1不可卖"}]
    row = public_quant_document(raw)["signals"]["items"][0]
    assert row["reason"] == "策略1：ADX14=35，连续3次满足；strategy_1"
    assert row["attempts"][0]["reason"] == "策略1：T+1不可卖"
    assert row["attempts"][0]["status"] == "deferred_t1"
    assert row["code"] == "000001"
    assert row["name"] == "平安银行"
    assert row["adx_14"] == 35


def test_public_trade_models_cover_producer_business_fields():
    from dataclasses import fields
    from app.quant.runtime import daily_flow
    for producer, consumer in (
        (daily_flow.SignalExecution, contract.Execution),
        (daily_flow.HoldingItem, contract.Holding),
        (daily_flow.ClosedTrade, contract.ClosedTrade),
        (daily_flow.PreselectionItem, contract.Candidate),
        (daily_flow.SellCandidateItem, contract.Candidate),
    ):
        assert {field.name for field in fields(producer)} <= set(consumer.model_fields)


def test_active_account_basis_has_consistent_snapshot_across_projected_endpoints():
    db = database_with_document()
    raw = db["quant_daily_results"].rows[0]
    raw["summary"]["return_basis"] = "traded_accounts_initial_capital"
    raw["recording"]["accounting_rebased_at"] = "2026-09-06T20:00:00+08:00"
    with make_client(db) as client:
        overview = client.get(f"{BASE}/overview").json()["data"]
        assert overview["schema_version"] == "1.2"
        snapshot = overview["snapshot_id"]
        for endpoint in ["accounts", "signals", "executions", "holdings", "exit-decisions"]:
            response = client.get(f"{BASE}/{endpoint}?snapshot_id={snapshot}")
            assert response.status_code == 200
            assert response.json()["snapshot_id"] == snapshot
        curve = client.get(f"{BASE}/performance").json()["items"]
        assert curve[0]["snapshot_id"] == snapshot
        raw["recording"]["accounting_rebased_at"] = "2026-09-06T20:01:00+08:00"
        assert client.get(f"{BASE}/accounts?snapshot_id={snapshot}").status_code == 409
