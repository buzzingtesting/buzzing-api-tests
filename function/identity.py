import logging
from functools import partial

import pytest

from function.api_client import LOGIN_PATH, create_client, request
from function.auth_cache import CachedLoginUnavailable, load_buzzing_jwt, save_tokens
from function.gmail_code import get_gmail_code
from function.mailosaur_code import get_mailosaur_code
from function.privy_email import (
    create_privy_client,
    send_email_code,
    verify_email_code,
    verify_privy_user,
)
from function.response_checks import expect_native
from function.settings import BASE_URLS, WEB_ORIGINS, check_account_environment, get_environment


def request_email_code(privy_client, account):
    """按测试数据指定的邮箱服务发送并获取本轮验证码。"""
    check_account_environment(account)
    email = account["email"]
    if not email or "@" not in email:
        pytest.fail("请在 ci.yaml 填好当前环境的测试账号邮箱", pytrace=False)
    subject = account.get("mail_subject")
    if not subject or not subject.strip():
        pytest.fail("请在 ci.yaml 填好当前账号的验证码邮件标题", pytrace=False)
    provider = account.get("mail_provider", "gmail")
    send_code = partial(send_email_code, privy_client, email)
    if provider == "gmail":
        return get_gmail_code(email, subject, send_code, password_env=account.get("gmail_password_env"))
    if provider == "mailosaur":
        return get_mailosaur_code(email, subject, send_code)
    pytest.fail("mail_provider 只支持 gmail 或 mailosaur", pytrace=False)


def login_with_privy_identity_token(api_client, identity_token, *, expected_uid=None, platform=None, audit=None):
    """把同一个环境的 Privy 身份凭证换成 Buzzing token。"""
    environment = get_environment()
    if str(api_client.base_url).rstrip("/") != BASE_URLS[environment]:
        raise ValueError("登录客户端和当前环境不匹配")
    if not isinstance(identity_token, str) or not identity_token.strip():
        raise ValueError("缺少 Privy identity token")

    payload = {"privy_identity_token": identity_token}
    if expected_uid is not None:
        payload["expect_user_id"] = str(expected_uid)
    if platform is not None:
        if environment != "preview" or platform != "ios" or expected_uid is None:
            raise ValueError("App 登录只允许指定 UID 的 Preview 场景")
        payload["platform"] = platform

    response = request(
        api_client,
        "POST",
        LOGIN_PATH,
        operation="privy_identity_login",
        headers={"Origin": WEB_ORIGINS[environment]},
        json=payload,
        follow_redirects=False,
    )
    if audit is not None:
        audit.append({"method": "POST", "path": LOGIN_PATH, "http_status": response.status_code,
                      "request_id": response.headers.get("x-request-id"), "cf_ray": response.headers.get("cf-ray")})
    return expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["token", "expire", "user_id"],
            "properties": {
                "token": {"type": "string", "minLength": 1},
                "expire": {"type": "string", "minLength": 1},
                "user_id": {"type": "integer"} if expected_uid is None else {"const": expected_uid},
            },
        },
    )


def get_session(api_client):
    return request(api_client, "GET", "/api/v1/auth/session", operation="login_session")


def get_session_user_id(api_client):
    session = expect_native(
        get_session(api_client),
        status=200,
        schema={
            "type": "object",
            "required": ["user_id"],
            "properties": {"user_id": {"type": "string", "pattern": "^[1-9][0-9]*$"}},
        },
    )
    return session["user_id"]


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


def get_privy_email_identity_token(account):
    with create_privy_client() as privy:
        code = request_email_code(privy, account)
        privy_session = verify_email_code(privy, account["email"], code)
        verify_privy_user(privy, privy_session)
    return privy_session["identity_token"]


def get_test_jwt(case_name, account):
    """复用有效凭证，缺失或快到期时自动通过邮箱登录签发新凭证。"""
    check_account_environment(account)
    email = account["email"]
    try:
        return load_buzzing_jwt(case_name, email, min_validity_seconds=60)
    except CachedLoginUnavailable as error:
        logging.getLogger("buzzing.api").info("%s，自动重新登录", error)

    identity_token = get_privy_email_identity_token(account)
    with create_client() as client:
        login = login_with_privy_identity_token(client, identity_token)
    verify_login_session(login, account)
    save_tokens(case_name, email, login, identity_token)
    return login["token"]
