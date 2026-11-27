import os
from uuid import uuid4

import httpx

from function.api_client import request
from function.response_checks import expect_native
from function.settings import WEB_ORIGINS, get_environment


def privy_app_config():
    environment = get_environment()
    app_id = "cmhlogpua008ll20cgyvimcsj"
    client_id = "client-WY6SV4h82eW49szjZVqkK4NnyfkDHL1gXViXuCxF6jiyE"
    if environment == "prod":
        app_id = os.getenv("BUZZING_PROD_PRIVY_APP_ID", "").strip()
        client_id = os.getenv("BUZZING_PROD_PRIVY_CLIENT_ID", "").strip()
        if not app_id:
            raise ValueError("请配置 BUZZING_PROD_PRIVY_APP_ID 后再运行生产登录")
    return {"appId": app_id, "clientId": client_id}


def create_privy_client():
    """使用当前环境的 Privy 应用发送和验证验证码。"""
    config = privy_app_config()
    headers = {
        "origin": WEB_ORIGINS[get_environment()],
        "privy-app-id": config["appId"],
        "privy-ca-id": str(uuid4()),
    }
    if config["clientId"]:
        headers["privy-client-id"] = config["clientId"]
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


def submit_email_code(client, email, code):
    return request(
        client,
        "POST",
        "/api/v1/passwordless/authenticate",
        operation="privy_verify_email_code",
        json={"email": email, "code": code, "mode": "no-signup"},
    )


def verify_email_code(client, email, code):
    response = submit_email_code(client, email, code)
    return expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["token", "identity_token", "user"],
            "properties": {
                "token": {"type": "string", "minLength": 1},
                "identity_token": {"type": "string", "minLength": 1},
                "user": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string", "minLength": 1}},
                },
            },
        },
    )


def verify_privy_user(client, session):
    """使用本轮 Access Token 查询 Privy 用户并核对用户 ID。"""
    response = request(
        client,
        "GET",
        "/api/v1/users/me",
        operation="privy_current_user",
        headers={"Authorization": f"Bearer {session['token']}"},
    )
    expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["user"],
            "properties": {
                "user": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"const": session["user"]["id"]}},
                },
            },
        },
    )
