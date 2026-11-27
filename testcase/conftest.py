import json
from pathlib import Path
from uuid import UUID

import allure
import pytest

from function.api_client import create_client, preview_submit_alert_handler
from function.identity import get_test_jwt
from function.settings import full_preview_required, get_environment, get_test_account, load_test_data


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
    parser.addoption("--dispute-no-position-market-id", type=int, help="CI 账号无持仓的 Preview 争议回归市场 ID")
    parser.addoption("--disputed-market-id", type=int, help="手动回归使用的 Preview 争议中市场 ID")
    parser.addoption("--trade-run", action="append", type=Path, default=[],
                     help="本次已批准的 Preview 交易参数 JSON，可重复传入")
    rewards = parser.getgroup("M06 Rewards")
    rewards.addoption("--m06-operation", choices=("snapshot", "inspect", "inspect-admin", "simulate", "prepare",
                                                  "grant", "reset", "flow", "replay"), help="单独运行指定的 Preview M06 操作")
    rewards.addoption("--m06-case", choices=("daily", "friends"), help="日期工具的预设任务")
    rewards.addoption("--m06-case-file", type=Path, help="真实业务场景的用例 JSON")
    rewards.addoption("--m06-run-id", type=UUID, help="模拟场景或日期工具的运行 ID")
    rewards.addoption("--m06-resume", action="store_true", help="从指定的日期工具证据目录恢复原运行")
    rewards.addoption("--m06-scene-version", type=int, help="指定要核对或接管的模拟场景版本")
    rewards.addoption("--m06-journal", type=Path, help="M06 运行证据目录")
    rewards.addoption("--m06-report", type=Path, help="用户端只读检查的 JSON 报告路径")
    rewards.addoption("--m06-wait-seconds", type=int, default=90, help="等待发奖对账的秒数")
    rewards.addoption("--m06-replay-event", type=Path, help="服务端原事件重投的证据 JSON")


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
    if len({run.batch_id for run in runs if run.batch_id}) != sum(bool(run.batch_id) for run in runs):
        raise pytest.UsageError("同一批 Credit 不可作为两轮独立测试数据")
    config.stash[TRADE_RUNS_KEY] = runs
    return runs


def pytest_generate_tests(metafunc):
    marker = metafunc.definition.get_closest_marker("m03_case")
    if marker is None:
        return
    runs = configured_trade_runs(metafunc.config)
    selected = [pytest.param(run, id=run.account) for run in runs if run.scenario == marker.args[0]]
    metafunc.parametrize("run_trade", selected or [None], indirect=True)


def pytest_collection_modifyitems(config, items):
    environment = get_environment()
    m06_operation = config.getoption("--m06-operation")
    if m06_operation is not None and environment != "preview":
        raise pytest.UsageError("--m06-operation 仅支持 Preview")
    dispute_market_id = config.getoption("--dispute-market-id")
    no_position_market_id = config.getoption("--dispute-no-position-market-id")
    disputed_market_id = config.getoption("--disputed-market-id")
    for option, market_id in (
        ("--dispute-market-id", dispute_market_id),
        ("--dispute-no-position-market-id", no_position_market_id),
        ("--disputed-market-id", disputed_market_id),
    ):
        if market_id is not None and (environment != "preview" or market_id <= 0):
            raise pytest.UsageError(f"{option} 仅接受 Preview 的正整数市场 ID")

    ordinary, trades, fixture_trades, deselected = [], [], [], []
    require_full = full_preview_required()
    scenario_order = {run.run_id: index for index, run in enumerate(configured_trade_runs(config))}
    for item in items:
        rewards = item.get_closest_marker("m06_operation")
        if m06_operation is not None or rewards is not None:
            if rewards is not None and rewards.args == (m06_operation,):
                ordinary.append(item)
            else:
                deselected.append(item)
            continue
        dispute = item.get_closest_marker("manual_dispute")
        if dispute is not None:
            if dispute.args == ("already_open",):
                skip_dispute = disputed_market_id is None and (
                    environment != "preview" or dispute_market_id is not None or no_position_market_id is not None
                )
            elif dispute.args == ("no_position",):
                skip_dispute = no_position_market_id is None
            else:
                skip_dispute = dispute_market_id is None
            if skip_dispute:
                item.add_marker(pytest.mark.skip(reason="自营市场争议仅在发版回归准备好数据后手动执行"))
        if item.get_closest_marker("b3_fixture"):
            fixture_trades.append(item)
        elif not item.get_closest_marker("m03_case"):
            ordinary.append(item)
        elif item.callspec.params["run_trade"] is None and not require_full:
            deselected.append(item)
        else:
            trades.append(item)
    trades.sort(key=lambda item: (
        scenario_order[run.run_id] if (run := item.callspec.params["run_trade"]) is not None else len(scenario_order)
    ))
    items[:] = ordinary + trades + fixture_trades
    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.fixture
def dispute_market_id(pytestconfig):
    return pytestconfig.getoption("--dispute-market-id")


