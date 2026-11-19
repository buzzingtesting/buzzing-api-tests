import os
from uuid import uuid4

import httpx

from function.api_client import request
from function.response_checks import expect_native
from function.settings import WEB_ORIGINS, get_environment


def create_privy_client():
    """使用当前环境的 Privy 应用发送和验证验证码。"""
    environment = get_environment()
    app_id = "cmhlogpua008ll20cgyvimcsj"
    client_id = "client-WY6SV4h82eW49szjZVqkK4NnyfkDHL1gXViXuCxF6jiyE"
    if environment == "prod":
        app_id = os.getenv("BUZZING_PROD_PRIVY_APP_ID", "").strip()
        client_id = os.getenv("BUZZING_PROD_PRIVY_CLIENT_ID", "").strip()
        if not app_id:
            raise ValueError("请配置 BUZZING_PROD_PRIVY_APP_ID 后再运行生产登录")
    headers = {
        "origin": WEB_ORIGINS[environment],
        "privy-app-id": app_id,
        "privy-ca-id": str(uuid4()),
    }
    if client_id:
        headers["privy-client-id"] = client_id
    return httpx.Client(
        base_url="https://auth.privy.io",
        timeout=10.0,
        headers=headers,
    )


def send_email_code(client, email):
    response = request(
        client,
        "POST",
        "/api/v1/passwordless/init",
        operation="privy_send_email_code",
        json={"email": email},
    )
    expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["success"],
            "properties": {"success": {"const": True}},
        },
    )


def verify_email_code(client, email, code):
    response = request(
        client,
        "POST",
        "/api/v1/passwordless/authenticate",
        operation="privy_verify_email_code",
        json={"email": email, "code": code, "mode": "no-signup"},
    )
    session = expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["identity_token"],
            "properties": {"identity_token": {"type": "string", "minLength": 1}},
        },
    )
    return session["identity_token"]
