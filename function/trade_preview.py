import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
from types import SimpleNamespace

import httpx
import pytest

from function.api_client import create_client, request
from function.identity import get_test_jwt
from function.polymarket_limit_trade import execute_nba_limit_buy
from function.polymarket_replay import verify_nba_limit_replay
from function.polymarket_trade import execute_market_cycle, read_btc_trade_wallet
from function.settings import get_environment, load_test_data
from function.trade_cash import check_order_views, execute_cash, sign_plan
from function.trade_market import expected_limit_price, read_trade_depth, select_trade_market
from function.trade_market import read_partial_cancel_quote, select_partial_cancel_market
from function.trade_run import (
    BUY_BUDGET_RAW, CI_TRADE_ACCOUNT, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, MARKET_SELL, POLYMARKET_BTC_BUY,
    POLYMARKET_NBA_SCENARIOS, POLYMARKET_SCENARIOS, raw_amount, signing_route, timestamp,
)
from function.trade_state import BOOK, OrderAmounts, order_fills, order_query, own_order


BACKEND_REVISION = "04bdad8214319d1d7a8a8d4651c5e1219f94992a"


def backend_support(checkout):
    revision = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    if revision != BACKEND_REVISION:
        raise ValueError("后端测试支持库版本与已验证的交易契约不符")
    folder = Path(checkout).resolve() / "tests/preview_api"
    bundle = folder / "privy_sdk/build/login.js"
    if not bundle.is_file():
        raise ValueError("请先在固定版本的 privy_sdk 目录构建浏览器 SDK")
    sys.path.insert(0, str(folder))
    import credit_live
    import gmail_otp
    import gmail_sample
    import no_live
    import test_identity
    import test_trade_identity

    return SimpleNamespace(credit=credit_live, cash=no_live, identity=test_identity,
                           trade=test_trade_identity, mail=gmail_otp, sample=gmail_sample,
                           checkout=Path(checkout).resolve(), bundle=bundle)


@contextmanager
def signing_bundle(support, run):
    if run.scenario != MARKET_SELL:
        yield support.bundle
        return
    directory = support.bundle.parent.parent
    source = subprocess.check_output(
        ["git", "-C", support.checkout, "show", f"{BACKEND_REVISION}:tests/preview_api/privy_sdk/login.jsx"],
        text=True,
    )
    # Reuse the pinned SDK's wallet, exchange, typed data and signature recovery guards for SELL.
    signature_guard = "message.side !== 0"
    shape = "typedData, forSubmit = false})"
    if source.count(signature_guard) != 1 or source.count(shape) != 1:
        raise ValueError("固定版本签名 SDK 的校验结构有变")
    source = source.replace(shape, "typedData, forSubmit = false, expectedSide = 0})")
    source = source.replace(signature_guard, "![0, 1].includes(expectedSide) || message.side !== expectedSide")
    esbuild = directory / "node_modules/.bin/esbuild"
    with TemporaryDirectory(prefix="buzzing-m03-") as temp:
        bundle = Path(temp) / "login.js"
        subprocess.run(
            [esbuild, "--loader=jsx", "--sourcefile=login.jsx", "--bundle", "--platform=browser",
             "--format=iife", "--minify", f"--outfile={bundle}"],
            input=source, text=True, cwd=directory, check=True,
        )
        yield bundle


