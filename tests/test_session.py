import allure
import pytest

from api_client import request
from response_checks import expect_native


@pytest.mark.preflight
@allure.title("匿名会话的认证级别为 S0")
def test_anonymous_session(api_client):
    path = "/api/v1/auth/session"
    with allure.step(f"请求 GET {path}"):
        response = request(api_client, "GET", path, operation="anonymous_session")

    with allure.step("检查 HTTP 状态和匿名认证级别"):
        expect_native(
            response,
            status=200,
            schema={
                "type": "object",
                "required": ["auth_level"],
                "properties": {"auth_level": {"type": "integer", "const": 0}},
            },
        )
