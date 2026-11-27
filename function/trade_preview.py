import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from re import fullmatch
from time import time
from types import SimpleNamespace

import pytest

from function.api_client import create_client, request
from function.identity import get_test_jwt
from function.polymarket_limit_trade import execute_nba_limit_buy, execute_nba_limit_sell
from function.polymarket_replay import verify_nba_limit_replay
from function.polymarket_sports_trade import execute_sports_draw_buy, execute_sports_draw_sell
from function.polymarket_trade import execute_market_cycle, market_closed_reason, read_btc_trade_wallet
from function.market import get_orderbook_event
from function.privy_wallet_signer import (
    SIGNATURE_PATTERN, preview_wallet_session, sign_limit_cancel_plan, verify_bound_wallet,
)
from function.response_checks import expect_json
from function.settings import get_environment, get_test_account, load_test_data
from function.trade_cash import CashPlan, check_order_views, execute_cash
from function.trade_contract import plan_identifiers
from function.trade_market import expected_limit_price, read_trade_depth, select_trade_market
from function.trade_market import read_partial_cancel_quote, select_partial_cancel_market
from function.trade_market import expected_volume_limit_fills, select_volume_limit_market, verify_volume_limit_fills
from function.trade_run import (
    BUY_BUDGET_RAW, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, MARKET_ID,
    POLYMARKET_NBA_LIMIT_BUY, POLYMARKET_NBA_SCENARIOS, POLYMARKET_SCENARIOS, POLYMARKET_SPORTS_DRAW_BUY,
    raw_amount, signing_route, timestamp,
)
from function.trade_state import BOOK, OrderAmounts, order_fills, order_query, own_order, raw_at_price


BACKEND_REVISION = "04bdad8214319d1d7a8a8d4651c5e1219f94992a"
OWN_TEST_EVENT_ID = "1601693"


def backend_support(checkout):
    revision = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    if revision != BACKEND_REVISION:
        raise ValueError("后端测试支持库版本与已验证的交易契约不符")
    folder = Path(checkout).resolve() / "tests/preview_api"
    sys.path.insert(0, str(folder))
    import credit_live
    import no_live
    import test_trade_identity

    return SimpleNamespace(credit=credit_live, cash=no_live, trade=test_trade_identity)


