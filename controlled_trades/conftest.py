from pathlib import Path

import pytest

from function.settings import get_environment
from function.trade_run import TradeRun, trade_journal


def pytest_addoption(parser):
    parser.addoption("--trade-run", action="append", type=Path, default=[],
                     help="本次已批准的 Preview 交易参数 JSON，可重复传入")


def pytest_configure(config):
    config.addinivalue_line("markers", "m03_case(scenario): M03 受控写入场景")


def pytest_generate_tests(metafunc):
    marker = metafunc.definition.get_closest_marker("m03_case")
    if marker is None:
        return
    try:
        runs = [TradeRun.from_file(path, check_window=False) for path in metafunc.config.getoption("--trade-run")]
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise pytest.UsageError(f"M03 运行参数无效（{error}）") from None
    if len({run.run_id for run in runs}) != len(runs):
        raise pytest.UsageError("每个 M03 测试运行须指定独立的 run_id")
    if len({(run.scenario, run.account) for run in runs}) != len(runs):
        raise pytest.UsageError("同一 M03 场景的各轮必须使用独立的专用账号及钱包")
    if len({run.batch_id for run in runs if run.batch_id}) != sum(bool(run.batch_id) for run in runs):
        raise pytest.UsageError("同一批 Credit 不可作为两轮独立测试数据")
    selected = [pytest.param(run, id=run.account) for run in runs if run.scenario == marker.args[0]]
    metafunc.parametrize("trade_fixture", selected or [None], indirect=True)


@pytest.fixture
def trade_fixture(pytestconfig, request):
    run = request.param
    if run is None:
        pytest.skip("需要提供该场景的 --trade-run")
    if get_environment() != "preview" or hasattr(pytestconfig, "workerinput"):
        pytest.fail("本场景仅支持串行 Preview 执行", pytrace=False)
    run.check_window()
    root = Path(__file__).resolve().parents[1] / "reports" / "trade-runs"
    return run, trade_journal(root, run)
