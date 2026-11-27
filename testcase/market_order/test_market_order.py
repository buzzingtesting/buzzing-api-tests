import allure
import pytest

from function.reporting import PROJECT_NAME, ordered_suite_name
from function.trade_run import POLYMARKET_BTC_BUY, POLYMARKET_SPORTS_DRAW_BUY


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite(ordered_suite_name("市价单")),
    allure.epic(PROJECT_NAME),
    allure.feature("市价单"),
]


@allure.title("体育 Moneyline Draw · 1.3 U 市价买入、持仓与订单历史")
@pytest.mark.m03_case(POLYMARKET_SPORTS_DRAW_BUY)
def test_polymarket_sports_draw_market_buy(run_trade):
    run_trade()


@allure.title("体育 Moneyline Draw · 市价卖出、可卖持仓、资金与订单历史")
@pytest.mark.m03_case(POLYMARKET_SPORTS_DRAW_BUY)
@pytest.mark.trade_followup
def test_polymarket_sports_draw_market_sell(run_trade):
    run_trade(sell_sports=True)


@allure.title("Polymarket BTC 二元循环 · 1.5 市价买入与成交确认")
@pytest.mark.m03_case(POLYMARKET_BTC_BUY)
def test_polymarket_btc_market_buy(run_trade):
    run_trade()


@allure.title("Polymarket BTC 二元循环 · 市价卖出 50% 并卖出剩余份额")
@pytest.mark.m03_case(POLYMARKET_BTC_BUY)
@pytest.mark.trade_followup
def test_polymarket_btc_market_sell(run_trade):
    run_trade(sell_btc=True)
