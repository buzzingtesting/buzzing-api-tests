from api_client import PREVIEW_URL, request
from response_checks import expect_native


def login_with_privy_identity_token(api_client, identity_token):
    """把从 Preview Privy 会话取得的凭证换成 Buzzing token。"""
    if str(api_client.base_url).rstrip("/") != PREVIEW_URL:
        raise ValueError("验证码登录用例只在 Buzzing Preview 运行")
    if not isinstance(identity_token, str) or not identity_token.strip():
        raise ValueError("缺少 Preview Privy identity token")

    response = request(
        api_client,
        "POST",
        "/api/v1/login",
        operation="privy_identity_login",
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
