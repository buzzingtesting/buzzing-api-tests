import json
import os
from pathlib import Path

import allure
import pytest

from function.api_client import create_client, preview_submit_alert_handler
from function.identity import get_test_jwt
from function.settings import check_account_environment, get_environment, get_testdata, load_test_data


TRADE_RUNS_KEY = pytest.StashKey()
PREVIEW_SUBMIT_ALERT_KEY = pytest.StashKey()
PREVIEW_SUBMIT_ALERT_TITLE = "测试环境测试金额不足（疑似）"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    def report_alert(details):
        session = item.session
        if PREVIEW_SUBMIT_ALERT_KEY in session.stash:
            return
        message = f"{PREVIEW_SUBMIT_ALERT_TITLE}，{details['alert_reason']}"
        session.stash[PREVIEW_SUBMIT_ALERT_KEY] = message
        session.shouldstop = message
        allure.dynamic.tag(PREVIEW_SUBMIT_ALERT_TITLE)
        allure.attach(json.dumps(details, ensure_ascii=False, indent=2), PREVIEW_SUBMIT_ALERT_TITLE,
                      allure.attachment_type.JSON)

    token = preview_submit_alert_handler.set(report_alert)
    try:
        return (yield)
    finally:
        preview_submit_alert_handler.reset(token)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == pytest.ExitCode.TESTS_FAILED and PREVIEW_SUBMIT_ALERT_KEY in session.stash:
        session.exitstatus = pytest.ExitCode.INTERRUPTED


def pytest_addoption(parser):
    parser.addoption("--dispute-market-id", type=int, help="手动回归使用的 Preview 自营市场 ID")
    parser.addoption("--disputed-market-id", type=int, help="手动回归使用的 Preview 争议中市场 ID")
    parser.addoption("--trade-run", action="append", type=Path, default=[],
                     help="本次已批准的 Preview 交易参数 JSON，可重复传入")


def configured_trade_runs(config):
    if TRADE_RUNS_KEY in config.stash:
        return config.stash[TRADE_RUNS_KEY]
    from function.trade_run import TradeRun

    try:
        runs = tuple(TradeRun.from_file(path, check_window=False) for path in config.getoption("--trade-run"))
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise pytest.UsageError(f"M03 运行参数无效（{error}）") from None
    if len({run.run_id for run in runs}) != len(runs):
        raise pytest.UsageError("每个 M03 测试运行须指定独立的 run_id")
    if len({(run.scenario, run.account) for run in runs}) != len(runs):
        raise pytest.UsageError("同一 M03 场景的各轮必须使用独立的专用账号及钱包")
    if len({run.batch_id for run in runs if run.batch_id}) != sum(bool(run.batch_id) for run in runs):
        raise pytest.UsageError("同一批 Credit 不可作为两轮独立测试数据")
    config.stash[TRADE_RUNS_KEY] = runs
    return runs


def pytest_generate_tests(metafunc):
    dispute = metafunc.definition.get_closest_marker("manual_dispute")
    if dispute is not None and dispute.args == ("no_position",):
        account = os.getenv("BUZZING_DISPUTE_NO_POSITION_ACCOUNT", "").strip()
        marks = []
        if not account and metafunc.config.getoption("--dispute-market-id") is not None:
            marks.append(pytest.mark.skip(reason="请为本轮争议回归指定无持仓测试账号"))
        metafunc.parametrize("test_account", [pytest.param(account, id="无持仓账号", marks=marks)], indirect=True)
        return

    marker = metafunc.definition.get_closest_marker("m03_case")
    if marker is None:
        return
    runs = configured_trade_runs(metafunc.config)
    selected = [pytest.param(run, id=run.account) for run in runs if run.scenario == marker.args[0]]
    metafunc.parametrize("run_trade", selected or [None], indirect=True)


