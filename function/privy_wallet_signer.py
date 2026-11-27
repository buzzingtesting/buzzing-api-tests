import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from re import fullmatch

from function.api_client import create_client
from function.identity import login_with_privy_identity_token, read_login_code, verify_login_session
from function.privy_email import privy_app_config
from function.settings import WEB_ORIGINS, get_environment
from function.trade_run import raw_amount, timestamp
from function.trade_state import raw_at_price


BUNDLE = Path(__file__).resolve().parents[1] / "privy_signer" / "build" / "signer.js"
BASE_SEPOLIA_EXCHANGE = "0x3d30e9cac3620793c29724cc350bbb486b101456"
SIGNATURE_PATTERN = r"0x[0-9a-fA-F]{130}"


@contextmanager
def preview_wallet_session(account):
    if get_environment() != "preview":
        raise ValueError("钱包签名用例仅用于 Preview")
    if not BUNDLE.is_file():
        raise ValueError("缺少 Preview Privy SDK 构建产物，请先构建 privy_signer")

    from playwright.sync_api import Error as BrowserError, sync_playwright

    origin = WEB_ORIGINS["preview"]
    page_url = origin + "/__buzzing_ci_wallet_signer"
    bundle_url = page_url + ".js"
    config = json.dumps(privy_app_config())
    html = (f'<!doctype html><meta charset="utf-8"><div id="app"></div>'
            f'<script>window.previewWalletConfig={config}</script><script src="{bundle_url}"></script>')
    launch_args = []
    if sys.platform == "darwin" and os.getenv("BUZZING_PREVIEW_CHROMIUM_SINGLE_PROCESS") == "1":
        launch_args = ["--single-process"]
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(args=launch_args, timeout=15000)
        except BrowserError:
            raise RuntimeError("无法启动 Preview 钱包签名所需的 Chromium") from None
        with browser.new_context(service_workers="block") as context:
            page = context.new_page()
            page.route(page_url, lambda route: route.fulfill(body=html, content_type="text/html"))
            page.route(bundle_url, lambda route: route.fulfill(path=BUNDLE, content_type="text/javascript"))
            try:
                step = "初始化"
                page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function("window.previewWalletSigner?.ready", timeout=45000)
                step = "发起邮箱验证"
                code = read_login_code(
                    account, lambda: page.evaluate("email => window.previewWalletSigner.sendCode(email)", account["email"])
                )
                step = "验证邮箱并获取身份凭证"
                page.evaluate("code => window.previewWalletSigner.login(code)", code)
                page.wait_for_function("window.previewWalletSigner?.identityToken", timeout=30000)
                identity_token = page.evaluate("window.previewWalletSigner.identityToken")
            except BrowserError:
                raise RuntimeError(f"Preview Privy SDK 在{step}时失败") from None
            with create_client() as api:
                login = login_with_privy_identity_token(api, identity_token)
            verify_login_session(login, account)
            yield login["token"], page


def verify_bound_wallet(page, signer, maker):
    from playwright.sync_api import Error as BrowserError

    try:
        page.wait_for_function(
            "([signer, maker]) => window.previewWalletSigner?.hasWallet(signer, maker)",
            arg=[signer, maker], timeout=30000,
        )
    except BrowserError:
        raise RuntimeError("Preview Privy SDK 的签名钱包或交易钱包与当前 CI 会话不一致") from None


def sign_limit_cancel_plan(page, plan, run, market, signer, maker):
    from playwright.sync_api import Error as BrowserError

    cash_leg = plan["cash_leg"]
    order_hash = str(cash_leg.get("order_hash", "")).lower()
    leg = plan["legs"][0]
    if (not fullmatch(r"0x[0-9a-f]{64}", order_hash) or order_hash != str(leg.get("order_hash", "")).lower()
            or plan.get("credit_applied")):
        raise ValueError("Cash 签名计划的哈希或资金来源不符")
    try:
        typed = json.loads(cash_leg["typed_data_json"])
        domain, message = typed["domain"], typed["message"]
        quote = raw_amount(str(message["makerAmount"]))
        quantity = raw_amount(str(message["takerAmount"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("Cash 计划缺少有效的 EIP-712 数据") from None
    if (typed.get("primaryType") != "Order" or str(domain.get("chainId")) != "84532"
            or str(domain.get("verifyingContract", "")).lower() != BASE_SEPOLIA_EXCHANGE
            or any(str(message.get(key, "")).lower() != address.lower()
                   for key, address in (("maker", maker), ("recipient", maker), ("signer", signer)))
            or str(message.get("tokenId")) != market["token_id"]
            or str(message.get("side")) != "0" or str(message.get("signatureType")) != "2"
            or quantity != raw_amount(leg["quantity"]) or quote != raw_amount(leg["quote_amount"])
            or not 0 < quote <= min(run.budget_raw, raw_at_price(quantity, run.price))):
        raise ValueError("Cash 签名数据与本轮账号、盘口、方向或预算不符")

    run.check_window()
    expires = timestamp(plan["plan_expires_at"])
    if expires <= datetime.now(timezone.utc):
        raise ValueError("Cash 计划在钱包签名前已过期")
    try:
        signature = page.evaluate(
            """async ({signer, maker, typed, orderHash}) => {
                const sdk = window.previewWalletSigner;
                if (!sdk.hasWallet(signer, maker)) throw new Error('Wallet binding mismatch');
                let timer;
                try {
                    return await Promise.race([
                        sdk.sign(signer, typed, orderHash),
                        new Promise((_, reject) => {
                            timer = setTimeout(() => reject(new Error('Wallet signing timed out')), 20000);
                        }),
                    ]);
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {"signer": signer, "maker": maker, "typed": typed, "orderHash": order_hash},
        )
    except BrowserError:
        raise RuntimeError("Preview Privy SDK 未能签署本轮 Cash 计划或恢复匹配的签名地址") from None
    if not isinstance(signature, str) or not fullmatch(SIGNATURE_PATTERN, signature):
        raise ValueError("Preview Privy SDK 未返回有效的 Cash 签名")
    return order_hash, signature
