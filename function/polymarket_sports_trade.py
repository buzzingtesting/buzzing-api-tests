from decimal import Decimal
from random import sample
from time import monotonic, sleep, time

import pytest

from function.market import get_sports_match_detail, list_event_markets, list_sports_matches
from function.orders import list_orders
from function.polymarket_limit_trade import read_depth_levels, read_withdrawable_cash, wait_cash_snapshot
from function.polymarket_trade import (
    MarketBuyFill,
    MarketClosed,
    POSITION_SYNC_SECONDS,
    SELL_STEP_RAW,
    SOURCE,
    check_cash_buy_capacity,
    ensure_market_trading,
    execute_polymarket_order,
    fill_quantity_raw,
    market_closed_reason,
    match_polymarket_order,
    read_cash_position,
    read_eligible_quote,
    shares,
    wait_for_cash_position,
)
from function.positions import list_position_history
from function.response_checks import expect_json
from function.trade_run import raw_amount, timestamp


def select_sports_draw(client, run, record, config):
    page = expect_json(list_sports_matches(client, params=config["list_params"]), 200)
    matches = [
        item["match"] for item in page["items"]
        if item["match"]["status"] == "scheduled" or (item["match"].get("live") or {}).get("is_live") is True
    ]
    for match in sample(matches, len(matches)):
        detail = expect_json(get_sports_match_detail(client, match["id"], params=config["detail_params"]), 200)
        if any(detail["match"][key] != match[key] for key in ("id", "event_id")):
            raise ValueError("体育详情的比赛或事件 ID 与列表不一致")
        draw = next((
            leg for section in detail["market_sections"] for block in section["blocks"]
            if block["key"] == "game_lines.moneyline" for leg in block["legs"]
            if leg["label"].strip().upper() == "DRAW" and leg["market_type"] == SOURCE
        ), None)
        if draw is None or market_closed_reason(draw):
            continue
        markets = expect_json(list_event_markets(client, match["event_id"]), 200)["markets"]
        market_id = str(draw["market_id"])
        child = next((row for row in markets if str(row["market"]["id"]) == market_id), None)
        if child is None:
            raise ValueError("Moneyline Draw 盘口不属于列表中的比赛事件")
        market = child["market"]
        end_time = timestamp(market["market_end_date"]).timestamp()
        if market_closed_reason(market) or time() >= end_time:
            continue
        yes = next((option for option in child["options"] if option["outcome_label"].upper() == "YES"), None)
        if (market["market_type"] != SOURCE or market.get("sports_market_type") != "moneyline"
                or draw["condition_id"] != market["condition_id"] or yes is None
                or draw["position_id"] != yes["position_id"] or draw["outcome_index"] != 0
                or type(draw.get("is_negrisk")) is not bool):
            raise ValueError("Moneyline Draw 的市场、条件或 Yes 份额与事件详情不一致")
        quote = read_eligible_quote(client, draw["position_id"], Decimal("1"))
        if quote < Decimal(shares(run.budget_raw)):
            continue
        selected = {
            "match_id": match["id"],
            "event_id": match["event_id"],
            "market_id": market_id,
            "title": f"{match['home_team']['name']} vs {match['away_team']['name']}",
            "condition_id": draw["condition_id"],
            "position_id": draw["position_id"],
            "outcome": "DRAW",
            "end_time": end_time,
            "eligible_quote": str(quote),
        }
        record(stage="market_selected", **selected)
        return selected, draw["is_negrisk"]
    pytest.skip(f"体育列表 {len(page['items'])} 场比赛中没有可按 1.3 U 买入的 Moneyline Draw")


