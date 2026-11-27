from decimal import Decimal
from time import time

import allure
import pytest

from function.api_client import request
from function.funds import get_buying_power
from function.market import is_open_market_event, iter_market_events
from function.polymarket_funding import execute_funded_limit
from function.polymarket_limit_trade import LIMIT_EXPIRATION_SECONDS, read_withdrawable_cash, select_nba_limit_quote
from function.polymarket_trade import ensure_market_trading, market_closed_reason, read_btc_trade_wallet, read_cash_position
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_json
from function.settings import get_environment
from function.trade_contract import (
    TradeFixture,
    attach_evidence,
    expect_trade_problem,
    funding_snapshot,
    plan_identifiers,
    read_scoped_market,
    require_no_order,
    unavailable_trade_fixture,
    verify_funding_plan,
)
from function.trade_run import raw_amount
from function.trade_state import BOOK


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("限价单")),
    allure.sub_suite("资金与份额"),
    allure.epic(PROJECT_NAME),
    allure.feature("限价单"),
]


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
@pytest.mark.skip(reason="暂时无法执行，待准备 Polymarket Credit 专用测试数据")
@pytest.mark.parametrize("scenario", ["polymarket_credit", "polymarket_mixed"], ids=["纯 Credit", "Cash + Credit"])
def test_polymarket_funding_plan(scenario):
    fixture = TradeFixture(scenario)
    with fixture.connect() as (client, uid, signer, maker):
        selected = fixture.select_market(client)
        attach_evidence(selected, "本轮专用市场")
        before = funding_snapshot(client)
        wallet_before = read_withdrawable_cash(client, uid)
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
        wallet_after = read_withdrawable_cash(client, uid)
        attach_evidence({**selected, **ids, "budget_raw": fixture.budget_raw, "market_credit_raw": market_credit,
                         "legs": legs, "before": before, "after": after,
                         "pUSD_before": str(wallet_before[1]), "pUSD_after": str(wallet_after[1]),
                         "withdrawable_before": str(wallet_before[0]), "withdrawable_after": str(wallet_after[0])})
        assert before == after and wallet_before == wallet_after, "只生成交易计划时资金快照应保持一致"
        if fixture.spec.get("submit") is not True:
            unavailable_trade_fixture("出资计划已核对，完整成交需要已批准的 submit=true 及有效交易窗口")

        def record(title, evidence):
            attach_evidence({**selected, **evidence}, title)

        execute_funded_limit(client, fixture, signer, selected, plan, ids, before, record, uid=uid)