def checked_api(client, run, support, record, *, market, allow_client_signing=False):
    prepared = {}
    writes = {BOOK + "orders/prepare", BOOK + "orders/submit", BOOK + "order/cancel"}

    def api(method, path, status, *, params=None, token=None, json=None, timeout=10):
        if method != "GET" and (method != "POST" or path not in writes):
            raise ValueError("交易 fixture 拒绝未列入范围的操作")
        if path == BOOK + "orders/prepare":
            run.check_window()
            if prepared or token is None:
                raise ValueError("每轮只允许一次已登录 prepare")
            expected = {
                **market, "venue": "buzzing", "side": run.side, "order_type": run.order_type,
                "use_credit": run.funding == "credit", "post_only": run.scenario == LIMIT_CANCEL,
            }
            if run.side == "BUY":
                expected["total_budget"] = str(run.budget_raw)
            else:
                expected.update(quantity=str(run.quantity_raw))
            if run.order_type == "LIMIT":
                expected["price"] = str(run.price)
            elif any(field in json for field in ("price", "expiration")):
                raise ValueError("市价单不得使用限价单字段")
            if any(json.get(key) != value for key, value in expected.items()):
                raise ValueError("prepare 请求与本轮场景配置不符")
            prepared["started"] = True
            record(stage="prepare_attempt")
        if path == BOOK + "orders/submit":
            run.check_window()
            if ("submitted" in prepared or not prepared.get("plan") or json.get("plan_id") != prepared["plan"]
                    or prepared["session"] != token):
                raise ValueError("submit 必须使用本轮同一会话和计划，且只发送一次")
            if datetime.now(timezone.utc) >= prepared["expires"]:
                raise ValueError("本轮计划已过期")
            signature = json.get("cash_signature")
            requires_signature = prepared["route"] == "client"
            valid_signature = isinstance(signature, str) and fullmatch(SIGNATURE_PATTERN, signature)
            if (requires_signature and not valid_signature) or (not requires_signature and signature):
                raise ValueError("签名字段与准备单资金来源不符")
            prepared["submitted"] = True
            record(stage="submit_attempt", logical_order_id=prepared["logical"])
        try:
            response = request(client, method, path, operation=path.rsplit("/", 1)[-1], params=params,
                               headers={"Authorization": "Bearer " + token} if token else {}, json=json, timeout=timeout)
            if response.status_code != status:
                if path in {BOOK + "orders/prepare", BOOK + "orders/submit", BOOK + "order/cancel"}:
                    record(stage="trade_http_error", operation=path.rsplit("/", 1)[-1], status=response.status_code)
                raise ValueError(f"HTTP {response.status_code}")
            reply = response.json()
            if not isinstance(reply, dict):
                raise ValueError("回执不是 JSON 对象")
            body = support.cash.data(reply)
        except (RuntimeError, ValueError, pytest.fail.Exception) as error:
            if path == BOOK + "orders/prepare":
                record(stage="prepare_result_unknown", error_type=type(error).__name__)
            pytest.fail(f"{method} {path} 未取得有效回执 ({type(error).__name__})", pytrace=False)
        if path == BOOK + "orders/prepare":
            plan, logical = support.cash.plan_ids(reply)
            route = signing_route(body)
            record(stage="prepared", plan_id=plan, logical_order_id=logical, signing_route=route)
            if run.funding == "cash" and route == "client" and not allow_client_signing:
                pytest.fail("自营 Cash prepare 要求客户端钱包签名，当前计划未启用服务端代签", pytrace=False)
            routes = {"server", "client"} if allow_client_signing else {"server"}
            if body.get("venue") != "buzzing" or route not in (routes if run.funding == "cash" else {"credit"}):
                raise ValueError("prepare 的签名分支与本轮批准资金不符")
            if run.funding == "credit":
                for leg in body["legs"]:
                    expires = leg.get("batch_expires_at")
                    if not expires or timestamp(expires) <= datetime.now(timezone.utc):
                        raise ValueError("Credit 腿缺少有效的到期时间")
            prepared.update(plan=plan, logical=logical, session=token, route=route, reserve=body.get("credit_reserve_amount"),
                            expires=timestamp(body["plan_expires_at"]))
        if path == BOOK + "orders/submit":
            legs = body.get("results")
            if (body.get("replayed") is True or body.get("logical_order_id") != prepared["logical"]
                    or not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict)
                    or type(legs[0].get("success")) is not bool):
                prepared["receipt_error"] = "提交回执与本轮首次下单不符"
            elif (run.funding == "credit" and legs[0].get("success") is True
                  and body.get("credit_reserved") != prepared["reserve"]):
                prepared["receipt_error"] = "Credit 实际预留额与方案不符"
            if "receipt_error" in prepared:
                record(stage="receipt_mismatch", logical_order_id=prepared["logical"])
                pytest.fail(prepared["receipt_error"], pytrace=False)
        return reply

    def depth(session, *, initialize=False):
        size, best_ask, best_bid = read_trade_depth(api, support.cash, session, market, run)
        record(stage="depth_checked", eligible_quantity_raw=size, best_ask=best_ask, best_bid=best_bid)
        if initialize and run.order_type == "LIMIT":
            record(
                stage="limit_ticket_expected", best_ask=best_ask, best_bid=best_bid,
                expected_buy_limit=expected_limit_price(best_ask, side="BUY"),
                expected_sell_limit=expected_limit_price(best_bid, side="SELL"),
            )
        return size

    def verify_receipt():
        if "receipt_error" in prepared:
            raise ValueError(prepared["receipt_error"])

    return api, depth, verify_receipt


class OrderbookApiResponses:
    @staticmethod
    def data(reply):
        if "code" in reply and reply["code"] != 0:
            raise ValueError("自营交易接口返回业务错误")
        body = reply.get("data", reply)
        if not isinstance(body, dict):
            raise ValueError("自营交易回包数据应为 JSON 对象")
        return body

    @staticmethod
    def list_rows(reply):
        rows = OrderbookApiResponses.data(reply).get("items", [])
        if not isinstance(rows, list):
            raise ValueError("自营交易列表的 items 应为数组")
        return rows

    @staticmethod
    def plan_ids(reply):
        ids = plan_identifiers(OrderbookApiResponses.data(reply))
        return ids["plan_id"], ids["logical_order_id"]

    @staticmethod
    def trading_profile(api, session, uid, maker):
        profile = OrderbookApiResponses.data(api("GET", "/api/v1/user/info", 200, token=session))
        if str(profile.get("user_id")) != str(uid) or str(profile.get("smart_wallet_address", "")).lower() != maker.lower():
            raise ValueError("资金快照与本轮自营交易钱包不符")
        funds = profile.get("polymarket")
        if not isinstance(funds, dict):
            raise ValueError("钱包资料缺少自营 USDB 主余额和冻结字段")
        return funds


