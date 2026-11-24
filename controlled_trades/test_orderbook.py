from function.trade_preview import execute_trade


def test_limit_buy_no(trade_fixture):
    run, journal = trade_fixture
    with journal as record:
        execute_trade(run, record)
