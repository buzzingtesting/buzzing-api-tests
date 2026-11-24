import allure
import pytest

from function.api_client import create_client
from function.identity import get_test_jwt
from function.settings import check_account_environment, get_testdata, load_test_data


@pytest.fixture
def test_data():
    return load_test_data()


@pytest.fixture
def test_account(request, test_data):
    """优先使用场景指定的账号，否则使用当前环境的默认账号。"""
    account_name = get_testdata(getattr(request, "param", None))
    account = test_data.get(account_name) if isinstance(test_data, dict) else None
    if not isinstance(account, dict) or account_name == "market_cases":
        pytest.fail(f"ci.yaml 中没有账号 {account_name}", pytrace=False)
    check_account_environment(account)
    allure.dynamic.parameter("测试账号", account_name)
    return account_name, account


@pytest.fixture
def api_client():
    """为每条用例提供独立客户端，每次用完自动关闭。"""
    with create_client() as client:
        yield client


@pytest.fixture
def authenticated_api_client(test_account):
    """为其他用例注入当前环境的 Buzzing JWT，必要时自动重新登录。"""
    case_name, account = test_account
    token = get_test_jwt(case_name, account)
    with create_client(bearer_token=token) as client:
        yield client
