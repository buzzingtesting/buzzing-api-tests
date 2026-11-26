import json
from decimal import Decimal
from time import monotonic, time

import allure
import pytest

from function.api_client import request
from function.orders import list_orders
from function.polymarket_limit_trade import CANCELLED, LIMIT_EXPIRATION_SECONDS, LIMIT_FEE_RESERVE
from function.polymarket_limit_trade import read_asks, read_withdrawable_cash, wait_cash_snapshot, wait_limit_order
from function.polymarket_trade import read_cash_position, shares, submit_polymarket_order, wait_for_cash_position
from function.reporting import PROJECT_NAME, ordered_suite_name
from function.response_checks import expect_json
from function.trade_contract import TradeFixture, require_empty_market_orders
from function.trade_run import raw_amount
from function.trade_state import BOOK


@allure.parent_suite(PROJECT_NAME)
@allure.suite(ordered_suite_name("限价单"))
@allure.epic(PROJECT_NAME)
@allure.feature("限价单")
@allure.title("Polymarket 限价买入 · 部分成交后撤销余单")
@pytest.mark.b3_fixture
def test_polymarket_partial_fill_and_cancel():
    fixture = TradeFixture("polymarket_partial")
    expected_fill = Decimal(shares(raw_amount(fixture.spec["opposing_shares_raw"])))
    assert expected_fill > 0, "专用盘口应有可控的对手卖单"
    with fixture.connect() as (client, signer, maker):
        selected = fixture.select_market(client)
        allure.dynamic.parameter("市场", selected["market_id"])

        def record(**evidence):
            stage = evidence.pop("stage")
            allure.attach(json.dumps({**selected, **evidence}, ensure_ascii=False, indent=2), stage,
                          allure.attachment_type.JSON)

        market_filters = require_empty_market_orders(client, fixture.source, selected)
        cash_before, pusd = read_withdrawable_cash(client, fixture.uid)
        assert min(cash_before, pusd) >= Decimal(shares(fixture.budget_raw)), "需预置足额 pUSD 和可提现余额"
        position_before, _ = read_cash_position(client, fixture.uid, selected)

        payload = dict(side="BUY", total_budget=str(fixture.budget_raw), price=str(fixture.price), post_only=False,
                       expiration=str(int(time()) + LIMIT_EXPIRATION_SECONDS))
        planned = None

        def confirm_fixture_depth(plan):
            nonlocal planned
            planned = Decimal(shares(raw_amount(plan["legs"][0]["quantity"])))
            assert len(plan["legs"]) == 1, "本场景只验证单腿 Cash"
            fees = Decimal(shares(raw_amount(plan["legs"][0]["est_fee"])))
            crossing = [quantity for price, quantity in read_asks(client, selected["position_id"]) if price <= fixture.price]
            assert sum(crossing) == expected_fill < planned, "限价内卖盘与本轮配置不符或足以全部成交"
            assert planned * fixture.price + fees <= Decimal(shares(fixture.budget_raw)), "限价及费用超过预算"
            assert planned >= Decimal(selected["minimum_shares"]) and planned * fixture.price * (1 + LIMIT_FEE_RESERVE) <= pusd
            record(stage="准备单与对手深度", planned_shares=str(planned), crossing_shares=str(expected_fill),
                   budget_raw=fixture.budget_raw, cash_before=str(cash_before))

        assert sum(quantity for price, quantity in read_asks(client, selected["position_id"])
                   if price <= fixture.price) == expected_fill, "准备前对手卖单已变动"
        logical, order_id, started = submit_polymarket_order(
            client, fixture, signer, maker, record, selected, selected["neg_risk"], payload,
            before_submit=confirm_fixture_depth,
        )
        ids = {"logical_order_id": logical, "order_id": order_id}
        partial = cancelled = None
        try:
            partial = wait_limit_order(client, fixture.uid, selected, record, logical, order_id, started, partial=True)
            filled = Decimal(partial["filled_quantity"])
            remaining = Decimal(partial["remaining_quantity"])
            assert partial["status"] in {"partial", "open"} and filled == expected_fill and filled + remaining == planned
            assert remaining > 0 and Decimal(partial["filled_quote"]) > 0
            cash_held, _ = read_withdrawable_cash(client, fixture.uid)
            locked = remaining * fixture.price
            record(stage="部分成交及剩余挂单", **ids, status=partial["status"], filled_quantity=str(filled),
                   remaining_quantity=str(remaining), remaining_reserve=str(locked), cash_held=str(cash_held))
        finally:
            if partial is None or partial["status"] in {"open", "partial"}:
                record(stage="发送撤单并回查终态", **ids)
                expect_json(request(client, "POST", BOOK + "order/cancel", operation="cancel_partial_polymarket_order",
                                    json={"order_id": order_id, "source": fixture.source}), 200)
                cancelled = wait_limit_order(client, fixture.uid, selected, record, logical, order_id, monotonic(), cancelling=True)
                record(stage="撤单终态", **ids, status=cancelled["status"], filled_quantity=cancelled.get("filled_quantity"),
                       remaining_quantity=cancelled.get("remaining_quantity"))
        assert cancelled is not None, "订单在观察到部分成交前已结束"
        assert cancelled["status"] in CANCELLED and Decimal(cancelled["filled_quantity"]) == filled
        matches = expect_json(list_orders(client, params={**market_filters, "view": "history"}), 200)["items"]
        rows = [row["order"] for row in matches if row["order"]["logical_order_id"] == logical]
        assert len(rows) == 1 and rows[0]["order_id"] == order_id and rows[0]["ended_reason"] == "cancelled"
        assert rows[0]["had_fill"] is True and Decimal(rows[0]["filled_quantity"]) == filled
        expected_position_raw = position_before + int(filled * 1_000_000)
        position_after, _ = wait_for_cash_position(client, fixture.uid, selected, expected_position_raw)
        cash_after = wait_cash_snapshot(client, fixture.uid, cash_held + locked)
        assert cash_after < cash_before, "撤单保留已成交部分的扣款"
        record(stage="部分成交撤单后的资金、持仓与历史", **ids, position_before=str(position_before),
               position_after=str(position_after), cash_after=str(cash_after), released_cash=str(locked),
               filled_quote=rows[0]["filled_quote"], history_status=rows[0]["ended_reason"])
