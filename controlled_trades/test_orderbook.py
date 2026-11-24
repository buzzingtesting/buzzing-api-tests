import pytest

from function.trade_preview import execute_trade
from function.trade_run import LIMIT_BUY, LIMIT_CANCEL, MARKET_SELL


@pytest.mark.m03_case(LIMIT_BUY)
def test_limit_buy_no(trade_fixture):
    run_case(trade_fixture)


@pytest.mark.m03_case(MARKET_SELL)
def test_market_sell_no(trade_fixture):
    run_case(trade_fixture)


@pytest.mark.m03_case(LIMIT_CANCEL)
def test_post_only_limit_buy_cancel_no(trade_fixture):
    run_case(trade_fixture)


def run_case(trade_fixture):
    run, journal = trade_fixture
    with journal as record:
        execute_trade(run, record)
