from dataclasses import dataclass
from time import monotonic, sleep

import pytest

from function.trade_run import LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, MARKET_SELL, price_amount, raw_amount
from function.trade_state import (BOOK, OPEN_STATES, TERMINAL_STATES, CashSnapshot, OrderAmounts,
                                  order_fills, order_query, own_order, profile_order, raw_at_price)


@dataclass(frozen=True)
class CashPlan:
    plan_id: str
    logical_id: str
    order_hash: str | None
    signature: str
    quantity: int


def execute_cash(cash, api, record, session, run, uid, maker, prepare, *, market, check_fills=None):
    query = order_query(market)

    def snapshot():
        return CashSnapshot.read(api, cash, session, query, uid, maker)

    def available_cash():
        params = {"condition_id": query["condition_id"], "token_id": query["position_id"]}
        balance = cash.data(api("GET", BOOK + "buying-power", 200, token=session, params=params))
        return raw_amount(balance["balance"])

    before = snapshot()
    open_orders = cash.list_rows(api("GET", BOOK + "orders", 200, token=session,
                                     params={**query, "statuses": "init,pending,partial"}))
    if open_orders or before.frozen or before.position_frozen:
        raise ValueError("本轮测试钱包仍有挂单或冻结")
    available_shares = before.position - before.position_frozen
    if (run.side == "BUY" and before.cash < run.budget_raw) or available_shares < run.quantity_raw:
        raise ValueError("专用钱包的 Cash 或自有持仓不足")
    passive = run.scenario == LIMIT_CANCEL
    baseline = before.evidence()
    if passive:
        available_before = available_cash()
        baseline["cash_available_raw"] = available_before
    record(stage="cash_baseline", available_shares_raw=available_shares, **baseline)

    needs_reconciliation = False
    run.check_window()
    try:
        plan = prepare()
        if own_order(api, cash, session, query, plan.logical_id, uid, run, order_hash=plan.order_hash):
            raise ValueError("本轮计划已有订单")
        run.check_window()
        needs_reconciliation = True
        receipt_leg = submit_cash(api, cash, record, session, plan)
        if receipt_leg and receipt_leg.get("success") is False:
            needs_reconciliation = False
            raise ValueError("服务端拒绝本轮 Cash 腿")

        started = monotonic()
        cancel_attempted = False
        cancel_receipt = None
        frozen_observed = False
        partial_observed = False
        while monotonic() - started < 145:
            order = own_order(api, cash, session, query, plan.logical_id, uid, run, order_hash=plan.order_hash)
            if order is None:
                sleep(4)
                continue
            order_id, state, settlement = order["order_id"], order["status"], order["onchain_status"]
            if run.order_type == "LIMIT" and state in OPEN_STATES and state != "init" and not cancel_attempted:
                open_check_failed = False
                if passive and not frozen_observed:
                    try:
                        opened = snapshot()
                        pending = OrderAmounts.read(order, plan.quantity)
                        reserved = raw_at_price(pending.remaining, run.price)
                        expected_frozen = before.frozen + reserved
                        if pending.filled or pending.quote:
                            raise ValueError("零成交用例的挂单已发生实际成交")
                        if (state in {"open", "pending"} and price_amount(order["price"]) == run.price
                                and reserved > 0 and opened.frozen == expected_frozen
                                and (opened.position, opened.position_frozen) == (before.position, before.position_frozen)
                                and profile_order(api, cash, session, "open", order_id) is not None):
                            available_open = available_cash()
                            frozen_observed = available_open == available_before - reserved
                            record(stage="cash_frozen", order_id=order_id, remaining_raw=pending.remaining,
                                   requested_post_only=True, post_only=order.get("post_only"),
                                   cash_available_raw=available_open,
                                   expected_cash_available_raw=available_before - reserved,
                                   expected_cash_frozen_raw=expected_frozen, **opened.evidence())
                    except (KeyError, ValueError, pytest.fail.Exception) as error:
                        record(stage="freeze_check_failed", order_id=order_id, error_type=type(error).__name__)
                        open_check_failed = True
                if run.scenario == LIMIT_PARTIAL_CANCEL:
                    try:
                        partial = OrderAmounts.read(order, plan.quantity)
                        if partial.filled > 0 and partial.remaining > 0:
                            partial_observed = True
                            record(stage="partial_fill_observed", logical_order_id=plan.logical_id, order_id=order_id,
                                   status=state, quantity_raw=partial.filled, remaining_raw=partial.remaining,
                                   quote_raw=partial.quote, elapsed_seconds=round(monotonic() - started, 3))
                    except (KeyError, ValueError) as error:
                        record(stage="partial_check_failed", order_id=order_id, error_type=type(error).__name__)
                        open_check_failed = True
                cancel_after = 35 if run.scenario in {LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL} else 10
                if frozen_observed or partial_observed or open_check_failed or monotonic() - started >= cancel_after:
                    cancel_attempted = True
                    cancel_receipt = cancel_cash(api, cash, record, session, order_id)
            if state not in TERMINAL_STATES or settlement not in {"confirmed", "none", "failed"}:
                sleep(4)
                continue
            after = snapshot()
            if after.frozen != before.frozen or after.position_frozen != before.position_frozen:
                sleep(4)
                continue
            if passive:
                available_after = available_cash()
                restored = available_after == available_before
                record(stage="cash_available_checked_after_cancel", order_id=order_id,
                       cash_available_raw=available_after, expected_cash_available_raw=available_before,
                       expected_cash_frozen_raw=before.frozen, **after.evidence())
                if not restored:
                    sleep(4)
                    continue
            amounts = OrderAmounts.read(order, plan.quantity)
            fills = order_fills(api, cash, session, query, order, run, uid)
            if not reconcile_cash(record, run, before, after, order, amounts, fills,
                                  cancel_attempted=cancel_attempted, frozen_observed=frozen_observed,
                                  partial_observed=partial_observed):
                sleep(4)
                continue
            receipt_hash = (receipt_leg or {}).get("order_hash")
            expected_hash = plan.order_hash or order.get("order_hash")
            if receipt_leg and (receipt_leg.get("funding_source") != "cash"
                                or receipt_leg.get("order_id") != order_id
                                or ((plan.order_hash or receipt_hash)
                                    and str(receipt_hash).lower() != str(expected_hash).lower())):
                raise ValueError("Cash 提交回执与查回的订单不符")
            check_order_views(api, cash, record, session, order, run, amounts)
            if cancel_receipt is not None:
                check_cancel_receipt(cancel_receipt, order, amounts, record)
            if check_fills is not None:
                check_fills(fills)
            needs_reconciliation = False
            return
        raise ValueError("本轮 Cash 订单、冻结释放或对账尚未确认，请按原单核查")
    finally:
        record(stage="check_order_and_grants" if needs_reconciliation else "preexisting_grants_kept", uid=uid)


