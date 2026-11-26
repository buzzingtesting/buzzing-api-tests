import allure
import pytest

from function.reporting import PROJECT_NAME, ordered_suite_name
from function.trade_run import MARKET_BUY, MARKET_SELL, POLYMARKET_BTC_BUY


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("市价单")),
    allure.epic(PROJECT_NAME),
    allure.feature("市价单"),
]


@allure.title("市价买入 · 成交、持仓增加与订单历史")
@pytest.mark.m03_case(MARKET_BUY)
def test_market_buy_no(run_trade):
    run_trade()


@allure.title("市价卖出 · 可卖持仓、成交、资金与订单历史")
@pytest.mark.m03_case(MARKET_SELL)
def test_market_sell_no(run_trade):
    run_trade()


@allure.title("Polymarket BTC 二元循环 · 1.5 市价买入与成交确认")
@pytest.mark.m03_case(POLYMARKET_BTC_BUY)
def test_polymarket_btc_market_buy(run_trade):
    run_trade()


@allure.title("Polymarket BTC 二元循环 · 市价卖出 50% 并卖出剩余份额")
@pytest.mark.m03_case(POLYMARKET_BTC_BUY)
def test_polymarket_btc_market_sell(run_trade):
    run_trade(sell_btc=True)
