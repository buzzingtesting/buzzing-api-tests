import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import sleep, time

import allure
import pytest

from function.api_client import create_client, request
from function.funds import get_buying_power
from function.market import is_open_market_event, iter_market_events
from function.polymarket_funding import execute_funded_limit
from function.polymarket_limit_trade import LIMIT_EXPIRATION_SECONDS, read_withdrawable_cash, select_nba_limit_quote
from function.polymarket_trade import ensure_market_trading, market_closed_reason, read_btc_trade_wallet, read_cash_position
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_envelope, expect_json
from function.settings import get_environment
from function.trade_contract import TradeFixture, expect_trade_problem, funding_snapshot
from function.trade_contract import plan_identifiers, read_scoped_market, require_no_order, verify_funding_plan
from function.trade_run import raw_amount, signing_route, timestamp
from function.trade_state import BOOK


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("Orderbook 盘口")),
    allure.sub_suite("交易高风险分支"),
    allure.epic(PROJECT_NAME),
    allure.feature("交易高风险分支"),
]


def attach_evidence(evidence, title="交易契约核对"):
    allure.attach(json.dumps(evidence, ensure_ascii=False, indent=2), title, allure.attachment_type.JSON)


@allure.title("订单鉴权 · {identity}访问 {endpoint}")
@pytest.mark.parametrize("identity", ["未登录", "无效凭证"])
@pytest.mark.parametrize(
    ("method", "endpoint", "payload"),
    [("GET", "orders", None), ("POST", "orders/prepare", {}),
     ("POST", "orders/submit", {"plan_id": "invalid-ci-plan", "cash_signature": ""})],
    ids=["orders", "prepare", "submit"],
)
def test_trade_access_rejected(identity, method, endpoint, payload):
    if get_environment() != "preview":
        pytest.skip("订单鉴权用例仅在 Preview 验证")
    token = "ci.invalid.token" if identity == "无效凭证" else None
    with create_client(token) as client:
        kwargs = {} if payload is None else {"json": payload}
        response = request(client, method, BOOK + endpoint, operation="trade_access_rejection", **kwargs)
    expect_envelope(response, status=401, code=1005)
    attach_evidence({"identity": identity, "method": method, "endpoint": BOOK + endpoint,
                     "http_status": 401, "biz_code": 1005})


@allure.title("交易计划 · 格式错误的 plan_id 被拒绝")
def test_invalid_plan_id(authenticated_api_client):
    if get_environment() != "preview":
        pytest.skip("本用例仅在 Preview 验证")
    response = request(authenticated_api_client, "POST", BOOK + "orders/submit", operation="reject_invalid_plan_id",
                       json={"plan_id": "invalid-ci-plan", "cash_signature": ""})
    attach_evidence(expect_trade_problem(response, status=400, reason="INVALID_PARAMS", biz_code=1004))


@allure.title("自有 Cash 计划 · {scenario}")
@pytest.mark.b3_fixture
@pytest.mark.parametrize("scenario", ["签名不合法", "方案已过期"])
def test_cash_plan_rejected(scenario):
    fixture = TradeFixture("buzzing_plan")
    with fixture.connect() as (client, signer, maker):
        selected = fixture.select_market(client)
        attach_evidence(selected, "本轮专用市场")
        funding_before = funding_snapshot(client)
        plan = expect_json(fixture.prepare(client, selected, signer, maker), 200)
        ids = plan_identifiers(plan)
        legs = plan["legs"]
        assert (plan["venue"] == "buzzing" and signing_route(plan) == "client" and len(legs) == 1
                and legs[0]["funding_source"] == "cash" and not plan.get("credit_applied")), "需要自有 Cash 单腿客户端签名计划"
        expires = timestamp(plan["plan_expires_at"])
        now = datetime.now(timezone.utc)
        is_expired_case = scenario == "方案已过期"
        if is_expired_case:
            deadline = expires + timedelta(seconds=2)
            if deadline >= fixture.end or (deadline - now).total_seconds() > 90:
                pytest.skip("计划的过期点超出本轮窗口或 90 秒等待上限")
            sleep(max(0, (deadline - now).total_seconds()))
        require_no_order(client, fixture.source, ids["logical_order_id"])
        fixture.check_window()
        if not is_expired_case and expires <= datetime.now(timezone.utc) + timedelta(seconds=5):
            pytest.skip("签名分支的计划已接近过期")
        response = request(client, "POST", BOOK + "orders/submit", operation="reject_cash_plan",
                           json={"plan_id": ids["plan_id"], "cash_signature": "0x" + "00" * 65})
        evidence = {
            **selected, **ids, "scenario": scenario, "plan_expires_at": plan["plan_expires_at"],
            "http_status": response.status_code,
        }
        attach_evidence(evidence)
        require_no_order(client, fixture.source, ids["logical_order_id"])
        assert funding_snapshot(client) == funding_before, "拒绝提交后 Cash/Credit 快照应保持一致"
        expected = (409, "CREDIT_PLAN_STALE", 1035) if is_expired_case else (400, "INVALID_PARAMS", 1004)
        attach_evidence(expect_trade_problem(response, status=expected[0], reason=expected[1], biz_code=expected[2]))


