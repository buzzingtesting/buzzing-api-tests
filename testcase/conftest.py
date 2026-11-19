from pathlib import Path

import pytest
import yaml

from function.api_client import create_client
from function.identity import get_test_jwt
from function.settings import check_account_environment, get_testdata


DATA_FILE = Path(__file__).resolve().parents[1] / "email_login.yaml"


@pytest.fixture
def test_account():
    """从 YAML 读取本轮选中的测试账号。"""
    case_name = get_testdata()
    data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or case_name not in data:
        pytest.fail("email_login.yaml 中没有选中的测试数据", pytrace=False)
    account = data[case_name]
    check_account_environment(account)
    return case_name, account


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