@pytest.fixture
def dispute_no_position_market_id(pytestconfig):
    return pytestconfig.getoption("--dispute-no-position-market-id")


@pytest.fixture
def disputed_market_id(pytestconfig):
    return pytestconfig.getoption("--disputed-market-id")


@pytest.fixture
def test_data():
    return load_test_data()


@pytest.fixture
def test_account(test_data):
    """使用本轮配置的测试账号。"""
    account_name, account = get_test_account(test_data)
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
        if full_preview_required():
            from function.trade_run import CI_TRADE_SCENARIOS

            scenario = request.node.get_closest_marker("m03_case").args[0]
            if scenario in CI_TRADE_SCENARIOS:
                pytest.fail(f"CI 未生成 {scenario} 的运行参数", pytrace=False)
            pytest.fail(f"全量 Preview 缺少 {scenario} 的运行参数，请配置 BUZZING_OWN_TRADE_RUNS", pytrace=False)
        pytest.skip("需要提供该场景的 --trade-run")
    if get_environment() != "preview" or hasattr(pytestconfig, "workerinput"):
        pytest.fail("本场景仅支持串行 Preview 执行", pytrace=False)
    run.check_window()

    from function.polymarket_trade import OrderConfirmationTimeout
    from function.trade_preview import execute_trade
    from function.trade_run import POLYMARKET_BTC_BUY, POLYMARKET_NBA_LIMIT_BUY, POLYMARKET_SPORTS_DRAW_BUY, trade_journal

    def execute(*, sell_btc=False, replay_nba=False, sell_nba=False, sell_sports=False):
        allure.dynamic.parameter("测试账号", run.account)
        allure.dynamic.parameter("交易运行 ID", run.run_id)
        buy = trade_buy_results.get(run.run_id)
        if sell_btc and buy is None:
            pytest.skip("本轮买单未确认成交，跳过两笔卖单")
        if replay_nba and buy is None:
            pytest.skip("本轮 NBA 限价买单未确认成交，跳过幂等重放")
        if sell_nba and buy is None:
            pytest.skip("本轮 NBA 限价买单未确认成交，跳过两笔限价卖单")
        if sell_sports and buy is None:
            pytest.skip("本轮 Moneyline Draw 买单未成功完成，跳过市价卖出")
        root = Path(__file__).resolve().parents[1] / "reports" / "trade-runs"
        evidence_stages = {
            "cash_baseline", "depth_checked", "limit_ticket_expected", "buying_power_checked", "prepared", "signed",
            "partial_quote_selected", "partial_quote_ineligible", "partial_quote_rechecked", "partial_quote_moved",
            "partial_fill_observed", "cash_frozen", "cancel_attempt", "cancel_receipt_checked", "reconciled", "limit_views_checked",
            "cash_available_checked_after_cancel",
            "market_history_checked", "market_selected", "btc_fill_confirmed", "btc_confirmation_timeout",
            "btc_half_sell_confirmed", "btc_remaining_sell_confirmed", "btc_sell_position_checked", "market_fallback",
            "nba_limit_quote_skipped", "nba_limit_depth_checked", "nba_limit_buy_confirmed",
            "nba_limit_sell_quote", "nba_limit_half_sell_confirmed", "nba_limit_remaining_sell_confirmed",
            "nba_limit_sells_reconciled",
            "nba_limit_open", "nba_limit_cash_reserved", "nba_limit_cancelled", "nba_limit_cash_released",
            "nba_limit_replay_attempt", "nba_limit_replay_response", "nba_limit_replay_reconciled", "nba_limit_replay_readback",
            "sports_draw_fill_confirmed", "sports_draw_position_checked", "sports_draw_history_checked",
            "sports_draw_sell_ready", "sports_draw_sell_plan_checked", "sports_draw_sell_confirmed", "sports_draw_sell_history_checked",
            "sports_draw_sell_reconciled",
            "volume_limit_depth_rechecked", "volume_limit_fills_checked",
        }
        evidence_fields = {
            "stage", "eligible_quantity_raw", "best_ask", "best_bid", "balance_raw", "buying_power_raw",
            "expected_buy_limit", "expected_sell_limit", "plan_id", "logical_order_id", "signing_route",
            "order_id", "order_hash", "ordered_raw", "quantity_raw", "remaining_raw", "quote_raw", "fee_raw",
            "cash_raw", "cash_frozen_raw", "position_raw", "available_shares_raw", "position_frozen_raw",
            "cash_available_raw", "expected_cash_available_raw", "expected_cash_frozen_raw",
            "status", "onchain_status", "history_status", "elapsed_seconds", "filled_quantity", "filled_quote",
            "remaining_quantity", "source", "side", "order_type", "funding_source", "budget",
            "event_id", "market_id", "condition_id", "position_id", "title", "cadence", "outcome", "eligible_quote",
            "buy_logical_order_id", "bought_shares", "requested_shares", "sold_shares", "remaining_bought_shares",
            "position_before_buy", "position_before_sell", "position_after_sell", "position_before_buy_raw",
            "asset", "fallback_asset", "phase", "reason", "end_time", "timeout_seconds",
            "market_title", "limit_price", "minimum_shares", "minimum_budget", "depth_shares", "neg_risk",
            "required_buy_shares", "sell_leg", "minimum_filled_quote", "filled_avg_price",
            "observed_limit_price", "observed_quantity_raw", "post_only", "requested_post_only", "last_trade_price", "eligible_quote_raw", "budget_raw", "max_budget_raw",
            "cash_before", "cash_open", "cash_after", "reserved_cash",
            "before", "after", "new_order_ids", "replayed", "replay_logical_order_id", "replay_order_ids",
            "match_id", "position_after_buy", "cash_debit_raw", "cash_proceeds_raw", "verification_status",
            "volume_clob", "expected_fills", "actual_fills", "had_fill",
        }
        attachment_titles = {
            "market_selected": "本轮选中市场",
            "partial_quote_selected": "自营卖一价、成交参考价与报价金额",
            "partial_quote_ineligible": "自营卖盘超出单笔上限",
            "partial_quote_rechecked": "提交前复核卖一与下单份额",
            "partial_quote_moved": "提交前卖一盘口变动",
            "partial_fill_observed": "撤单前的部分成交和未成交份额",
            "signed": "Privy 钱包签名与订单哈希",
            "cash_frozen": "挂单冻结与可用余额",
            "cash_available_checked_after_cancel": "撤单后的冻结与可用余额",
            "reconciled": "订单终态、成交金额与资金持仓对账", "btc_fill_confirmed": "BTC 成交详情",
            "btc_half_sell_confirmed": "BTC 第一次卖出（50%）", "btc_remaining_sell_confirmed": "BTC 第二次卖出（剩余）",
            "btc_sell_position_checked": "BTC 卖出后持仓核对",
            "btc_confirmation_timeout": "订单确认超时",
            "market_fallback": "BTC 市场关闭，切换 ETH",
            "nba_limit_quote_skipped": "NBA 限价报价排除",
            "nba_limit_depth_checked": "NBA 限价提交前盘口核对",
            "nba_limit_buy_confirmed": "NBA 限价买入成交详情",
            "nba_limit_sell_quote": "NBA 限价卖出 · 盘口与可成交份额",
            "nba_limit_half_sell_confirmed": "NBA 第一次限价卖出（50%）",
            "nba_limit_remaining_sell_confirmed": "NBA 第二次限价卖出（剩余）",
            "nba_limit_sells_reconciled": "NBA 两笔卖出的成交金额和持仓核对",
            "nba_limit_open": "NBA 限价挂单详情",
            "nba_limit_cash_reserved": "NBA 挂单占用资金",
            "nba_limit_cancelled": "NBA 撤单终态",
            "nba_limit_cash_released": "NBA 撤单资金释放",
            "nba_limit_replay_attempt": "同计划重放前快照",
            "nba_limit_replay_response": "同计划重放回执",
            "nba_limit_replay_reconciled": "重放订单与资金对账",
            "nba_limit_replay_readback": "重放后回查原单",
            "sports_draw_fill_confirmed": "Moneyline Draw 市价买入成交",
            "sports_draw_position_checked": "Moneyline Draw 买入前后持仓",
            "sports_draw_history_checked": "Moneyline Draw 订单历史核对",
            "sports_draw_sell_ready": "Moneyline Draw 卖出前持仓、买盘与资金",
            "sports_draw_sell_plan_checked": "Moneyline Draw 卖出准备单核对",
            "sports_draw_sell_confirmed": "Moneyline Draw 市价卖出成交",
            "sports_draw_sell_history_checked": "Moneyline Draw 卖单历史与手续费",
            "sports_draw_sell_reconciled": "Moneyline Draw 卖出后持仓与到账",
            "volume_limit_depth_rechecked": "提交前自营盘口与预期吃单量",
            "volume_limit_fills_checked": "自营盘口预期与实际成交明细",
            "cancel_receipt_checked": "自营撤单回执与订单终态",
        }
        selling = sell_btc or sell_nba or sell_sports
        with trade_journal(root, run, selling=selling, nba_replay=replay_nba) as record:
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
                if replay_nba or sell_nba:
                    return execute_trade(run, record_evidence, nba_buy=buy, sell_nba=sell_nba)
                result = execute_trade(run, record_evidence, btc_buy=buy if sell_btc else None,
                                       sports_buy=buy if sell_sports else None)
            except OrderConfirmationTimeout as error:
                pytest.skip(str(error))
            if not selling and run.scenario in {POLYMARKET_BTC_BUY, POLYMARKET_NBA_LIMIT_BUY, POLYMARKET_SPORTS_DRAW_BUY}:
                trade_buy_results[run.run_id] = result
            return result

    return execute


@pytest.fixture(scope="module")
def trade_buy_results():
    return {}
