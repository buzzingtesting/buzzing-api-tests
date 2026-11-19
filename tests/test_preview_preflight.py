import allure
import pytest

from api_client import request
from response_checks import expect_envelope, expect_native


pytestmark = pytest.mark.preflight


def fetch(api_client, path, operation):
    with allure.step(f"请求 GET {path}"):
        return request(api_client, "GET", path, operation=operation)


@allure.title("首页横幅返回预检所需的原生字段")
def test_home_banners(api_client):
    response = fetch(api_client, "/api/v1/home/banners", "home_banners")
    expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["version", "items"],
            "properties": {
                "version": {"type": "string"},
                "items": {"type": "array"},
            },
        },
    )


@allure.title("Rewards 公共配置返回任务列表")
def test_rewards_public_config(api_client):
    response = fetch(api_client, "/api/v1/rewards/public-config", "rewards_public_config")
    expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["tasks"],
            "properties": {"tasks": {"type": "array"}},
        },
    )


@allure.title("未登录访问用户资料返回 HTTP 401 和业务码 1005")
def test_user_info_requires_login(api_client):
    response = fetch(api_client, "/api/v1/user/info", "user_info_anonymous")
    expect_envelope(response, status=401, code=1005)


@allure.title("旧版错误码目录返回 HTTP 200 和业务码 0")
def test_error_code_catalog(api_client):
    response = fetch(api_client, "/api/v1/errorcode", "legacy_error_code_catalog")
    expect_envelope(response, status=200, code=0)