def pytest_collection_modifyitems(config, items):
    environment = get_environment()
    dispute_market_id = config.getoption("--dispute-market-id")
    disputed_market_id = config.getoption("--disputed-market-id")
    for option, market_id in (("--dispute-market-id", dispute_market_id), ("--disputed-market-id", disputed_market_id)):
        if market_id is not None and (environment != "preview" or market_id <= 0):
            raise pytest.UsageError(f"{option} 仅接受 Preview 的正整数市场 ID")

    ordinary, trades, fixture_trades, deselected = [], [], [], []
    scenario_order = {run.run_id: index for index, run in enumerate(configured_trade_runs(config))}
    for item in items:
        dispute = item.get_closest_marker("manual_dispute")
        if dispute is not None:
            if dispute.args == ("already_open",):
                skip_dispute = disputed_market_id is None and (environment != "preview" or dispute_market_id is not None)
            else:
                skip_dispute = dispute_market_id is None
            if skip_dispute:
                item.add_marker(pytest.mark.skip(reason="自营市场争议仅在发版回归准备好数据后手动执行"))
        if item.get_closest_marker("b3_fixture"):
            fixture_trades.append(item)
        elif not item.get_closest_marker("m03_case"):
            ordinary.append(item)
        elif item.callspec.params["run_trade"] is None:
            deselected.append(item)
        else:
            trades.append(item)
    trades.sort(key=lambda item: scenario_order[item.callspec.params["run_trade"].run_id])
    items[:] = ordinary + trades + fixture_trades
    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.fixture
def dispute_market_id(pytestconfig):
    return pytestconfig.getoption("--dispute-market-id")


@pytest.fixture
def disputed_market_id(pytestconfig):
    return pytestconfig.getoption("--disputed-market-id")


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