def submit_cash(api, cash, record, session, plan):
    try:
        receipt = cash.data(api("POST", BOOK + "orders/submit", 200, token=session, timeout=70,
                                json={"plan_id": plan.plan_id, "cash_signature": plan.signature}))
    except pytest.fail.Exception:
        record(stage="submit_result_unknown", logical_order_id=plan.logical_id, order_hash=plan.order_hash)
        return None
    return receipt["results"][0]


def cancel_cash(api, cash, record, session, order_id):
    record(stage="cancel_attempt", order_id=order_id)
    try:
        return cash.data(api("POST", BOOK + "order/cancel", 200, token=session,
                             json={"source": "orderbook", "order_id": order_id}))
    except pytest.fail.Exception:
        record(stage="cancel_result_unknown", order_id=order_id)
        return None


def check_cancel_receipt(receipt, order, amounts, record):
    row = receipt.get("order")
    cancelled = row.get("order") if isinstance(row, dict) else None
    keys = ("order_id", "logical_order_id", "condition_id", "position_id")
    if (not isinstance(cancelled, dict) or row.get("source") != "orderbook"
            or cancelled.get("status") != "cancelled" or order["status"] != "cancelled"
            or any(cancelled.get(key) != order[key] for key in keys)):
        raise ValueError("自营撤单回执的订单归属或终态与原单查回结果不符")
    if (OrderAmounts.read(cancelled, amounts.quantity) != amounts
            or cancelled.get("had_fill") is not (amounts.filled > 0)):
        raise ValueError("自营撤单回执的成交标记、份额或金额与原单查回结果不符")
    record(stage="cancel_receipt_checked", order_id=order["order_id"], logical_order_id=order["logical_order_id"],
           status=cancelled["status"], had_fill=cancelled["had_fill"], ordered_raw=amounts.quantity,
           quantity_raw=amounts.filled, remaining_raw=amounts.remaining, quote_raw=amounts.quote)


