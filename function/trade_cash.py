import json
import re
from dataclasses import dataclass
from time import monotonic, sleep

import pytest

from function.trade_run import BUY_BUDGET_RAW, LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, MARKET_SELL, raw_amount
from function.trade_state import (BOOK, OPEN_STATES, TERMINAL_STATES, CashSnapshot, OrderAmounts,
                                  order_fills, order_query, own_order, profile_order, raw_at_price)


@dataclass(frozen=True)
class CashPlan:
    plan_id: str
    logical_id: str
    order_hash: str
    signature: str
    quantity: int


def sign_plan(support, sdk, record, run, signer, maker, reply, *, market=None):
    cash, trade = support.cash, support.trade
    market = trade.TRADE_NO_MARKET if market is None else market
    body = cash.data(reply)
    leg, signing = body["legs"][0], body["cash_leg"]
    quote, fee, quantity = (raw_amount(leg[key]) for key in ("quote_amount", "est_fee", "quantity"))
    order_hash = str(signing.get("order_hash", "")).lower()
    if (str(leg.get("side")).upper() != run.side or body.get("credit_applied") or quantity <= 0
            or not re.fullmatch(r"0x[0-9a-f]{64}", order_hash) or order_hash != str(leg.get("order_hash")).lower()):
        raise ValueError("Cash 下单计划与本轮方向或订单哈希不符")
    if run.side == "BUY":
        if not 0 < quote <= quote + fee <= run.budget_raw or raw_amount(body["cash_quote_amount"]) != quote:
            raise ValueError("Cash 买单含费金额超出本轮预算")
    elif quantity != run.quantity_raw or quote < raw_at_price(quantity, run.price) or fee >= quote:
        raise ValueError("Cash 市价卖出方案的数量、预估成交额或费用不符")
    try:
        typed = json.loads(signing["typed_data_json"])
        message = typed["message"]
    except (KeyError, ValueError, TypeError):
        raise ValueError("Cash 计划缺少有效的 EIP-712 数据") from None
    maker_amount, taker_amount = (raw_amount(message[key]) for key in ("makerAmount", "takerAmount"))
    if run.side == "BUY" and (maker_amount != quote or taker_amount != quantity
                               or quote > raw_at_price(quantity, run.price)):
        raise ValueError("BUY 签名金额、数量或限价与方案不符")
    # A market sell can have no signed minimum. The configured price only gates Preview depth and reconciliation.
    if run.side == "SELL" and (maker_amount != quantity or taker_amount != raw_amount(leg["taker_amount"])):
        raise ValueError("SELL 签名的份额或对应金额与方案不符")
    run.check_window()
    side = 0 if run.side == "BUY" else 1
    result = sdk.evaluate("(sdk, data) => sdk.proveNoOrder(data)", {
        "address": signer, "maker": maker, "exchange": trade.TRADE_EXCHANGE,
        "tokenId": market["token_id"], "orderHash": order_hash, "typedData": typed,
        "forSubmit": True, "expectedSide": side,
    })
    support.identity.sdk_status(result, record, "cash_order_signature")
    signature = result.get("value")
    if not isinstance(signature, str) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
        raise ValueError("签名 SDK 未返回已核对的订单签名")
    plan_id, logical_id = cash.plan_ids(reply)
    record(stage="signed", logical_order_id=logical_id, order_hash=order_hash,
           quantity_raw=quantity, quote_raw=quote, estimated_fee_raw=fee)
    return CashPlan(plan_id, logical_id, order_hash, signature, quantity)


