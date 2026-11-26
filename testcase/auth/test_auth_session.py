import allure

from function.identity import get_session
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_native


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("身份与登录")),
    allure.epic(PROJECT_NAME),
    allure.feature("身份与登录"),
]


@allure.title("匿名会话返回 S0")
def test_anonymous_session(api_client):
    expect_native(
        get_session(api_client),
        status=200,
        schema={
            "type": "object",
            "required": ["auth_level"],
            "properties": {"auth_level": {"type": "integer", "const": 0}},
        },
    )


@allure.title("已登录用户可查询会话")
def test_authenticated_session(authenticated_api_client, test_account):
    _, account = test_account
    expect_native(
        get_session(authenticated_api_client),
        status=account["expected_session_status"],
        schema={
            "type": "object",
            "required": ["auth_level", "reauth_required"],
            "properties": {
                "auth_level": {"type": "integer", "enum": account["expected_auth_levels"]},
                "reauth_required": {"type": "boolean"},
            },
        },
    )
