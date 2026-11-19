import logging
from functools import partial

import pytest

from function.api_client import LOGIN_PATH, create_client, request
from function.auth_cache import CachedLoginUnavailable, load_buzzing_jwt, save_tokens
from function.gmail_code import get_gmail_code
from function.mailosaur_code import get_mailosaur_code
from function.privy_email import create_privy_client, send_email_code, verify_email_code
from function.response_checks import expect_native
from function.settings import BASE_URLS, WEB_ORIGINS, check_account_environment, get_environment


def request_email_code(privy_client, account):
    """按测试数据指定的邮箱服务发送并获取本轮验证码。"""
    check_account_environment(account)
    email = account["email"]
    if not email or "@" not in email:
        pytest.fail("请在 email_login.yaml 填好当前环境的测试账号邮箱", pytrace=False)
    subject = account.get("mail_subject")
    if not subject or not subject.strip():
        pytest.fail("请在 email_login.yaml 填好当前账号的验证码邮件标题", pytrace=False)
    readers = {"gmail": get_gmail_code, "mailosaur": get_mailosaur_code}
    read_code = readers.get(account.get("mail_provider", "gmail"))
    if read_code is None:
        pytest.fail("mail_provider 只支持 gmail 或 mailosaur", pytrace=False)

    send_code = partial(send_email_code, privy_client, email)
    return read_code(email, subject, send_code)


def login_with_privy_identity_token(api_client, identity_token):
    """把同一个环境的 Privy 身份凭证换成 Buzzing token。"""
    environment = get_environment()
    if str(api_client.base_url).rstrip("/") != BASE_URLS[environment]:
        raise ValueError("登录客户端和当前环境不匹配")
    if not isinstance(identity_token, str) or not identity_token.strip():
        raise ValueError("缺少 Privy identity token")

    response = request(
        api_client,
        "POST",
        LOGIN_PATH,
        operation="privy_identity_login",
        headers={"Origin": WEB_ORIGINS[environment]},
        json={"privy_identity_token": identity_token},
        follow_redirects=False,
    )
    return expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["token", "expire", "user_id"],
            "properties": {
                "token": {"type": "string", "minLength": 1},
                "expire": {"type": "string", "minLength": 1},
                "user_id": {"type": "integer"},
            },
        },
    )


def get_session(api_client):
    return request(api_client, "GET", "/api/v1/auth/session", operation="login_session")


def verify_login_session(login, account):
    with create_client(bearer_token=login["token"]) as client:
        response = get_session(client)
    expect_native(
        response,
        status=account["expected_session_status"],
        schema={
            "type": "object",
            "required": ["auth_level", "user_id"],
            "properties": {
                "auth_level": {"type": "integer", "enum": account["expected_auth_levels"]},
                "user_id": {"const": str(login["user_id"])},
            },
        },
    )


def get_test_jwt(case_name, account):
    """复用有效凭证，缺失或快到期时自动通过邮箱登录签发新凭证。"""
    check_account_environment(account)
    email = account["email"]
    try:
        return load_buzzing_jwt(case_name, email, min_validity_seconds=60)
    except CachedLoginUnavailable as error:
        logging.getLogger("buzzing.api").info("%s，自动重新登录", error)

    with create_privy_client() as privy:
        code = request_email_code(privy, account)
        identity_token = verify_email_code(privy, email, code)
    with create_client() as client:
        login = login_with_privy_identity_token(client, identity_token)
    verify_login_session(login, account)
    save_tokens(case_name, email, login, identity_token)
    return login["token"]