@allure.title("Polymarket NBA 限价买入 · Cash 余额不足")
def test_insufficient_cash(authenticated_api_client, test_data):
    if get_environment() != "preview":
        pytest.skip("本用例仅在 Preview 验证")
    client = authenticated_api_client
    _, signer, maker = read_btc_trade_wallet(client)
    client.headers["X-Wallet-Address"] = maker

    selected = select_nba_limit_quote(client, lambda **_: None, test_data["market_cases"]["nba_limit_buy"])
    cash_before = funding_snapshot(client)
    power = expect_json(get_buying_power(client, condition_id=selected["condition_id"],
                                         token_id=selected["position_id"]), 200)
    balance = raw_amount(power["balance"])
    buying_power = raw_amount(power["buying_power"])
    minimum_raw = int(Decimal(selected["minimum_budget"]) * 1_000_000)
    total_budget = max(balance, buying_power, minimum_raw) + 1_000_000
    attach_evidence({**selected, "balance_raw": balance, "buying_power_raw": buying_power,
                     "total_budget_raw": total_budget}, "NBA 盘口与超余额 Amount")
    ensure_market_trading(client, selected, "Cash 余额不足用例")
    response = request(client, "POST", BOOK + "orders/prepare", operation="reject_insufficient_nba_cash",
                       json={"venue": "polymarket", "condition_id": selected["condition_id"],
                             "token_id": selected["position_id"], "side": "BUY", "order_type": "LIMIT",
                             "total_budget": str(total_budget), "price": selected["limit_price"], "use_credit": False,
                             "is_neg_risk": selected["neg_risk"], "signer_address": signer,
                             "maker_address": maker, "signature_type": 2,
                             "expiration": str(int(time()) + LIMIT_EXPIRATION_SECONDS)})
    attach_evidence(expect_trade_problem(response, status=400, reason="INSUFFICIENT_BALANCE", biz_code=1004))
    assert funding_snapshot(client) == cash_before, "拒绝下单后资金快照应保持一致"


def find_direction_without_shares(client, uid, params):
    for event in iter_market_events(client, params):
        if event.get("type") != "polymarket" or not is_open_market_event(event, time()):
            continue
        child = next((item for item in (event.get("markets") or []) if item["market"].get("outcome_kind") == "binary"), None)
        if child is None:
            continue
        market = child["market"]
        tick = Decimal(market.get("order_price_min_tick_size") or "0.01")
        if market_closed_reason(market) or not tick.is_finite() or tick <= 0 or Decimal("0.5") % tick:
            continue
        for option in child["options"]:
            outcome = option["outcome_label"].upper()
            if outcome not in {"YES", "NO", "UP", "DOWN"}:
                continue
            selected = read_scoped_market(client, "polymarket", event["id"], market["id"], outcome)
            if read_cash_position(client, uid, selected)[1] == 0:
                return selected
    pytest.fail("24hr Volume 中未找到有效的 Polymarket 零可卖份额方向", pytrace=False)


