from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from function.api_client import ORDER_SUBMIT_PATH, preview_submit_alert_handler, request
from function.trade_contract import expect_trade_problem, funding_snapshot, verify_funding_plan


def test_funding_snapshot_keeps_lock_as_boolean():
    def reply(_):
        return httpx.Response(200, json={
            "cash_degraded": False, "credit_degraded": False,
            "cash": "2000000", "credit_available": "3000000", "credit_in_position": "0", "credit_locked": False,
        })

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(reply)) as client:
        snapshot = funding_snapshot(client)
    assert snapshot == {"cash": 2_000_000, "credit_available": 3_000_000, "credit_in_position": 0, "credit_locked": False}
    assert snapshot["credit_locked"] is False


@pytest.mark.parametrize("biz_code", ["1035", 1035])
def test_native_trade_error_checks_top_level_and_business_codes(biz_code):
    body = {"code": 409, "reason": "CREDIT_PLAN_STALE", "metadata": {"biz_code": biz_code}}
    expected = {"status": 409, "reason": "CREDIT_PLAN_STALE", "biz_code": 1035}
    assert expect_trade_problem(httpx.Response(409, json=body), **expected)["biz_code"] == 1035
    with pytest.raises(AssertionError, match="业务码"):
        expect_trade_problem(httpx.Response(409, json=body), **{**expected, "biz_code": 1004})


@pytest.mark.parametrize(("environment", "path", "message", "alert_reason"), [
    ("preview", ORDER_SUBMIT_PATH, "trade-service unavailable, request result unknown, do not replay", "订单结果未知，请回查原单"),
    ("preview", ORDER_SUBMIT_PATH, "Could not add funds. Please try again.", "Polymarket pUSD 补资未就绪"),
    ("preview", ORDER_SUBMIT_PATH, "other internal error", None),
    ("preview", "/api/v1/orderbook/orders/prepare", "Could not add funds. Please try again.", None),
    ("prod", ORDER_SUBMIT_PATH, "trade-service unavailable, request result unknown, do not replay", None),
])
def test_preview_submit_alert_requires_matching_error(monkeypatch, environment, path, message, alert_reason):
    monkeypatch.setenv("BUZZING_ENV", environment)
    alerts = []
    calls = []

    def handle(outgoing):
        calls.append(outgoing.url.path)
        return httpx.Response(500, json={
            "code": 500, "reason": "INTERNAL", "metadata": {"biz_code": "1002"}, "message": message,
        })

    token = preview_submit_alert_handler.set(alerts.append)
    try:
        with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
            response = request(client, "POST", path, operation="test_submit", json={"plan_id": "plan", "cash_signature": "private"})
    finally:
        preview_submit_alert_handler.reset(token)

    assert response.status_code == 500 and calls == [path]
    if alert_reason is None:
        assert not alerts
    else:
        alert, = alerts
        assert alert["plan_id"] == "plan" and alert["biz_code"] == "1002"
        assert alert["message"] == message and alert["alert_reason"] == alert_reason
        assert "cash_signature" not in alert


def test_mixed_funding_requires_approved_batch_and_cash_leg():
    fixture = SimpleNamespace(case="polymarket_mixed", budget_raw=3_000_000,
                              venue="polymarket", spec={"batch_ids": ["21"]})
    credit = {"funding_source": "credit", "batch_id": "21", "side": "BUY", "quote_amount": "1000000", "est_fee": "0"}
    cash = {"funding_source": "cash", "side": "BUY", "quote_amount": "1820000", "est_fee": "180000"}
    plan = {"plan_id": uuid4().hex, "logical_order_id": str(uuid4()), "venue": "polymarket",
            "legs": [credit, cash], "credit_applied": True, "credit_quote_amount": credit["quote_amount"],
            "cash_quote_amount": cash["quote_amount"], "cash_signing": "server", "cash_leg": {"typed_data_json": ""}}
    ids, legs = verify_funding_plan(plan, fixture)
    assert ids["plan_id"] == plan["plan_id"] and {leg["funding_source"] for leg in legs} == {"cash", "credit"}
    with pytest.raises(AssertionError, match="资金组合"):
        verify_funding_plan({**plan, "legs": [credit]}, fixture)
    with pytest.raises(AssertionError, match="Credit 批次"):
        verify_funding_plan({**plan, "legs": [{**credit, "batch_id": "22"}, cash]}, fixture)
    with pytest.raises(AssertionError, match="代签"):
        verify_funding_plan({**plan, "cash_signing": "client"}, fixture)