def execute_orderbook_cash_api(client, session, run, uid, signer, maker, record, data, *, wallet_page=None):
    support = SimpleNamespace(cash=OrderbookApiResponses())
    cash = support.cash
    list_params = data["market_cases"]["orderbook_binary"]["list_params"]
    partial = None
    if run.select_from_volume:
        volume_params = {"page_size": data["market_cases"]["home_list"]["page_size"], "sort_by": "volume"}
        market, run = select_volume_limit_market(client, run, volume_params, record)
    elif run.partial_budget_cap_raw:
        market, partial = select_partial_cancel_market(
            client, {**list_params, "sort_by": "newest"}, record, max_budget_raw=run.partial_budget_cap_raw,
        )
        run = replace(run, price=partial.price, selected_budget_raw=partial.budget_raw)
    elif run.market_ids:
        read_api, _, _ = checked_api(client, run, support, record, market={})
        market = select_trade_market(client, read_api, cash, session, run, uid, maker, record, list_params)
    else:
        event = expect_json(get_orderbook_event(client, OWN_TEST_EVENT_ID), 200)
        detail = next((m for m in event["markets"] if str(m["id"]) == MARKET_ID), None)
        if (str(event["event"]["id"]) != OWN_TEST_EVENT_ID or detail is None
                or detail.get("market_type") != "orderbook" or market_closed_reason(detail)):
            raise ValueError("固定自营测试盘口当前不可交易")
        option = next(o for o in detail["options"] if o["outcome_label"].upper() == run.outcome)
        market = {"condition_id": detail["condition_id"], "token_id": option["position_id"]}
        record(stage="market_selected", market_id=MARKET_ID, event_id=OWN_TEST_EVENT_ID,
               condition_id=market["condition_id"], position_id=market["token_id"])
    api, depth, verify_receipt = checked_api(
        client, run, support, record, market=market, allow_client_signing=wallet_page is not None,
    )
    if not run.select_from_volume:
        depth(session, initialize=True)
    if run.side == "BUY":
        check_buying_capacity(cash, api, record, session, market, budget_raw=run.budget_raw)

    expected_fills = None

    def prepare():
        nonlocal expected_fills
        if not run.select_from_volume:
            depth(session)
        reply = api("POST", BOOK + "orders/prepare", 200, token=session, timeout=20,
                    json=prepare_payload(run, market, signer, maker))
        body = cash.data(reply)
        leg = body["legs"][0]
        quantity, quote, fee = (raw_amount(leg[key]) for key in ("quantity", "quote_amount", "est_fee"))
        if str(leg.get("side")).upper() != run.side or quantity == 0:
            raise ValueError("自营代签准备单的方向或委托份额不符")
        if run.side == "BUY" and (not 0 < quote <= quote + fee <= run.budget_raw
                                  or quote > raw_at_price(quantity, run.price)):
            raise ValueError("自营代签买入金额超出限价或批准预算")
        if run.side == "SELL" and (quantity != run.quantity_raw or quote < raw_at_price(quantity, run.price)
                                   or not 0 <= fee < quote):
            raise ValueError("自营代签卖出份额或预计所得与本轮配置不符")
        if run.select_from_volume:
            expected_fills = expected_volume_limit_fills(client, market, run, quantity, record)
        elif partial and (read_partial_cancel_quote(client, market["token_id"]) != partial
                        or quantity <= partial.quantity_raw):
            raise ValueError("自营部分成交盘口已变动或准备单未留出余单")
        elif partial is None and run.scenario == LIMIT_PARTIAL_CANCEL and depth(session) >= quantity:
            raise ValueError("当前盘口深度足以全成，无法验证部分成交后撤销")
        plan_id, logical_id = cash.plan_ids(reply)
        if wallet_page is not None and signing_route(body) == "client":
            order_hash, signature = sign_limit_cancel_plan(wallet_page, body, run, market, signer, maker)
            record(stage="signed", plan_id=plan_id, logical_order_id=logical_id, order_hash=order_hash)
            return CashPlan(plan_id, logical_id, order_hash, signature, quantity)
        return CashPlan(plan_id, logical_id, None, "", quantity)

    def check_fills(fills):
        verify_volume_limit_fills(fills, expected_fills, record)

    execute_cash(cash, api, record, session, run, uid, maker, prepare, market=market,
                 check_fills=check_fills if run.select_from_volume else None)
    verify_receipt()


