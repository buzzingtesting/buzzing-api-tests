import allure
import pytest

from function.auth_cache import save_tokens
from function.identity import login_with_privy_identity_token, request_email_code, verify_login_session
from function.privy_email import create_privy_client, verify_email_code
from function.reporting import PROJECT_NAME


MODULE_NAME = "身份与登录"


@allure.parent_suite(PROJECT_NAME)
@allure.suite(MODULE_NAME)
@allure.epic(PROJECT_NAME)
@allure.feature(MODULE_NAME)
@allure.title("Google邮箱验证码登录")
@pytest.mark.pro
def test_email_login(api_client, test_account):
    case_name, account = test_account
    email = account["email"]

    with create_privy_client() as privy:
        with allure.step("发码并自动读取本轮邮件"):
            code = request_email_code(privy, account)

        with allure.step("用 Privy 校验邮箱验证码"):
            identity_token = verify_email_code(privy, email, code)

    with allure.step("用 Privy 身份凭证登录 Buzzing"):
        login = login_with_privy_identity_token(api_client, identity_token)

    with allure.step("确认会话已登录且用户 ID 一致"):
        verify_login_session(login, account)

    with allure.step("保存当前环境的登录凭证"):
        save_tokens(case_name, email, login, identity_token)