def reconcile_cash(record, run, before, after, order, amounts, fills, *, cancel_attempted, frozen_observed,
                   partial_observed=False):
    state, settlement = order["status"], order["onchain_status"]
    if run.scenario == LIMIT_CANCEL:
        if (state != "cancelled" or settlement != "none"
                or not cancel_attempted or not frozen_observed or amounts.filled or amounts.quote or fills):
            raise ValueError("主动挂单未取得零成交撤单及冻结证据")
        if (before.cash, before.position) != (after.cash, after.position):
            return False
        quantity = quote = fee = 0
    else:
        requires_full_fill = run.order_type == "MARKET" or (
            run.scenario == LIMIT_BUY and (run.market_ids or run.select_from_volume)
        )
        if run.scenario == LIMIT_PARTIAL_CANCEL and (
            state != "cancelled" or not partial_observed or not amounts.remaining
        ):
            raise ValueError("缺少本轮部分成交后撤掉余单的证据")
        if (settlement != "confirmed" or not amounts.filled
                or (state != "filled" and (state != "cancelled" or not cancel_attempted))
                or (requires_full_fill and state != "filled")
                or (run.scenario == MARKET_SELL and amounts.filled != run.quantity_raw)):
            raise ValueError("本轮订单未取得预期的成交结算")
        if not fills or any(row.get("status") != "success" or not row.get("tx_hash") for row in fills):
            return False
        quantity, quote, fee = (sum(raw_amount(row[key]) for row in fills)
                                for key in ("quantity", "quote_amount", "fee_amount"))
        if quantity != amounts.filled or quote != amounts.quote:
            raise ValueError("成交记录和订单的累计量或成交额不符")
        if run.side == "BUY":
            if not 0 < quote + fee <= run.budget_raw:
                raise ValueError("实际含费扣款超出批准预算")
            cash_delta, position_delta = -(quote + fee), quantity
        else:
            if fee >= quote:
                raise ValueError("成交手续费不应超过卖出所得")
            cash_delta, position_delta = quote - fee, -quantity
        if (after.cash - before.cash, after.position - before.position) != (cash_delta, position_delta):
            return False
    record(stage="reconciled", logical_order_id=order["logical_order_id"], order_id=order["order_id"],
           order_hash=order["order_hash"], status=state, onchain_status=settlement, ordered_raw=amounts.quantity,
           quantity_raw=quantity, remaining_raw=amounts.remaining, quote_raw=quote, fee_raw=fee,
           fill_keys=[row["fill_key"] for row in fills], txs=sorted({row["tx_hash"] for row in fills}),
           **after.evidence())
    return True


def check_order_views(api, cash, record, session, order, run, amounts):
    order_id = order["order_id"]
    # Profile history deliberately excludes cancellations with no fills.
    deadline = monotonic() + 15
    while True:
        opened = profile_order(api, cash, session, "open", order_id)
        history = profile_order(api, cash, session, "history", order_id)
        if opened is None and (history is None) == (amounts.filled == 0):
            break
        if monotonic() >= deadline:
            raise ValueError("订单开放列表或历史未同步到预期状态")
        sleep(3)
    if history and (history["status"] != order["status"]
                    or history.get("logical_order_id") != order["logical_order_id"]
                    or history.get("had_fill") is not True
                    or (run.order_type == "LIMIT" and history.get("ended_reason") != order["status"])
                    or OrderAmounts.read(history, amounts.quantity) != amounts):
        raise ValueError("历史订单与本轮终态或成交数据不符")
    stage = "limit_views_checked" if run.order_type == "LIMIT" else "market_history_checked"
    record(stage=stage, order_id=order_id, history_status=history["status"] if history else None)
