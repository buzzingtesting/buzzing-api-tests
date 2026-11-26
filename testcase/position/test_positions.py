import allure

from function.identity import get_session_user_id
from function.positions import (
    expect_closed_positions_page,
    expect_positions_page,
    list_closed_positions,
    list_positions,
)
from function.reporting import PROJECT_NAME, ordered_suite_name


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("Position 持仓")),
    allure.epic(PROJECT_NAME),
    allure.feature("Position 持仓"),
]


@allure.title("已登录用户查询本人未平持仓")
def test_open_positions(authenticated_api_client, test_data):
    params = test_data["market_cases"]["open_positions"]["params"]
    with allure.step("从当前登录会话获取用户 ID"):
        user_id = get_session_user_id(authenticated_api_client)

    with allure.step("查询本人未平持仓并校验分页和持仓归属"):
        first_page = _check_positions_page(authenticated_api_client, user_id, params)

    with allure.step("查询末页之后的分页并检查空结果"):
        _check_positions_page(
            authenticated_api_client, user_id, _params_after_last_page(params, first_page),
        )


@allure.title("已登录用户查询本人已关闭持仓")
def test_closed_positions(authenticated_api_client, test_data):
    params = test_data["market_cases"]["closed_positions"]["params"]
    with allure.step("从当前登录会话获取用户 ID"):
        user_id = get_session_user_id(authenticated_api_client)
    params = {**params, "uid": user_id}

    with allure.step("查询本人已关闭持仓并校验当前页和关闭记录"):
        first_page = expect_closed_positions_page(
            list_closed_positions(authenticated_api_client, params=params), params,
        )

    with allure.step("查询已关闭持仓末页之后的空结果"):
        empty_page_params = _params_after_last_page(params, first_page)
        expect_closed_positions_page(
            list_closed_positions(authenticated_api_client, params=empty_page_params),
            empty_page_params,
        )


def _params_after_last_page(params, first_page):
    total = int(first_page["pagination"]["total"])
    page_size = params["page_size"]
    last_page = (total + page_size - 1) // page_size
    return {**params, "page": max(params["page"], last_page) + 1}


def _check_positions_page(client, user_id, params):
    response = list_positions(client, params={**params, "uid": user_id})
    result = expect_positions_page(response, params)
    pretrading = result.get("pretrading")
    if pretrading is not None:
        assert pretrading["uid"] == int(user_id), "预交易持仓应属于当前账号"
    for item in result["items"]:
        if item.get("cycle_summary") is not None:
            assert item["cycle_summary"]["user_id"] == user_id, "持仓周期记录应属于当前账号"
    return result
