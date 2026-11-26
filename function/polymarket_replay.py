from time import monotonic, sleep

from function.api_client import request
from function.orders import list_orders
from function.polymarket_limit_trade import read_withdrawable_cash
from function.polymarket_trade import SOURCE, fill_quantity_raw, match_polymarket_order, read_cash_position, shares
from function.response_checks import expect_json
from function.trade_run import raw_amount
from function.trade_state import BOOK


def limit_order_ids(client):
    ids = set()
    for view in ("open", "history"):
        params = {"source": SOURCE, "view": view, "page_size": 100}
        cursors = set()
        while True:
            page = expect_json(list_orders(client, params=params), 200)
            if page.get("truncated"):
                raise ValueError(f"Polymarket {view} 订单视图数据被截断")
            for item in page["items"]:
                order_id = item["order"]["order_id"]
                if item["source"] != SOURCE or not order_id:
                    raise ValueError(f"Polymarket {view} 订单视图身份不符")
                ids.add(order_id)
            if not page["pagination"].get("has_more"):
                break
            cursor = page["pagination"].get("next_cursor")
            if not cursor or cursor in cursors:
                raise ValueError(f"Polymarket {view} 订单视图缺少可继续翻页的游标")
            cursors.add(cursor)
            params["next_cursor"] = cursor
    return ids


def stable_replay_snapshot(client, uid, selected, order_id):
    deadline = monotonic() + 30
    previous = None
    while (remaining := deadline - monotonic()) > 0:
        funding = expect_json(request(client, "GET", "/api/v1/profile/funding", operation="replay_funding"), 200)
        if funding.get("cash_degraded") is not False:
            raise ValueError("Cash 资金数据处于降级状态")
        withdrawable, pusd = read_withdrawable_cash(client, uid)
        quantity, available = read_cash_position(client, uid, selected)
        snapshot = {
            "cash": shares(raw_amount(funding["cash"])), "pusd": str(pusd), "withdrawable": str(withdrawable),
            "quantity": shares(quantity), "available": shares(available), "orders": sorted(limit_order_ids(client)),
        }
        if snapshot == previous and order_id in snapshot["orders"]:
            return snapshot
        previous = snapshot
        wait = deadline - monotonic()
        if wait > 0:
            sleep(min(1, wait))
    raise ValueError(f"订单 {order_id} 的资金、持仓或历史订单快照尚未稳定")


def read_replayed_order(client, uid, selected, logical, order_id):
    response = list_orders(client, params={"source": SOURCE, "logical_order_ids": logical, "page": 1, "page_size": 100})
    return match_polymarket_order(response, logical, order_id, selected, uid, "BUY", "LIMIT")


def verify_nba_limit_replay(client, run, uid, record, original):
    selected, fill = original.selected, original.fill
    if uid != original.uid or not original.plan_id or fill["status"] != "filled" or fill["onchain_status"] not in {"confirmed", "none"}:
        raise ValueError("重放要求同一账号本轮已确认全成的 NBA 限价单")
    logical, order_id = fill["logical_order_id"], fill["order_id"]
    ids = dict(plan_id=original.plan_id, logical_order_id=logical, order_id=order_id)
    before = stable_replay_snapshot(client, uid, selected, order_id)
    original_readback = read_replayed_order(client, uid, selected, logical, order_id) or {}
    run.check_window()
    record(stage="nba_limit_replay_attempt", **ids, before=before, status=original_readback.get("status"),
           onchain_status=original_readback.get("onchain_status"))
    try:
        response = request(client, "POST", BOOK + "orders/submit", operation="replay_nba_limit_order",
                           json={"plan_id": original.plan_id, "cash_signature": ""}, timeout=30)
    except RuntimeError:
        order = read_replayed_order(client, uid, selected, logical, order_id) or {}
        record(stage="nba_limit_replay_readback", **ids, status=order.get("status"), onchain_status=order.get("onchain_status"))
        raise
    if response.status_code >= 500:
        order = read_replayed_order(client, uid, selected, logical, order_id) or {}
        record(stage="nba_limit_replay_readback", **ids, status=order.get("status"), onchain_status=order.get("onchain_status"))
    result = expect_json(response, 200)
    legs = result.get("results") or []
    replay_order_ids = [leg.get("order_id") for leg in legs]
    record(stage="nba_limit_replay_response", **ids, replayed=result.get("replayed"),
           replay_logical_order_id=result.get("logical_order_id"), replay_order_ids=replay_order_ids)
    after = stable_replay_snapshot(client, uid, selected, order_id)
    record(stage="nba_limit_replay_reconciled", **selected, **ids, replayed=result.get("replayed"),
           before=before, after=after, new_order_ids=sorted(set(after["orders"]) - set(before["orders"])))
    if (result.get("replayed") is not True or result.get("logical_order_id") != logical
            or replay_order_ids != [order_id] or legs[0].get("success") is not True
            or legs[0].get("funding_source") != "cash"):
        raise ValueError(f"原计划 {original.plan_id} 的重放回包与已成交订单不一致")
    if before != after:
        raise ValueError(f"订单 {logical} 重放前后的订单、资金或持仓不一致")
    order = read_replayed_order(client, uid, selected, logical, order_id) or {}
    record(stage="nba_limit_replay_readback", **ids, status=order.get("status"), onchain_status=order.get("onchain_status"),
           filled_quantity=order.get("filled_quantity"), filled_quote=order.get("filled_quote"),
           remaining_quantity=order.get("remaining_quantity"))
    if (order.get("status") != "filled" or order.get("onchain_status") not in {"confirmed", "none"}
            or fill_quantity_raw(order) != fill_quantity_raw(fill)):
        raise ValueError(f"订单 {logical} 重放后的成交状态或数量与原单不一致")
