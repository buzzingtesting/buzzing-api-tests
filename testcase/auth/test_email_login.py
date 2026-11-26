import allure
import pytest

from function.auth_cache import save_tokens
from function.identity import login_with_privy_identity_token, request_email_code, verify_login_session
from function.privy_email import (
    create_privy_client,
    submit_email_code,
    verify_email_code,
    verify_privy_user,
)
from function.reporting import PROJECT_NAME
from function.response_checks import expect_native


MODULE_NAME = "身份与登录"
pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(MODULE_NAME),
    allure.epic(PROJECT_NAME),
    allure.feature(MODULE_NAME),
]


@allure.title("Google邮箱验证码登录")
@pytest.mark.pro
def test_email_login(api_client, test_account):
    case_name, account = test_account
    email = account["email"]

    with create_privy_client() as privy:
        with allure.step("发码并自动读取本轮邮件"):
            code = request_email_code(privy, account)

        with allure.step("用 Privy 校验邮箱验证码"):
            privy_session = verify_email_code(privy, email, code)

        with allure.step("查询 Privy 当前用户并核对用户 ID"):
            verify_privy_user(privy, privy_session)

    identity_token = privy_session["identity_token"]
    with allure.step("用 Privy 身份凭证登录 Buzzing"):
        login = login_with_privy_identity_token(api_client, identity_token)

    with allure.step("确认会话已登录且用户 ID 一致"):
        verify_login_session(login, account)

    with allure.step("保存当前环境的登录凭证"):
        save_tokens(case_name, email, login, identity_token)


@allure.title("邮箱验证码错误时拒绝登录")
def test_invalid_email_code(test_account):
    _, account = test_account
    with create_privy_client() as privy:
        with allure.step("发码并自动读取本轮邮件"):
            code = request_email_code(privy, account)

        with allure.step("修改验证码末位并提交一次"):
            wrong_code = code[:-1] + ("1" if code[-1] == "0" else "0")
            response = submit_email_code(privy, account["email"], wrong_code)

    with allure.step("校验错误码与凭证字段"):
        expect_native(
            response,
            status=422,
            schema={
                "type": "object",
                "required": ["code", "error"],
                "properties": {
                    "code": {"const": "invalid_credentials"},
                    "error": {"type": "string", "minLength": 1},
                    "identity_token": False,
                    "token": False,
                    "access_token": False,
                    "privy_access_token": False,
                    "refresh_token": False,
                },
            },
        )