def verify_sports_draw_history(client, selected, fill, quantity_raw, record, *, side, budget_raw=None):
    params = {"source": SOURCE, "condition_id": selected["condition_id"],
              "position_id": selected["position_id"], "page_size": 20}
    deadline = monotonic() + POSITION_SYNC_SECONDS
    while True:
        history = expect_json(list_position_history(client, params=params), 200)
        if any(history[key] != params[key] for key in ("source", "condition_id", "position_id")):
            raise ValueError("持仓历史未返回本轮 Moneyline Draw")
        rows = [row for row in history["rows"] if row.get("order_hash") == fill["order_id"]]
        if rows:
            row = rows[0]
            if (len(rows) != 1 or row["type"] != side or row["order_type"] != "market"
                    or raw_amount(row["shares"]) != quantity_raw):
                raise ValueError("Moneyline Draw 市价交易历史与成交记录不一致")
            amount = row["amount"]
            fee_raw = raw_amount(row["fee_amount"])
            gross = Decimal(fill["filled_quote"])
            if side == "buy":
                if not amount.startswith("-"):
                    raise ValueError("买入历史应返回负数扣款")
                debit_raw = raw_amount(amount[1:])
                if Decimal(shares(debit_raw - fee_raw)) != gross or debit_raw > budget_raw:
                    raise ValueError("买入历史扣款应等于成交额加手续费，且不得超过本轮预算")
                record(stage="sports_draw_history_checked", **selected, **fill, history_status=side,
                       cash_debit_raw=debit_raw, fee_raw=fee_raw)
                return debit_raw
            proceeds_raw = raw_amount(amount)
            if proceeds_raw <= 0 or Decimal(shares(proceeds_raw + fee_raw)) != gross:
                raise ValueError("卖出历史到账应等于成交额减去手续费")
            record(stage="sports_draw_sell_history_checked", **selected, **fill, history_status=side,
                   cash_proceeds_raw=proceeds_raw, fee_raw=fee_raw)
            return proceeds_raw
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ValueError(f"持仓历史未查到订单 {fill['order_id']}")
        sleep(min(1, remaining))


def execute_sports_draw_buy(client, run, uid, signer, maker, record, config):
    selected, neg_risk = select_sports_draw(client, run, record, config)
    check_cash_buy_capacity(client, run, selected, signer, record)
    before, _ = read_cash_position(client, uid, selected)
    record(stage="cash_baseline", **selected, position_raw=before)
    fill = execute_polymarket_order(client, run, uid, signer, maker, record, selected, neg_risk,
                                    {"side": "BUY", "total_budget": str(run.budget_raw)})
    bought = fill_quantity_raw(fill)
    record(stage="sports_draw_fill_confirmed", **selected, **fill, budget=shares(run.budget_raw),
           side="BUY", order_type="MARKET", funding_source=run.funding)
    if Decimal(fill["filled_quote"]) > Decimal(shares(run.budget_raw)):
        raise ValueError(f"订单 {fill['order_id']} 的成交金额超过 1.3 U 预算")
    expected = before + bought
    # The order can report two decimal places while positions retain six.
    # Require a positive increase even when the fill is smaller than one sell step.
    tolerance = min(bought, SELL_STEP_RAW) - 1
    after, _ = wait_for_cash_position(client, uid, selected, expected, tolerance_raw=tolerance)
    record(stage="sports_draw_position_checked", **selected, **fill, position_before_buy=shares(before),
           position_after_buy=shares(after), bought_shares=shares(bought),
           expected_position_after_buy=shares(expected), position_difference=shares(after - expected),
           position_tolerance=shares(tolerance))
    verify_sports_draw_history(client, selected, fill, bought, record, side="buy", budget_raw=run.budget_raw)
    return MarketBuyFill(selected, neg_risk, uid, fill["logical_order_id"], fill["order_id"], bought, before)


def verify_sports_draw_sell_plan(plan, quantity):
    # The shared submit path has already verified the single Polymarket Cash leg.
    leg = plan["legs"][0]
    if (leg["side"] != "SELL" or raw_amount(leg["quantity"]) != quantity
            or raw_amount(leg["maker_amount"]) != quantity):
        raise ValueError("Moneyline Draw 卖出准备单的方向或份额与本轮可卖份额不一致")
    if plan.get("verification_status") != "verified" or plan.get("credit_applied") is not False:
        raise ValueError("Moneyline Draw 卖出准备单应通过校验并仅使用 Cash")


