import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep, time
from types import SimpleNamespace

import httpx
import pytest

from function.api_client import create_client, request
from function.settings import get_environment
from function.trade_cash import execute_cash
from function.trade_run import check_depth, signing_route, timestamp


BACKEND_REVISION = "04bdad8214319d1d7a8a8d4651c5e1219f94992a"
BOOK = "/api/v1/orderbook/"


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
                           trade=test_trade_identity, mail=gmail_otp, sample=gmail_sample, bundle=bundle)


def checked_api(client, run, support, record):
    market = support.trade.TRADE_NO_MARKET
    position_id = market["token_id"]
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
            prepared["started"] = True
        if path == BOOK + "orders/submit":
            run.check_window()
            if ("submitted" in prepared or not prepared.get("plan") or json.get("plan_id") != prepared["plan"]
                    or prepared["session"] != token):
                raise ValueError("submit 必须使用本轮同一会话和计划，且只发送一次")
            if datetime.now(timezone.utc) >= prepared["expires"]:
                raise ValueError("本轮计划已过期")
            prepared["submitted"] = True
            record(stage="submit_attempt", logical_order_id=prepared["logical"])
        try:
            response = request(client, method, path, operation=path.rsplit("/", 1)[-1], params=params,
                               headers={"Authorization": "Bearer " + token} if token else {}, json=json, timeout=timeout)
            if response.status_code != status:
                raise ValueError(f"HTTP {response.status_code}")
            reply = response.json()
            if not isinstance(reply, dict):
                raise ValueError("回执不是 JSON 对象")
            body = support.cash.data(reply)
        except (RuntimeError, ValueError, pytest.fail.Exception) as error:
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
                    or not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict)):
                prepared["receipt_error"] = "提交回执与本轮首次下单不符"
            elif (run.funding == "credit" and legs[0].get("success") is True
                  and body.get("credit_reserved") != prepared["reserve"]):
                prepared["receipt_error"] = "Credit 实际预留额与方案不符"
            if "receipt_error" in prepared:
                record(stage="receipt_mismatch", logical_order_id=prepared["logical"])
                pytest.fail(prepared["receipt_error"], pytrace=False)
        return reply

    def depth(session):
        reply = api("GET", BOOK + "depth", 200, token=session, params={"source": "orderbook", "position_id": position_id})
        size = check_depth(support.cash.data(reply), position_id, support.trade.TRADE_LIMIT, support.trade.TRADE_BUDGET)
        record(stage="depth_checked", ask_quantity_raw=size)

    def verify_receipt():
        if "receipt_error" in prepared:
            raise ValueError(prepared["receipt_error"])

    return api, depth, verify_receipt


@contextmanager
def wallet_session(support, api, run, record):
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
            sdk = identity.load_privy_sdk(page, support.bundle, app_id, trade_email=address, funded_rotation=rotated)
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


def execute_trade(run, record):
    if get_environment() != "preview":
        raise ValueError("交易 fixture 只支持 Preview")
    run.check_window()
    support = backend_support(os.environ["BUZZING_TRADE_BACKEND"])
    trade = support.trade
    with create_client() as client:
        api, depth, verify_receipt = checked_api(client, run, support, record)
        with wallet_session(support, api, run, record) as (sdk, session, uid, signer, maker):
            record(stage="identity_checked", uid=uid, backend_revision=BACKEND_REVISION)
            trade.check_followup_funding(api, record, session, run.account, uid, maker)
            depth(session)
            run.check_window()
            if run.funding == "cash":
                trade.check_cash_readiness(api, record, session, run.account, uid)

                def prepare_cash():
                    depth(session)
                    return trade.prove_no_order(api, record, sdk, session, run.account, uid, signer, maker,
                                                for_submit=True, window_end=run.end)

                execute_cash(support, api, record, sdk, session, run, uid, signer, maker, prepare_cash)
            else:
                def prepare_credit():
                    depth(session)
                    return api("POST", BOOK + "orders/prepare", 200, token=session, timeout=20, json={
                        **trade.TRADE_NO_MARKET, "venue": "buzzing", "side": "BUY", "order_type": "LIMIT",
                        "price": str(trade.TRADE_LIMIT), "total_budget": str(trade.TRADE_BUDGET), "use_credit": True,
                        "post_only": False, "is_neg_risk": False, "expiration": str(int(time()) + 240),
                        "maker_address": maker, "signer_address": signer, "signature_type": 2,
                    })

                support.credit.execute_credit(api, record, session, run.account, uid, maker, trade.TRADE_NO_MARKET,
                                              trade.TRADE_BUDGET, trade.TRADE_LIMIT, run.batch_id, prepare_credit)
            verify_receipt()
