import allure

from function.identity import get_session_user_id
from function.orders import list_orders
from function.reporting import PROJECT_NAME
from function.response_checks import expect_native


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("订单查询"),
    allure.epic(PROJECT_NAME),
    allure.feature("订单查询"),
]


@allure.title("已登录用户查询本人订单历史")
def test_order_history(authenticated_api_client, test_data):
    case = test_data["market_cases"]["order_history"]
    params = case["params"]
    with allure.step("从当前登录会话获取用户 ID"):
        user_id = get_session_user_id(authenticated_api_client)

    with allure.step("查询本人订单历史首页并核对状态和归属"):
        first_page = _check_history_page(
            authenticated_api_client, user_id, params, case["terminal_statuses"],
        )

    pagination = first_page["pagination"]
    if pagination["has_more"]:
        with allure.step("用本轮 next_cursor 查询下一页并核对订单是否重复"):
            cursor = pagination["next_cursor"]
            next_page = _check_history_page(
                authenticated_api_client, user_id, {**params, "next_cursor": cursor},
                case["terminal_statuses"],
            )
            assert next_page["items"], "首页标记有后续订单，下一页应有记录"
            assert _order_ids(first_page).isdisjoint(_order_ids(next_page)), "相邻两页返回了重复订单"


def _check_history_page(client, user_id, params, terminal_statuses):
    result = expect_native(
        list_orders(client, params=params),
        status=200,
        schema={
            "type": "object",
            "required": ["items", "source", "pagination"],
            "properties": {
                "source": {"const": params["source"]},
                "items": {
                    "type": "array",
                    "maxItems": params["page_size"],
                    "items": {
                        "type": "object",
                        "required": ["source", "order"],
                        "properties": {
                            "source": {"type": "string", "minLength": 1},
                            "order": {
                                "type": "object",
                                "required": ["uid", "order_id", "status"],
                                "properties": {
                                    "uid": {"const": int(user_id)},
                                    "order_id": {"type": "string", "minLength": 1},
                                    "status": {"enum": terminal_statuses},
                                },
                            },
                        },
                    },
                },
                "pagination": {
                    "type": "object",
                    "required": ["page_size", "has_more"],
                    "properties": {
                        "page_size": {"const": params["page_size"]},
                        "has_more": {"type": "boolean"},
                    },
                    "if": {"properties": {"has_more": {"const": True}}},
                    "then": {
                        "required": ["next_cursor"],
                        "properties": {
                            "next_cursor": {
                                "anyOf": [
                                    {"type": "integer", "minimum": 1},
                                    {"type": "string", "minLength": 1},
                                ],
                            },
                        },
                    },
                },
            },
        },
    )
    items = result["items"]
    pagination = result["pagination"]
    assert len(_order_ids(result)) == len(items), "同一页返回了重复订单"
    if pagination["has_more"]:
        assert items, "订单列表为空但分页标记仍有后续"
        assert str(pagination["next_cursor"]) != str(params.get("next_cursor", "")), "订单游标应向前推进"
    allure.attach(str(len(items)), "本页订单数", allure.attachment_type.TEXT)
    return result


def _order_ids(result):
    return {(item["source"], item["order"]["order_id"]) for item in result["items"]}
