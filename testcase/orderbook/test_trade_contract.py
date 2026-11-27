from datetime import datetime, timedelta, timezone
from time import sleep

import allure
import pytest

from function.api_client import create_client, request
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_envelope, expect_json
from function.settings import get_environment
from function.trade_contract import (
    TradeFixture,
    attach_evidence,
    expect_trade_problem,
    funding_snapshot,
    plan_identifiers,
    require_no_order,
    unavailable_trade_fixture,
)
from function.trade_run import signing_route, timestamp
from function.trade_state import BOOK


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("Orderbook 盘口")),
    allure.sub_suite("交易高风险分支"),
    allure.epic(PROJECT_NAME),
    allure.feature("交易高风险分支"),
]



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
    with fixture.connect() as (client, _, signer, maker):
        selected = fixture.select_market(client)
        attach_evidence(selected, "本轮选中市场")
        funding_before = funding_snapshot(client)
        plan = expect_json(fixture.prepare(client, selected, signer, maker), 200)
        ids = plan_identifiers(plan)
        legs = plan["legs"]
        assert (plan["venue"] == "buzzing" and len(legs) == 1 and legs[0]["funding_source"] == "cash"
                and not plan.get("credit_applied")), "需要自有 Cash 单腿计划"
        route = signing_route(plan)
        if route == "server":
            pytest.skip("当前 Cash 计划走服务端代签，本用例需要客户端验签")
        assert route == "client", "需要客户端签名的 Cash 计划"
        expires = timestamp(plan["plan_expires_at"])
        now = datetime.now(timezone.utc)
        is_expired_case = scenario == "方案已过期"
        if is_expired_case:
            deadline = expires + timedelta(seconds=2)
            if deadline >= fixture.end or (deadline - now).total_seconds() > 90:
                unavailable_trade_fixture("计划的过期点超出本轮窗口或 90 秒等待上限")
            sleep(max(0, (deadline - now).total_seconds()))
        require_no_order(client, fixture.source, ids["logical_order_id"])
        fixture.check_window()
        if not is_expired_case and expires <= datetime.now(timezone.utc) + timedelta(seconds=5):
            unavailable_trade_fixture("签名分支的计划已接近过期")
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
