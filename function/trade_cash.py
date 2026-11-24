from time import monotonic, sleep
from uuid import UUID

import pytest


def execute_cash(support, api, record, sdk, session, run, uid, signer, maker, prepare):
    cash, trade = support.cash, support.trade
    book = "/api/v1/orderbook"
    market, budget = trade.TRADE_NO_MARKET, trade.TRADE_BUDGET
    query = {"source": "orderbook", "condition_id": market["condition_id"], "position_id": market["token_id"]}
    profile = cash.trading_profile(api, session, uid, maker)
    before = cash.raw(profile["usdb_balance"])
    baseline = cash.position_quantity(api, session, query)
    open_orders = cash.list_rows(api("GET", book + "/orders", 200, token=session,
                                     params={**query, "statuses": "init,pending,partial"}))
    if (before < budget or open_orders or cash.raw(profile["usdb_frozen_amount"]) or
            profile.get("is_approve_orderbook_all") is not False or cash.chain_approvals(maker) != (0,) * 6):
        raise ValueError("Cash 余额、挂单、冻结或授权状态与本轮前置条件不符")
    record(stage="cash_baseline", cash_raw=before, position_raw=baseline)
    can_revoke = False
    run.check_window()
    try:
        cash.set_approvals(sdk, record, run.account, signer, maker, grant=True)
        can_revoke = True
        if cash.chain_approvals(maker) != (budget, 1, 1, 1, 1, 1):
            raise ValueError("链上授权与本轮预算不符")
        cash.wait_approval(api, session, uid, maker, True)
        plan, logical, signature, order_hash = prepare()
        if cash.order_state(api, session, query, order_hash) is not None:
            raise ValueError("本轮订单哈希已有记录")
        run.check_window()
        can_revoke = False
        receipt = None
        try:
            receipt = cash.data(api("POST", book + "/orders/submit", 200, token=session, timeout=70,
                                    json={"plan_id": plan, "cash_signature": signature}))
        except pytest.fail.Exception:
            record(stage="submit_result_unknown", logical_order_id=logical, order_hash=order_hash)
        if receipt is not None:
            legs = receipt.get("results")
            if receipt.get("logical_order_id") != logical or not isinstance(legs, list) or len(legs) != 1:
                raise ValueError("Cash 分腿回执无法关联，请核对本轮订单")
            if legs[0].get("success") is False:
                can_revoke = True
                raise ValueError("服务端拒绝本轮 Cash 腿")
            if legs[0].get("success") is not True or legs[0].get("funding_source") != "cash":
                raise ValueError("Cash 分腿结果不明")
        started = monotonic()
        cancelled = False
        while monotonic() - started < 145:
            order = cash.order_state(api, session, query, order_hash)
            if order:
                if (str(order.get("uid")) != str(uid) or order.get("logical_order_id") != logical or
                        order.get("condition_id") != query["condition_id"] or order.get("position_id") != query["position_id"]
                        or str(order.get("side")).lower() != "buy"):
                    raise ValueError("查回的订单不属于本轮")
                order_id = str(UUID(order["order_id"]))
                state, settlement = order["status"], order["onchain_status"]
                if state in {"pending", "partial"} and not cancelled and monotonic() - started >= 10:
                    cancelled = True
                    record(stage="cancel_attempt", order_id=order_id)
                    cash.data(api("POST", book + "/order/cancel", 200, token=session, json={"order_id": order_id}))
                if state in {"filled", "cancelled", "expired", "rejected"}:
                    if settlement == "failed" or (settlement == "none" and cash.raw(order["filled_quantity"]) == 0):
                        can_revoke = True
                        raise ValueError("本轮订单未成功成交结算")
                    if settlement == "confirmed":
                        fills = cash.matching_fills(api, session, query, order_id, order_hash, uid, trade.TRADE_LIMIT)
                        if fills and all(row.get("status") == "success" and row.get("tx_hash") for row in fills):
                            can_revoke = True
                            quantity, quote, fee = (sum(cash.raw(row[field]) for row in fills)
                                                    for field in ("quantity", "quote_amount", "fee_amount"))
                            if not (quantity == cash.raw(order["filled_quantity"]) > 0 and 0 < quote + fee <= budget):
                                raise ValueError("成交量或含费扣款与本轮订单不符")
                            after = cash.trading_profile(api, session, uid, maker)
                            balance = cash.raw(after["usdb_balance"])
                            position = cash.position_quantity(api, session, query)
                            if (before - balance == quote + fee and position - baseline == quantity
                                    and cash.raw(after["usdb_frozen_amount"]) == 0):
                                record(stage="reconciled", logical_order_id=logical, order_id=order_id,
                                       fill_keys=[row["fill_key"] for row in fills], txs=[row["tx_hash"] for row in fills],
                                       quantity_raw=quantity, quote_raw=quote, fee_raw=fee, after_cash_raw=balance,
                                       after_position_raw=position)
                                return
            sleep(4)
        raise ValueError("本轮 Cash 订单或对账结果尚未确认，请按原单核查")
    finally:
        if can_revoke:
            cash.set_approvals(sdk, record, run.account, signer, maker, grant=False)
            if cash.chain_approvals(maker) != (0,) * 6:
                raise ValueError("链上仍有本轮临时授权")
            cash.wait_approval(api, session, uid, maker, False)
            record(stage="grants_cleared")
        else:
            record(stage="check_order_and_grants", uid=uid)