@allure.title("Polymarket 限价卖出 · 可卖份额不足")
def test_insufficient_shares(authenticated_api_client, test_data):
    if get_environment() != "preview":
        pytest.skip("本用例仅在 Preview 验证")
    client = authenticated_api_client
    uid, signer, maker = read_btc_trade_wallet(client)
    client.headers["X-Wallet-Address"] = maker
    params = test_data["market_cases"]["nba_limit_buy"]["list_params"]
    selected = find_direction_without_shares(client, uid, params)
    amount = Decimal(selected["minimum_shares"])
    assert amount.is_finite() and amount > 0, "Polymarket 最小下单量无效"
    quantity_raw = str(int(amount * 1_000_000))
    attach_evidence({**selected, "available_shares": "0", "requested_shares_raw": quantity_raw})
    before = funding_snapshot(client)
    response = request(client, "POST", BOOK + "orders/prepare", operation="reject_insufficient_polymarket_shares",
                       json={"venue": "polymarket", "condition_id": selected["condition_id"],
                             "token_id": selected["position_id"], "side": "SELL", "order_type": "LIMIT",
                             "quantity": quantity_raw, "price": "0.5", "use_credit": False,
                             "is_neg_risk": selected["neg_risk"], "signer_address": signer,
                             "maker_address": maker, "signature_type": 2, "expiration": str(int(time()) + 300)})
    attach_evidence(expect_trade_problem(response, status=400, reason="INSUFFICIENT_SHARES", biz_code=1004))
    assert funding_snapshot(client) == before
    assert read_cash_position(client, uid, selected)[1] == 0, "拒绝后仍应为零可卖份额"


@allure.title("Polymarket 出资计划与成交 · {scenario}")
@pytest.mark.b3_fixture
@pytest.mark.parametrize("scenario", ["polymarket_credit", "polymarket_mixed"], ids=["纯 Credit", "Cash + Credit"])
def test_polymarket_funding_plan(scenario):
    fixture = TradeFixture(scenario)
    with fixture.connect() as (client, signer, maker):
        selected = fixture.select_market(client)
        attach_evidence(selected, "本轮专用市场")
        before = funding_snapshot(client)
        wallet_before = read_withdrawable_cash(client, fixture.uid)
        power = expect_json(get_buying_power(client, condition_id=selected["condition_id"],
                                             token_id=selected["position_id"]), 200)
        market_credit = raw_amount(power["credit_available"])
        assert market_credit > 0 and power["credit_locked"] is False and not before["credit_locked"], (
            "专用账号在该盘口需要可交易的 Credit"
        )
        if scenario == "polymarket_mixed":
            assert market_credit < fixture.budget_raw, "混合资金用例需要该盘口可用 Credit 少于下单预算"
        else:
            assert market_credit >= fixture.budget_raw, "纯 Credit 用例需要该盘口可用 Credit 覆盖下单预算"
        plan = expect_json(fixture.prepare(client, selected, signer, maker, use_credit=True), 200)
        ids, legs = verify_funding_plan(plan, fixture)
        require_no_order(client, fixture.source, ids["logical_order_id"])
        after = funding_snapshot(client)
        wallet_after = read_withdrawable_cash(client, fixture.uid)
        attach_evidence({**selected, **ids, "budget_raw": fixture.budget_raw, "market_credit_raw": market_credit,
                         "legs": legs, "before": before, "after": after,
                         "pUSD_before": str(wallet_before[1]), "pUSD_after": str(wallet_after[1]),
                         "withdrawable_before": str(wallet_before[0]), "withdrawable_after": str(wallet_after[0])})
        assert before == after and wallet_before == wallet_after, "只生成交易计划时资金快照应保持一致"
        if fixture.spec.get("submit") is not True:
            pytest.skip("出资计划已核对，等待本轮专用数据的真实交易窗口")

        def record(title, evidence):
            attach_evidence({**selected, **evidence}, title)

        execute_funded_limit(client, fixture, signer, selected, plan, ids, before, record)