def execute_sports_draw_sell(client, run, uid, signer, maker, record, buy):
    if uid != buy.uid:
        raise ValueError("Moneyline Draw 买卖用例的登录账号不一致")
    selected = buy.selection
    response = list_orders(client, params={"source": SOURCE, "logical_order_ids": buy.logical_order_id, "page_size": 100})
    order = match_polymarket_order(response, buy.logical_order_id, buy.order_id, selected, uid, "BUY", "MARKET")
    if (order is None or order["status"] != "filled" or order.get("onchain_status") not in {"confirmed", "none"}
            or fill_quantity_raw(order) != buy.quantity_raw):
        raise ValueError("无法确认同一轮 Moneyline Draw 买单的最终成交份额")

    phase = "Moneyline Draw 卖出前"
    try:
        ensure_market_trading(client, selected, phase, refresh=True)
    except MarketClosed as error:
        pytest.skip(str(error))
    baseline = buy.position_before_buy_raw
    before, available = wait_for_cash_position(client, uid, selected, baseline + buy.quantity_raw, phase=phase,
                                               tolerance_raw=min(buy.quantity_raw, SELL_STEP_RAW) - 1)
    quantity = min(buy.quantity_raw, before - baseline) // SELL_STEP_RAW * SELL_STEP_RAW
    if quantity <= 0 or quantity > available:
        raise ValueError("Moneyline Draw 没有足够可卖份额完成本轮卖单")
    bids = read_depth_levels(client, selected["position_id"], "bids")
    depth = sum(size for _, size in bids)
    if depth < Decimal(shares(quantity)):
        raise ValueError("Moneyline Draw 买盘不足以成交本轮市价卖单")
    cash_before, _ = read_withdrawable_cash(client, uid)
    evidence = {**selected, "buy_logical_order_id": buy.logical_order_id}
    record(stage="sports_draw_sell_ready", **evidence, requested_shares=shares(quantity),
           available_shares_raw=available, position_before_sell=shares(before), depth_shares=str(depth),
           cash_before=str(cash_before))

    def check_sell_plan(plan):
        verify_sports_draw_sell_plan(plan, quantity)
        record(stage="sports_draw_sell_plan_checked", **evidence, plan_id=plan["plan_id"],
               logical_order_id=plan["logical_order_id"], side="SELL", quantity_raw=quantity,
               funding_source="cash", verification_status=plan["verification_status"])

    fill = execute_polymarket_order(client, run, uid, signer, maker, record, selected, buy.neg_risk,
                                    {"side": "SELL", "quantity": str(quantity)}, before_submit=check_sell_plan)
    sold = fill_quantity_raw(fill)
    record(stage="sports_draw_sell_confirmed", **evidence, **fill, side="SELL", sold_shares=shares(sold))
    if sold != quantity or fill["logical_order_id"] == buy.logical_order_id:
        raise ValueError("Moneyline Draw 卖单的成交份额或逻辑订单号与预期不符")
    remaining = before - baseline - sold
    after, _ = wait_for_cash_position(client, uid, selected, before - sold, baseline_raw=baseline)
    proceeds = verify_sports_draw_history(client, evidence, fill, sold, record, side="sell")
    cash_after = wait_cash_snapshot(client, uid, cash_before + Decimal(shares(proceeds)))
    record(stage="sports_draw_sell_reconciled", **evidence, **fill, sold_shares=shares(sold),
           position_before_buy=shares(baseline), position_after_sell=shares(after),
           remaining_bought_shares=shares(remaining), cash_before=str(cash_before), cash_after=str(cash_after),
           cash_proceeds_raw=proceeds)
