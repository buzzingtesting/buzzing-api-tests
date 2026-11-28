import os

import allure
import pytest

from function.reporting import PROJECT_NAME, ordered_suite_name
from function.trade_run import LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, POLYMARKET_NBA_LIMIT_BUY
from function.trade_run import POLYMARKET_NBA_LIMIT_CANCEL


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("限价单")),
    allure.sub_suite("下单与撤单"),
    allure.epic(PROJECT_NAME),
    allure.feature("限价单"),
]


@allure.title("限价买入 · 盘口、购买力、成交与订单历史")
@pytest.mark.m03_case(LIMIT_BUY)
def test_limit_buy_no(run_trade):
    run_trade()


@allure.title("自营限价买入 · 部分成交、撤余单与资金释放")
@pytest.mark.m03_case(LIMIT_PARTIAL_CANCEL)
def test_limit_partial_cancel_no(run_trade):
    run_trade()


@allure.title("自营限价买入 · 零成交挂单、撤单与可用余额恢复")
@pytest.mark.m03_case(LIMIT_CANCEL)
@pytest.mark.skipif(os.getenv("BUZZING_WALLET_SIGNER_SETUP_FAILED") == "1",
                    reason="Preview 钱包签名依赖初始化失败，见 Actions 安装步骤")
def test_own_limit_cancel_restores_available_cash(run_trade):
    run_trade()


@allure.title("Polymarket NBA 多元 · 3 U 限价买入与全成确认")
@pytest.mark.m03_case(POLYMARKET_NBA_LIMIT_BUY)
def test_polymarket_nba_limit_buy(run_trade):
    run_trade()


@allure.title("Polymarket NBA 限价买入 · 同计划重放与订单资金对账")
@pytest.mark.m03_case(POLYMARKET_NBA_LIMIT_BUY)
@pytest.mark.trade_followup
def test_polymarket_nba_limit_buy_replay(run_trade):
    run_trade(replay_nba=True)


@allure.title("Polymarket NBA 多元 · 两次限价卖出及成交金额核对")
@pytest.mark.m03_case(POLYMARKET_NBA_LIMIT_BUY)
@pytest.mark.trade_followup
def test_polymarket_nba_limit_sell(run_trade):
    run_trade(sell_nba=True)


@allure.title("Polymarket NBA 多元 · 被动挂单、撤单与资金释放")
@pytest.mark.m03_case(POLYMARKET_NBA_LIMIT_CANCEL)
def test_polymarket_nba_limit_cancel(run_trade):
    run_trade()