@pytest.fixture
def run_trade(pytestconfig, request, trade_buy_results):
    run = request.param
    if run is None:
        pytest.skip("需要提供该场景的 --trade-run")
    if get_environment() != "preview" or hasattr(pytestconfig, "workerinput"):
        pytest.fail("本场景仅支持串行 Preview 执行", pytrace=False)
    run.check_window()

    from function.polymarket_trade import OrderConfirmationTimeout
    from function.trade_preview import execute_trade
    from function.trade_run import POLYMARKET_BTC_BUY, POLYMARKET_NBA_LIMIT_BUY, trade_journal

    def execute(*, sell_btc=False, replay_nba=False):
        allure.dynamic.parameter("交易运行 ID", run.run_id)
        buy = trade_buy_results.get(run.run_id)
        if sell_btc and buy is None:
            pytest.skip("本轮买单未确认成交，跳过两笔卖单")
        if replay_nba and buy is None:
            pytest.skip("本轮 NBA 限价买单未确认成交，跳过幂等重放")
        root = Path(__file__).resolve().parents[1] / "reports" / "trade-runs"
        evidence_stages = {
            "cash_baseline", "depth_checked", "limit_ticket_expected", "buying_power_checked", "prepared",
            "partial_quote_selected", "partial_quote_ineligible", "partial_quote_rechecked", "partial_quote_moved",
            "partial_fill_observed", "cash_frozen", "cancel_attempt", "reconciled", "limit_views_checked",
            "market_history_checked", "market_selected", "btc_fill_confirmed", "btc_confirmation_timeout",
            "btc_half_sell_confirmed", "btc_remaining_sell_confirmed", "btc_sell_position_checked", "market_fallback",
            "nba_limit_quote_skipped", "nba_limit_depth_checked", "nba_limit_buy_confirmed",
            "nba_limit_open", "nba_limit_cash_reserved", "nba_limit_cancelled", "nba_limit_cash_released",
            "nba_limit_replay_attempt", "nba_limit_replay_response", "nba_limit_replay_reconciled", "nba_limit_replay_readback",
        }
        evidence_fields = {
            "stage", "eligible_quantity_raw", "best_ask", "best_bid", "balance_raw", "buying_power_raw",
            "expected_buy_limit", "expected_sell_limit", "plan_id", "logical_order_id",
            "order_id", "ordered_raw", "quantity_raw", "remaining_raw", "quote_raw", "fee_raw",
            "cash_raw", "cash_frozen_raw", "position_raw", "available_shares_raw", "position_frozen_raw",
            "status", "onchain_status", "history_status", "elapsed_seconds", "filled_quantity", "filled_quote",
            "remaining_quantity", "source", "side", "order_type", "funding_source", "budget",
            "event_id", "market_id", "condition_id", "position_id", "title", "cadence", "outcome", "eligible_quote",
            "buy_logical_order_id", "bought_shares", "requested_shares", "sold_shares", "remaining_bought_shares",
            "position_before_buy", "position_before_sell", "position_after_sell", "position_before_buy_raw",
            "asset", "fallback_asset", "phase", "reason", "end_time", "timeout_seconds",
            "market_title", "limit_price", "minimum_shares", "minimum_budget", "depth_shares", "neg_risk",
            "observed_limit_price", "observed_quantity_raw", "post_only", "requested_post_only", "last_trade_price", "eligible_quote_raw", "budget_raw", "max_budget_raw",
            "cash_before", "cash_open", "cash_after", "reserved_cash",
            "before", "after", "new_order_ids", "replayed", "replay_logical_order_id", "replay_order_ids",
        }
        attachment_titles = {
            "market_selected": "本轮选中市场",
            "partial_quote_selected": "自营卖一价、成交参考价与报价金额",
            "partial_quote_ineligible": "自营卖盘超出单笔上限",
            "partial_quote_rechecked": "提交前复核卖一与下单份额",
            "partial_quote_moved": "提交前卖一盘口变动",
            "partial_fill_observed": "撤单前的部分成交和未成交份额",
            "reconciled": "订单终态、成交金额与资金持仓对账", "btc_fill_confirmed": "BTC 成交详情",
            "btc_half_sell_confirmed": "BTC 第一次卖出（50%）", "btc_remaining_sell_confirmed": "BTC 第二次卖出（剩余）",
            "btc_sell_position_checked": "BTC 卖出后持仓核对",
            "btc_confirmation_timeout": "订单确认超时",
            "market_fallback": "BTC 市场关闭，切换 ETH",
            "nba_limit_quote_skipped": "NBA 限价报价排除",
            "nba_limit_depth_checked": "NBA 限价提交前盘口核对",
            "nba_limit_buy_confirmed": "NBA 限价买入成交详情",
            "nba_limit_open": "NBA 限价挂单详情",
            "nba_limit_cash_reserved": "NBA 挂单占用资金",
            "nba_limit_cancelled": "NBA 撤单终态",
            "nba_limit_cash_released": "NBA 撤单资金释放",
            "nba_limit_replay_attempt": "同计划重放前快照",
            "nba_limit_replay_response": "同计划重放回执",
            "nba_limit_replay_reconciled": "重放订单与资金对账",
            "nba_limit_replay_readback": "重放后回查原单",
        }
        with trade_journal(root, run, btc_sell=sell_btc, nba_replay=replay_nba) as record:
            def record_evidence(**fields):
                record(**fields)
                if fields.get("stage") in evidence_stages:
                    evidence = {key: value for key, value in fields.items() if key in evidence_fields}
                    stage = fields["stage"]
                    title = attachment_titles.get(stage, stage)
                    if fields.get("asset") == "ETH" and title.startswith("BTC "):
                        title = "ETH " + title.removeprefix("BTC ")
                    allure.attach(json.dumps(evidence, ensure_ascii=False, indent=2), title,
                                  allure.attachment_type.JSON)

            try:
                if replay_nba:
                    return execute_trade(run, record_evidence, nba_buy=buy)
                result = execute_trade(run, record_evidence, btc_buy=buy if sell_btc else None)
            except OrderConfirmationTimeout as error:
                pytest.skip(str(error))
            if run.scenario in {POLYMARKET_BTC_BUY, POLYMARKET_NBA_LIMIT_BUY} and not sell_btc:
                trade_buy_results[run.run_id] = result
            return result

    return execute


@pytest.fixture(scope="module")
def trade_buy_results():
    return {}