def execute_cash(support, api, record, sdk, session, run, uid, signer, maker, prepare, *, market=None):
    cash = support.cash
    market = support.trade.TRADE_NO_MARKET if market is None else market
    query = order_query(market)

    def snapshot():
        return CashSnapshot.read(api, cash, session, query, uid, maker)

    before = snapshot()
    open_orders = cash.list_rows(api("GET", BOOK + "orders", 200, token=session,
                                     params={**query, "statuses": "init,pending,partial"}))
    if open_orders or before.frozen or before.position_frozen:
        raise ValueError("本轮测试钱包仍有挂单或冻结")
    existing_grants = bool(run.partial_budget_cap_raw)
    grants = cash.chain_approvals(maker)
    if existing_grants:
        if before.approvals is not True or grants[0] < run.budget_raw or grants[1:] != (1,) * 5:
            raise ValueError("专用签名钱包缺少覆盖本轮动态 Amount 的自营交易授权")
    elif before.approvals is not False or grants != (0,) * 6:
        raise ValueError("Cash 临时授权的起始状态与本轮前置条件不符")
    available_shares = before.position - before.position_frozen
    if (run.side == "BUY" and before.cash < run.budget_raw) or available_shares < run.quantity_raw:
        raise ValueError("专用钱包的 Cash 或自有持仓不足")
    record(stage="cash_baseline", available_shares_raw=available_shares, **before.evidence())

    can_revoke = existing_grants
    run.check_window()
    try:
        if not existing_grants:
            cash.set_approvals(sdk, record, run.account, signer, maker, grant=True)
            can_revoke = True
            if cash.chain_approvals(maker) != (BUY_BUDGET_RAW, 1, 1, 1, 1, 1):
                raise ValueError("链上授权与受控钱包授权范围不符")
            cash.wait_approval(api, session, uid, maker, True)
        plan = prepare()
        if own_order(api, cash, session, query, plan.logical_id, uid, run, order_hash=plan.order_hash):
            raise ValueError("本轮计划已有订单")
        run.check_window()
        can_revoke = False
        receipt_leg = submit_cash(api, cash, record, session, plan)
        if receipt_leg and receipt_leg.get("success") is False:
            can_revoke = True
            raise ValueError("服务端拒绝本轮 Cash 腿")

        started = monotonic()
        cancel_attempted = False
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
                if run.scenario == LIMIT_CANCEL:
                    try:
                        opened = snapshot()
                        pending = OrderAmounts.read(order, plan.quantity)
                        expected_frozen = before.frozen + raw_at_price(pending.remaining, run.price)
                        if (state in {"open", "pending"} and order.get("post_only") is True and pending.filled == 0
                                and opened == CashSnapshot(before.cash, expected_frozen,
                                                           before.position, before.position_frozen, True)
                                and profile_order(api, cash, session, "open", order_id) is not None):
                            record(stage="cash_frozen", order_id=order_id, remaining_raw=pending.remaining,
                                   **opened.evidence())
                            frozen_observed = True
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
                    cancel_cash(api, cash, record, session, order_id)
            if state not in TERMINAL_STATES or settlement not in {"confirmed", "none", "failed"}:
                sleep(4)
                continue
            after = snapshot()
            if after.frozen != before.frozen or after.position_frozen != before.position_frozen:
                sleep(4)
                continue
            can_revoke = True
            amounts = OrderAmounts.read(order, plan.quantity)
            fills = order_fills(api, cash, session, query, order, run, uid)
            if not reconcile_cash(record, run, before, after, order, amounts, fills,
                                  cancel_attempted=cancel_attempted, frozen_observed=frozen_observed,
                                  partial_observed=partial_observed):
                sleep(4)
                continue
            if receipt_leg and (receipt_leg.get("funding_source") != "cash"
                                or receipt_leg.get("order_id") != order_id
                                or str(receipt_leg.get("order_hash")).lower() != plan.order_hash):
                raise ValueError("Cash 提交回执与查回的订单不符")
            check_order_views(api, cash, record, session, order, run, amounts)
            return
        raise ValueError("本轮 Cash 订单、冻结释放或对账尚未确认，请按原单核查")
    finally:
        if existing_grants:
            record(stage="preexisting_grants_kept" if can_revoke else "check_order_and_grants", uid=uid)
        elif can_revoke:
            cash.set_approvals(sdk, record, run.account, signer, maker, grant=False)
            if cash.chain_approvals(maker) != (0,) * 6:
                raise ValueError("链上仍有本轮临时授权")
            cash.wait_approval(api, session, uid, maker, False)
            record(stage="grants_cleared")
        else:
            record(stage="check_order_and_grants", uid=uid)


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
        cash.data(api("POST", BOOK + "order/cancel", 200, token=session, json={"order_id": order_id}))
    except pytest.fail.Exception:
        record(stage="cancel_result_unknown", order_id=order_id)


def reconcile_cash(record, run, before, after, order, amounts, fills, *, cancel_attempted, frozen_observed,
                   partial_observed=False):
    state, settlement = order["status"], order["onchain_status"]
    if run.scenario == LIMIT_CANCEL:
        if (state != "cancelled" or settlement != "none" or order.get("post_only") is not True
                or not cancel_attempted or not frozen_observed or amounts.filled or amounts.quote or fills):
            raise ValueError("主动挂单未取得零成交撤单及冻结证据")
        if (before.cash, before.position) != (after.cash, after.position):
            return False
        quantity = quote = fee = 0
    else:
        requires_full_fill = run.order_type == "MARKET" or (run.scenario == LIMIT_BUY and bool(run.market_ids))
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
