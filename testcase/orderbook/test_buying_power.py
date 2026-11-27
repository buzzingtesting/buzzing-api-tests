import allure

from function.api_client import request
from function.funds import get_buying_power
from function.identity import get_session_user_id
from function.market import load_binary_orderbook_market
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_native
from function.trade_contract import funding_snapshot


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("Orderbook 盘口")),
    allure.sub_suite("购买力"),
    allure.epic(PROJECT_NAME),
    allure.feature("Orderbook 盘口"),
]

RAW_AMOUNT_SCHEMA = {"type": "string", "pattern": "^[0-9]+$"}


@allure.title("资金账户 · 核对登录用户、钱包余额与 Cash/Credit 状态")
def test_account_funding(authenticated_api_client):
    client = authenticated_api_client
    with allure.step("核对邮箱会话和交易账户归属"):
        uid = get_session_user_id(client)
        expect_native(
            request(client, "GET", "/api/v1/user/info", operation="funding_user_info"),
            status=200,
            schema={
                "type": "object",
                "required": ["user_id", "polymarket"],
                "properties": {
                    "user_id": {"enum": [int(uid), uid]},
                    "polymarket": {
                        "type": "object",
                        "required": ["usdb_balance", "usdb_frozen_amount"],
                        "properties": {
                            "usdb_balance": RAW_AMOUNT_SCHEMA,
                            "usdb_frozen_amount": RAW_AMOUNT_SCHEMA,
                        },
                    },
                },
            },
        )

    with allure.step("查询账户资金并校验 Cash 与 Credit 的金额和锁定状态"):
        funding_snapshot(client)


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