def execute_trade(run, record, *, btc_buy=None, nba_buy=None, sell_nba=False, sports_buy=None):
    if get_environment() != "preview":
        raise ValueError("交易 fixture 只支持 Preview")
    if sell_nba and (run.scenario != POLYMARKET_NBA_LIMIT_BUY or nba_buy is None):
        raise ValueError("NBA 限价卖出缺少本轮已确认的买单")
    run.check_window()
    data = load_test_data()
    name, account = get_test_account(data)
    if run.account != name:
        raise ValueError("交易运行账号与本轮 CI 配置不一致")
    login = (preview_wallet_session(account) if run.scenario == LIMIT_CANCEL
             else nullcontext((get_test_jwt(name, account), None)))
    with login as (session, wallet_page), create_client(session) as client:
        uid, signer, maker = read_btc_trade_wallet(client)
        if "expected_uid" in account and uid != str(account["expected_uid"]):
            raise ValueError("交易钱包 UID 与 CI 测试账号不一致")
        client.headers["X-Wallet-Address"] = maker
        record(stage="identity_checked", uid=uid)
        if wallet_page is not None:
            verify_bound_wallet(wallet_page, signer, maker)

        if run.scenario == POLYMARKET_SPORTS_DRAW_BUY:
            if sports_buy is not None:
                return execute_sports_draw_sell(client, run, uid, signer, maker, record, sports_buy)
            return execute_sports_draw_buy(client, run, uid, signer, maker, record,
                                           data["market_cases"]["sports_match_blocks"])
        if run.scenario in POLYMARKET_NBA_SCENARIOS:
            if sell_nba:
                return execute_nba_limit_sell(client, run, uid, signer, maker, record, nba_buy)
            if nba_buy is not None:
                return verify_nba_limit_replay(client, run, uid, record, nba_buy)
            return execute_nba_limit_buy(client, run, uid, signer, maker, record, data["market_cases"]["nba_limit_buy"])
        if run.scenario in POLYMARKET_SCENARIOS:
            volume_list = {"sort_by": "volume", "page_size": data["market_cases"]["home_list"]["page_size"]}
            return execute_market_cycle(client, run, uid, signer, maker, record, volume_list, btc_buy)
        if run.funding == "cash":
            return execute_orderbook_cash_api(
                client, session, run, uid, signer, maker, record, data, wallet_page=wallet_page,
            )

        checkout = os.getenv("BUZZING_TRADE_BACKEND")
        if not checkout:
            pytest.fail("显式指定的自营 Credit 场景需要交易支持库", pytrace=False)
        support = backend_support(checkout)
        market = support.trade.TRADE_NO_MARKET
        api, depth, verify_receipt = checked_api(client, run, support, record, market=market)
        depth(session, initialize=True)

        def prepare():
            depth(session)
            return api("POST", BOOK + "orders/prepare", 200, token=session, timeout=20,
                       json=prepare_payload(run, market, signer, maker))

        execute_credit(support, api, record, session, run, uid, maker, prepare)
        verify_receipt()


def check_buying_capacity(cash, api, record, session, market, *, budget_raw=BUY_BUDGET_RAW):
    params = {"condition_id": market["condition_id"], "token_id": market["token_id"]}
    funds = cash.data(api("GET", BOOK + "buying-power", 200, token=session, params=params))
    balance, buying_power = (raw_amount(funds[field]) for field in ("balance", "buying_power"))
    if min(balance, buying_power) < budget_raw:
        raise ValueError("所选方向的 Cash 余额或购买力不足以覆盖本轮预算")
    record(stage="buying_power_checked", balance_raw=balance, buying_power_raw=buying_power)


def prepare_payload(run, market, signer, maker):
    payload = {
        **market, "venue": "buzzing", "side": run.side, "order_type": run.order_type,
        "use_credit": run.funding == "credit", "post_only": run.scenario == LIMIT_CANCEL, "is_neg_risk": False,
        "maker_address": maker, "signer_address": signer, "signature_type": 2,
    }
    if run.order_type == "LIMIT":
        payload.update(price=str(run.price), total_budget=str(run.budget_raw), expiration=str(int(time()) + 240))
    elif run.side == "BUY":
        payload["total_budget"] = str(run.budget_raw)
    else:
        payload["quantity"] = str(run.quantity_raw)
    return payload


def execute_credit(support, api, record, session, run, uid, maker, prepare):
    cash, market = support.cash, support.trade.TRADE_NO_MARKET
    prepared = {}

    def prepare_credit():
        reply = prepare()
        prepared["logical_id"] = cash.plan_ids(reply)[1]
        prepared["quantity"] = raw_amount(cash.data(reply)["legs"][0]["quantity"])
        return reply

    support.credit.execute_credit(api, record, session, run.account, uid, maker, market,
                                  BUY_BUDGET_RAW, run.price, run.batch_id, prepare_credit)
    query = order_query(market)
    order = own_order(api, cash, session, query, prepared["logical_id"], uid, run)
    if order is None or order["status"] not in {"filled", "cancelled"}:
        raise ValueError("Credit 订单终态缺失")
    amounts = OrderAmounts.read(order, prepared["quantity"])
    fills = order_fills(api, cash, session, query, order, run, uid)
    if not fills or sum(raw_amount(row["quote_amount"]) for row in fills) != amounts.quote:
        raise ValueError("Credit 订单累计成交额与成交记录不符")
    record(stage="quantity_checked", logical_order_id=prepared["logical_id"], order_id=order["order_id"],
           ordered_raw=amounts.quantity, quantity_raw=amounts.filled, remaining_raw=amounts.remaining,
           status=order["status"])
    check_order_views(api, cash, record, session, order, run, amounts)