@pytest.mark.parametrize("with_cash", [False, True])
def test_funded_limit_links_receipt_history_and_funding(monkeypatch, with_cash):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from function import polymarket_funding

    logical = str(uuid4())
    fixture = SimpleNamespace(source="polymarket", uid="991", price=Decimal("0.20"),
                              budget_raw=3_000_000, check_window=lambda: None)
    market = {"market_id": "42", "condition_id": "condition", "position_id": "outcome", "minimum_shares": "5"}
    legs = [{"funding_source": "credit", "batch_id": "11", "quantity": "5000000",
             "quote_amount": "1000000", "est_fee": "0"}]
    if with_cash:
        legs.append({"funding_source": "cash", "batch_id": "", "quantity": "5000000",
                     "quote_amount": "1000000", "est_fee": "0"})
    plan = {"legs": legs, "credit_reserve_amount": "1000000",
            "plan_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    ids = {"plan_id": uuid4().hex, "logical_order_id": logical}
    receipts = [{"request_index": i, "order_id": f"order-{i}", "success": True,
                 "funding_source": leg["funding_source"], "batch_id": leg["batch_id"]}
                for i, leg in enumerate(legs)]
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/user/delegated-signer":
            return httpx.Response(200, json={"wallet_address": "wallet", "status": "granted", "signer_id": "id"})
        if request.method == "POST":
            assert b'"cash_signature":""' in request.content
            return httpx.Response(200, json={"replayed": False, "logical_order_id": logical,
                                             "credit_reserved": "1000000", "results": receipts})
        else:
            assert request.url.params["logical_order_ids"] == logical
            # The history API returns one combined row for a logical order with multiple funding legs.
            return httpx.Response(200, json={"items": [{
                "source": "polymarket", "market": {"id": 42},
                "order": {"order_id": "order-0", "logical_order_id": logical, "uid": "991", "status": "filled",
                          "side": "buy", "order_type": "limit", "condition_id": "condition", "position_id": "outcome",
                          "onchain_status": "confirmed", "filled_quantity": str(5 * len(legs)),
                          "filled_quote": str(len(legs)), "remaining_quantity": "0"},
            }]})

    before = {"cash": 3_000_000, "credit_available": 2_000_000, "credit_in_position": 0}
    after = {**before, "credit_available": 1_000_000, "credit_in_position": 1_000_000}
    balance_after = Decimal("2" if with_cash else "3")
    balances = iter([(Decimal("3"), Decimal("3")), (balance_after, balance_after)])
    monkeypatch.setattr(polymarket_funding, "read_withdrawable_cash", lambda *_: next(balances))
    monkeypatch.setattr(polymarket_funding, "funding_snapshot", lambda *_: after)
    monkeypatch.setattr(polymarket_funding, "read_asks", lambda *_: [(Decimal("0.2"), Decimal("10"))])
    monkeypatch.setattr(polymarket_funding, "require_empty_market_orders", lambda *_: None)
    monkeypatch.setattr(polymarket_funding, "ensure_market_trading", lambda *_, **__: None)
    evidence = {}

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
        polymarket_funding.execute_funded_limit(client, fixture, "wallet", market, plan, ids, before, evidence.__setitem__)

    assert [path for method, path in calls if method == "POST"] == ["/api/v1/orderbook/orders/submit"]
    assert evidence["成交与历史匹配"]["logical_order_id"] == logical
    assert evidence["成交后的 Cash 与 Credit"]["after_pusd"] == str(balance_after)