def checked_api(client, run, support, record, *, market=None):
    market = support.trade.TRADE_NO_MARKET if market is None else market
    prepared = {}
    writes = {"/api/v1/login/preview", "/api/v1/login", BOOK + "orders/prepare",
              BOOK + "orders/submit", BOOK + "order/cancel"}

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
            if bool(json.get("cash_signature")) != (run.funding == "cash"):
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
            record(stage="prepared", plan_id=plan, logical_order_id=logical)
            expected = "client" if run.funding == "cash" else "credit"
            if body.get("venue") != "buzzing" or signing_route(body) != expected:
                raise ValueError("prepare 的签名分支与本轮批准资金不符")
            if run.funding == "credit":
                for leg in body["legs"]:
                    expires = leg.get("batch_expires_at")
                    if not expires or timestamp(expires) <= datetime.now(timezone.utc):
                        raise ValueError("Credit 腿缺少有效的到期时间")
            prepared.update(plan=plan, logical=logical, session=token, reserve=body.get("credit_reserve_amount"),
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


@contextmanager
def wallet_session(support, api, run, record, bundle):
    from playwright.sync_api import sync_playwright

    identity, trade = support.identity, support.trade
    rotated = run.account in trade.ROTATION_FUNDED_WALLETS
    if rotated:
        uid, signer, maker = trade.ROTATION_FUNDED_WALLETS[run.account]
        address = trade.ROTATION_RECIPIENTS[run.account]
    else:
        uid, signer = trade.REGISTERED_TRADE_WALLETS[run.account]
        maker = trade.TRADE_WALLETS[run.account]
        address = support.sample.RECIPIENTS[run.account]
    app_id = identity.setting("PREVIEW_QA_PRIVY_APP_ID")
    cloud = identity.setting("PREVIEW_QA_GMAIL_CLOUD_ACCESS_TOKEN")
    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as gmail, sync_playwright() as driver:
        mailbox = support.sample.ACCOUNT
        access = support.mail.connect_mailbox(gmail, email=mailbox, workload_token=cloud, account="neo")
        inbox = support.mail.Inbox(gmail, email=address, sender=support.sample.SENDER, mailbox_email=mailbox,
                                   alternate_sender=support.sample.approved_b2_sender(gmail, access),
                                   preview_marker=trade.TRADE_APP_MARKER, subject=support.sample.SUBJECT, access_token=access)
        browser = driver.chromium.launch()
        with browser.new_context(service_workers="block") as context:
            page = identity.qa_page(context)
            sdk = identity.load_privy_sdk(page, bundle, app_id, trade_email=address, funded_rotation=rotated)
            run.check_window()
            stamp = inbox.before_request()
            identity.sdk_status(sdk.evaluate("(sdk, email) => sdk.send(email)", address), record, "send")
            code = inbox.wait_for_code(stamp, timeout=120)
            identity.sdk_status(sdk.evaluate("(sdk, code) => sdk.verify(code)", code), record, "verify")
            page.wait_for_function("({sdk, signer, maker}) => sdk.tradeReady() && sdk.hasTradeWallet(signer) && sdk.tradeSmartWallet() === maker",
                                   arg={"sdk": sdk, "signer": signer, "maker": maker}, timeout=45000)
            deadline = monotonic() + 20
            while True:
                result = sdk.evaluate("(sdk, maker) => sdk.tradeIdentityToken(maker)", maker)
                identity.sdk_status(result, record, "identity_token")
                if result.get("value"):
                    break
                identity.require(monotonic() < deadline, "缺少当前交易钱包的身份凭证")
                sleep(2)
            uid, session, _ = trade.buzz_login(api, result["value"], signer, uid)
            identity.require(trade.check_bound_smart_wallet(api, record, session, run.account, uid, signer,
                                                             maker, rotation=rotated), "交易钱包绑定不符")
            yield sdk, session, uid, signer, maker


def execute_trade(run, record, *, btc_buy=None, nba_buy=None):
    if get_environment() != "preview":
        raise ValueError("交易 fixture 只支持 Preview")
    run.check_window()
    if run.scenario in POLYMARKET_SCENARIOS:
        data = load_test_data()
        volume_list = {"sort_by": "volume", "page_size": data["market_cases"]["home_list"]["page_size"]}
        if run.account == CI_TRADE_ACCOUNT:
            token = get_test_jwt(run.account, data[run.account])
            with create_client(token) as client:
                uid, signer, maker = read_btc_trade_wallet(client)
                client.headers["X-Wallet-Address"] = maker
                record(stage="identity_checked", uid=uid)
                if run.scenario in POLYMARKET_NBA_SCENARIOS:
                    if nba_buy is not None:
                        return verify_nba_limit_replay(client, run, uid, record, nba_buy)
                    return execute_nba_limit_buy(client, run, uid, signer, maker, record, data["market_cases"]["nba_limit_buy"])
                return execute_market_cycle(client, run, uid, signer, maker, record, volume_list, btc_buy)
    checkout = os.getenv("BUZZING_TRADE_BACKEND")
    if run.partial_budget_cap_raw and not checkout:
        pytest.skip("Runner 尚未配置自营市场客户端签名所需的 BUZZING_TRADE_BACKEND")
    support = backend_support(os.environ["BUZZING_TRADE_BACKEND"])
    trade = support.trade
    with create_client() as client, signing_bundle(support, run) as bundle:
        api, depth, verify_receipt = checked_api(client, run, support, record)
        with wallet_session(support, api, run, record, bundle) as (sdk, session, uid, signer, maker):
            record(stage="identity_checked", uid=uid, backend_revision=BACKEND_REVISION)
            if run.scenario == POLYMARKET_BTC_BUY:
                with create_client(session) as trading_client:
                    trading_client.headers["X-Wallet-Address"] = maker
                    return execute_market_cycle(trading_client, run, uid, signer, maker, record, volume_list, btc_buy)
            market = trade.TRADE_NO_MARKET
            partial_quote = None
            if run.partial_budget_cap_raw:
                list_params = load_test_data()["market_cases"]["orderbook_binary"]["list_params"]
                market, partial_quote = select_partial_cancel_market(
                    client, {**list_params, "sort_by": "newest"}, record, max_budget_raw=run.partial_budget_cap_raw,
                )
                run = replace(run, price=partial_quote.price, selected_budget_raw=partial_quote.budget_raw)
                api, depth, verify_receipt = checked_api(client, run, support, record, market=market)
            else:
                trade.check_followup_funding(api, record, session, run.account, uid, maker)
                if run.market_ids:
                    list_params = load_test_data()["market_cases"]["orderbook_binary"]["list_params"]
                    market = select_trade_market(client, api, support.cash, session, run, uid, maker, record, list_params)
                    api, depth, verify_receipt = checked_api(client, run, support, record, market=market)
            depth(session, initialize=True)
            run.check_window()

            def prepare():
                depth(session)
                return api("POST", BOOK + "orders/prepare", 200, token=session, timeout=20,
                           json=prepare_payload(run, market, signer, maker))

            if run.funding == "cash":
                if run.side == "BUY":
                    if not run.market_ids and partial_quote is None:
                        trade.check_cash_readiness(api, record, session, run.account, uid)
                    check_buying_capacity(support.cash, api, record, session, market, budget_raw=run.budget_raw)

                def prepare_cash():
                    plan = sign_plan(support, sdk, record, run, signer, maker, prepare(), market=market)
                    if partial_quote is not None:
                        current = read_partial_cancel_quote(client, market["token_id"])
                        if current != partial_quote:
                            record(stage="partial_quote_moved", limit_price=str(partial_quote.price),
                                   eligible_quantity_raw=partial_quote.quantity_raw,
                                   observed_limit_price=str(current.price) if current else None,
                                   observed_quantity_raw=current.quantity_raw if current else 0)
                            pytest.skip("提交前卖一价格或份额已变动，本轮未提交订单")
                        if plan.quantity <= partial_quote.quantity_raw:
                            raise ValueError("准备单扣除预估手续费后未留出余单份额")
                        record(stage="partial_quote_rechecked", limit_price=str(partial_quote.price),
                               eligible_quantity_raw=partial_quote.quantity_raw, ordered_raw=plan.quantity,
                               budget_raw=run.budget_raw)
                    elif run.order_type == "MARKET" or run.scenario == LIMIT_PARTIAL_CANCEL:
                        size = depth(session)
                        if run.scenario == LIMIT_PARTIAL_CANCEL and size >= plan.quantity:
                            raise ValueError("当前限价内卖盘可全部成交，无法稳定验证部分成交后撤单")
                    return plan

                execute_cash(support, api, record, sdk, session, run, uid, signer, maker, prepare_cash, market=market)
            else:
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
