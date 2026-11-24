from decimal import Decimal

import allure

from function.market import (
    get_orderbook_depth,
    load_binary_orderbook_market,
)
from function.market_account import check_market_account_activity
from function.reporting import PROJECT_NAME
from function.response_checks import expect_native


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("盘口与 Orderbook"),
    allure.epic(PROJECT_NAME),
    allure.feature("盘口与 Orderbook"),
]

DEPTH_LEVELS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["price", "total_qty"],
        "properties": {
            "price": {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$"},
            "total_qty": {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$"},
        },
    },
}


@allure.title("独立二元市场 · Orderbook 事件")
def test_binary_orderbook_event(authenticated_api_client, test_data):
    cases = test_data["market_cases"]
    market = load_binary_orderbook_market(authenticated_api_client, cases["orderbook_binary"]["list_params"])
    check_market_account_activity(authenticated_api_client, market, cases["market_account"])


@allure.title("独立二元市场 · 盘口深度")
def test_binary_orderbook_depth(api_client, test_data):
    case = test_data["market_cases"]["orderbook_binary"]
    market = load_binary_orderbook_market(api_client, case["list_params"])

    with allure.step("从 Orderbook 事件选项读取 position_id"):
        position_id = market["options"][0]["position_id"]
        allure.attach(position_id, name="盘口选项 position_id", attachment_type=allure.attachment_type.TEXT)

    with allure.step("查询盘口深度并核对选项归属"):
        depth = expect_native(
            get_orderbook_depth(api_client, position_id, source=case["depth_source"]),
            status=200,
            schema={
                "type": "object",
                "required": ["position_id"],
                "properties": {
                    "position_id": {"const": position_id},
                    "bids": DEPTH_LEVELS_SCHEMA,
                    "asks": DEPTH_LEVELS_SCHEMA,
                },
            },
        )
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    with allure.step(f"校验买盘 {len(bids)} 档、卖盘 {len(asks)} 档"):
        for level in bids + asks:
            assert 0 <= Decimal(level["price"]) <= 1, "二元市场盘口价格应在 0 到 1 之间"
            assert Decimal(level["total_qty"]) > 0, "盘口档位数量应大于零"
