import pytest

from api_client import create_client


@pytest.fixture
def api_client():
    """为每条用例提供独立客户端，每次用完自动关闭。"""
    with create_client() as client:
        yield client
