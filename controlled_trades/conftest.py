from pathlib import Path

import pytest

from function.settings import get_environment
from function.trade_run import TradeRun, trade_journal


def pytest_addoption(parser):
    parser.addoption("--trade-run", type=Path, help="本次已批准的 Preview 交易参数 JSON")


@pytest.fixture
def trade_fixture(pytestconfig):
    path = pytestconfig.getoption("--trade-run")
    if path is None:
        pytest.skip("需要提供本轮交易参数 --trade-run")
    if get_environment() != "preview" or hasattr(pytestconfig, "workerinput"):
        pytest.fail("本场景仅支持串行 Preview 执行", pytrace=False)
    run = TradeRun.from_file(path)
    root = Path(__file__).resolve().parents[1] / "reports" / "trade-runs"
    return run, trade_journal(root, run)
