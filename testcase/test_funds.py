import allure

from function.funds import get_buying_power
from function.market import load_binary_orderbook_market
from function.reporting import PROJECT_NAME
from function.response_checks import expect_native


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("资金查询"),
    allure.epic(PROJECT_NAME),
    allure.feature("资金查询"),
]

RAW_AMOUNT_SCHEMA = {"type": "string", "pattern": "^[0-9]+$"}


@allure.title("已登录用户查询所选市场的可用购买力")
def test_buying_power(authenticated_api_client, test_data):
    params = test_data["market_cases"]["orderbook_binary"]["list_params"]
    market = load_binary_orderbook_market(authenticated_api_client, params)
    selected, opposite = market["options"]

    with allure.step("查询购买力并确认测试账号余额大于零"):
        funds = expect_native(
            get_buying_power(
                authenticated_api_client,
                condition_id=market["condition_id"],
                token_id=selected["position_id"],
            ),
            status=200,
            schema={
                "type": "object",
                "required": ["buying_power", "opposite_token_id"],
                "properties": {
                    "balance": RAW_AMOUNT_SCHEMA,
                    "buying_power": RAW_AMOUNT_SCHEMA,
                    "credit_available": RAW_AMOUNT_SCHEMA,
                    "available_from_offset": RAW_AMOUNT_SCHEMA,
                    "offset_shares": RAW_AMOUNT_SCHEMA,
                    "credit_locked": {"type": "boolean"},
                    "offset_applicable": {"type": "boolean"},
                    "opposite_token_id": {"const": opposite["position_id"]},
                },
            },
        )
        assert "balance" in funds, "购买力响应缺少 balance"
        assert int(funds["balance"]) > 0, "测试账号 balance 应大于 0"
