from datetime import datetime, timezone
from decimal import Decimal
from time import monotonic, sleep

from function.api_client import request
from function.orders import list_orders
from function.polymarket_limit_trade import CANCELLED, read_asks, read_withdrawable_cash
from function.polymarket_trade import CONFIRMATION_SECONDS, ensure_market_trading, shares
from function.response_checks import expect_json
from function.trade_contract import funding_snapshot, require_empty_market_orders
from function.trade_run import raw_amount, timestamp
from function.trade_state import BOOK


def execute_funded_limit(client, fixture, signer, selected, plan, ids, before, record, *, uid):
    ensure_market_trading(client, selected, "多资金限价提交前", refresh=True)
    fixture.check_window()
    if timestamp(plan["plan_expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError("出资计划已过期")
    require_empty_market_orders(client, fixture.source, selected)
    planned = sum(raw_amount(leg["quantity"]) for leg in plan["legs"])
    quote_depth = sum(size for price, size in read_asks(client, selected["position_id"]) if price <= fixture.price)
    if not Decimal(selected["minimum_shares"]) <= Decimal(shares(planned)) <= quote_depth:
        raise ValueError("专用盘口限价内的深度无法支持本轮计划全成")
    cash_plan = sum(raw_amount(leg["quote_amount"]) + raw_amount(leg["est_fee"])
                    for leg in plan["legs"] if leg["funding_source"] == "cash")
    if cash_plan:
        grant = expect_json(request(client, "GET", "/api/v1/user/delegated-signer", operation="funded_order_signer"), 200)
        before_withdrawable, before_pusd = read_withdrawable_cash(client, uid)
        if (grant.get("status") != "granted" or not grant.get("signer_id")
                or str(grant.get("wallet_address", "")).lower() != signer.lower()
                or min(before_withdrawable, before_pusd) < Decimal(shares(cash_plan))):
            raise ValueError("混合资金 Cash 腿需已有足额 pUSD 和代签授权")
    else:
        before_withdrawable, before_pusd = read_withdrawable_cash(client, uid)
    record("提交前资金和盘口", {**ids, "before": before, "planned_shares": shares(planned),
                           "crossing_shares": str(quote_depth), "cash_plan_raw": cash_plan,
                           "before_withdrawable": str(before_withdrawable), "before_pusd": str(before_pusd)})
    params = {"source": fixture.source, "logical_order_ids": ids["logical_order_id"], "page_size": 100}
    try:
        response = request(client, "POST", BOOK + "orders/submit", operation="submit_funded_limit", timeout=30,
                           json={"plan_id": ids["plan_id"], "cash_signature": ""})
    except RuntimeError:
        record_order_status(client, params, record)
        raise
    if response.status_code >= 500:
        record_order_status(client, params, record)
    receipt = expect_json(response, 200)
    legs = receipt["results"]
    order_ids = {leg["order_id"] for leg in legs if leg.get("order_id") and leg.get("success") is True}
    record("分腿提交回执", {**ids, "replayed": receipt.get("replayed"), "credit_reserved": receipt.get("credit_reserved"),
                      "results": [{key: leg.get(key) for key in ("request_index", "funding_source", "batch_id",
                                                                  "success", "order_id", "status", "error_code")}
                                  for leg in legs]})
    try:
        assert receipt["logical_order_id"] == ids["logical_order_id"] and receipt["replayed"] is False
        assert raw_amount(receipt["credit_reserved"]) == raw_amount(plan["credit_reserve_amount"]) > 0
        assert len(legs) == len(order_ids) == len(plan["legs"])
        by_index = {leg["request_index"]: leg for leg in legs}
        for index, expected in enumerate(plan["legs"]):
            live = by_index[index]
            assert (live["funding_source"], live.get("batch_id")) == (expected["funding_source"], expected.get("batch_id"))
        wait_funded_confirmation(client, fixture, selected, params, order_ids, planned, record, uid=uid)
    except (ValueError, AssertionError):
        if order_ids and receipt.get("logical_order_id") == ids["logical_order_id"]:
            active = qualified_orders(client, fixture, selected, params, order_ids, uid=uid)
            cancel_ids = [order["order_id"] for order in active if order["status"] in {"open", "partial"}]
            if cancel_ids:
                order_id = cancel_ids[0]
                record("撤销本轮未成交的逻辑单", {**ids, "order_id": order_id})
                expect_json(request(client, "POST", BOOK + "order/cancel", operation="cancel_funded_limit",
                                    json={"source": fixture.source, "order_id": order_id}), 200)
                record_order_status(client, params, record)
        raise
    deadline = monotonic() + CONFIRMATION_SECONDS
    while True:
        after = funding_snapshot(client)
        after_withdrawable, after_pusd = read_withdrawable_cash(client, uid)
        credit_recorded = (after["credit_available"] < before["credit_available"]
                           and after["credit_in_position"] > before["credit_in_position"])
        if cash_plan:
            cash_recorded = after_pusd < before_pusd and after_withdrawable < before_withdrawable
        else:
            cash_recorded = (before["cash"], before_pusd, before_withdrawable) == (
                after["cash"], after_pusd, after_withdrawable)
        if credit_recorded and cash_recorded:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(remaining, 1))
    record("成交后的 Cash 与 Credit", {**ids, "before": before, "after": after, "before_pusd": str(before_pusd),
                              "after_pusd": str(after_pusd), "before_withdrawable": str(before_withdrawable),
                              "after_withdrawable": str(after_withdrawable)})
    assert credit_recorded and cash_recorded, "成交后的 Cash 或 Credit 账务与计划不符"


def record_order_status(client, params, record):
    page = expect_json(list_orders(client, params=params), 200)
    record("按逻辑订单回查", {"logical_order_id": params["logical_order_ids"],
                       "orders": [{"order_id": row["order"]["order_id"], "status": row["order"]["status"]}
                                  for row in page["items"]]})


def qualified_orders(client, fixture, selected, params, order_ids, *, uid):
    page = expect_json(list_orders(client, params=params), 200)
    orders = []
    for row in page["items"]:
        order = row["order"]
        # The history view can ignore logical_order_ids. Check its returned rows locally.
        if params.get("view") == "history" and order["logical_order_id"] != params["logical_order_ids"]:
            continue
        assert row["source"] == fixture.source and order["logical_order_id"] == params["logical_order_ids"]
        assert order["order_id"] in order_ids and str(order["uid"]) == uid
        assert order["status"] and order["side"] == "buy" and order["order_type"] == "limit"
        assert str(row["market"]["id"]) == selected["market_id"] and order["condition_id"] == selected["condition_id"]
        assert order["position_id"] == selected["position_id"]
        orders.append(order)
    return orders


def wait_funded_confirmation(client, fixture, selected, params, order_ids, planned, record, *, uid):
    deadline = monotonic() + CONFIRMATION_SECONDS
    while (remaining := deadline - monotonic()) > 0:
        orders = qualified_orders(client, fixture, selected, params, order_ids, uid=uid)
        states = {order["status"] for order in orders}
        if states & {"expired", "rejected", "failed", *CANCELLED}:
            raise ValueError(f"逻辑订单 {params['logical_order_ids']} 有未成交终态")
        if states == {"filled"} and all(order.get("onchain_status") in {"confirmed", "none"} for order in orders):
            filled = sum(Decimal(order["filled_quantity"]) for order in orders)
            quote = sum(Decimal(order["filled_quote"]) for order in orders)
            unfilled = sum(Decimal(order["remaining_quantity"]) for order in orders)
            if filled == Decimal(shares(planned)) and 0 < quote <= Decimal(shares(fixture.budget_raw)) and unfilled == 0:
                history = qualified_orders(client, fixture, selected, {**params, "view": "history",
                                           "condition_id": selected["condition_id"],
                                           "position_ids": selected["position_id"]}, order_ids, uid=uid)
                if sum(Decimal(order["filled_quantity"]) for order in history) == filled:
                    record("成交与历史匹配", {"logical_order_id": params["logical_order_ids"],
                                        "order_ids": [order["order_id"] for order in orders],
                                        "filled_quantity": str(filled), "filled_quote": str(quote)})
                    return
        sleep(min(remaining, 1))
    record_order_status(client, params, record)
    raise ValueError(f"逻辑订单 {params['logical_order_ids']} 在 {CONFIRMATION_SECONDS} 秒内未确认全成和历史")
