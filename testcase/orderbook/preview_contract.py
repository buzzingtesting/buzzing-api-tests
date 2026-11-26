import os
from uuid import uuid4

import allure
import pytest

from function.api_client import create_client, request
from function.reporting import PROJECT_NAME
from function.settings import get_environment
from function.trade_run import raw_amount
from function.trade_state import BOOK


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("Orderbook 盘口"),
    allure.sub_suite("接口契约"),
    allure.epic(PROJECT_NAME),
    allure.feature("Orderbook 盘口"),
]


@pytest.fixture
def preview_get():
    if get_environment() != "preview":
        pytest.fail("临时身份只允许访问 Preview", pytrace=False)
    token = os.getenv("BUZZING_M03_PREVIEW_TOKEN", "").strip().removeprefix("Bearer ")
    if not token:
        pytest.skip("未注入临时 Preview Token")
    with create_client(bearer_token=token) as client:
        def get(path, params=None):
            reply = request(client, "GET", path, operation=path.rsplit("/", 1)[-1], params=params)
            if reply.status_code != 200:
                pytest.fail(f"{path} 返回 HTTP {reply.status_code}", pytrace=False)
            body = reply.json()
            if not isinstance(body, dict) or ("code" in body and body["code"] != 0):
                pytest.fail(f"{path} 未返回成功的对象", pytrace=False)
            return body.get("data", body)

        yield get


def test_m03_preview_user_funding_contract(preview_get):
    session = preview_get("/api/v1/auth/session")
    profile = preview_get("/api/v1/user/info")
    funding = preview_get("/api/v1/profile/funding")
    assert str(session["user_id"]) == str(profile["user_id"])
    assert session["auth_level"] in {1, 2}
    assert not funding["cash_degraded"] and not funding["credit_degraded"]
    info = profile["polymarket"]
    for field in ("usdb_balance", "usdb_frozen_amount"):
        raw_amount(info[field])


def test_m03_preview_logical_id_filter_contract(preview_get):
    params = {"source": "orderbook", "logical_order_ids": str(uuid4()), "page_size": 1}
    assert preview_get(BOOK + "orders", params).get("items", []) == []
    history = preview_get(BOOK + "orders", {"source": "orderbook", "view": "history", "page_size": 1})
    for item in history.get("items", []):
        order = item["order"]
        for field in ("quantity", "filled_quantity", "remaining_quantity", "filled_quote"):
            raw_amount(order[field])
