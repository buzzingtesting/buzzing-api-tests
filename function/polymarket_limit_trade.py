from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from random import sample
from re import IGNORECASE, escape, search
from time import monotonic, sleep, time

from function.api_client import request
from function.funds import get_buying_power
from function.market import get_market_event_detail, get_orderbook_depth, is_open_market_event
from function.market import iter_market_events, list_event_markets
from function.orders import list_orders
from function.polymarket_trade import CONFIRMATION_SECONDS, SELL_STEP_RAW, SOURCE, OrderConfirmationTimeout
from function.polymarket_trade import check_cash_buy_capacity, execute_polymarket_order, fill_quantity_raw
from function.polymarket_trade import market_closed_reason, match_polymarket_order, shares, submit_polymarket_order
from function.polymarket_trade import ensure_market_trading, read_cash_position, wait_for_cash_position
from function.response_checks import expect_json
from function.trade_run import POLYMARKET_NBA_LIMIT_CANCEL, price_amount, raw_amount, timestamp
from function.trade_state import BOOK


LIMIT_FEE_RESERVE = Decimal("0.09")
DEFAULT_MINIMUM_SHARES = Decimal("5")
LIMIT_EXPIRATION_SECONDS = 300
YES_NO = ("YES", "NO")
CANCELLED = {"cancelled", "canceled"}
TERMINAL = {"filled", "rejected", "expired", "failed", *CANCELLED}


@dataclass(frozen=True)
class NbaLimitFill:
    selected: dict
    uid: str
    plan_id: str
    fill: dict
    position_before_buy_raw: int


def read_depth_levels(client, position_id, side):
    depth = expect_json(get_orderbook_depth(client, position_id, source=SOURCE), 200)
    if depth.get("position_id") != position_id:
        raise ValueError("盘口深度未返回所选方向")
    levels = []
    for row in depth.get(side) or []:
        price = price_amount(row["price"], allow_one=True)
        quantity = Decimal(row["total_qty"])
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("盘口数量无效")
        if quantity > 0:
            levels.append((price, quantity))
    return levels


def read_asks(client, position_id):
    return read_depth_levels(client, position_id, "asks")


def select_nba_limit_quote(client, record, case, *, budget_raw=None, passive=False, split_sell=False,
                           market_ids=(), outcome=""):
    budget = Decimal(shares(budget_raw)) if budget_raw is not None else None
    keyword = case["title_keyword"]
    for event in iter_market_events(client, case["list_params"]):
        if (event.get("type") != SOURCE or not search(rf"\b{escape(keyword)}\b", event["title"], IGNORECASE)
                or (event.get("market_count") or 0) < 2 or not is_open_market_event(event, time())):
            continue
        listed = (event.get("markets") or [])
        if not listed:
            continue
        first = listed[0]
        market_id = str(first["market"]["id"])
        if market_ids and market_id not in market_ids:
            continue
        children = expect_json(list_event_markets(client, event["id"]), 200)["markets"]
        child = next((item for item in children if str(item["market"]["id"]) == market_id), None)
        if child is None:
            raise ValueError(f"NBA 事件 {event['id']} 的第一个子市场 {market_id} 缺少详情")
        market = child["market"]
        if market["market_type"] != SOURCE or market["outcome_kind"] != "binary" or market_closed_reason(market):
            continue
        options = {row["outcome_label"].upper(): row["position_id"] for row in child["options"]}
        if set(options) != set(YES_NO):
            continue
        condition_id = market["condition_id"]
        listed_options = {row["outcome_label"].upper(): row["position_id"] for row in first["options"]}
        if not condition_id or condition_id != first["market"]["condition_id"] or options != listed_options:
            raise ValueError(f"NBA 市场 {market_id} 的条件或方向标识与列表不一致")

        minimum = Decimal(str(market.get("order_min_size") or DEFAULT_MINIMUM_SHARES))
        tick = Decimal(str(market.get("order_price_min_tick_size") or 0))
        if not minimum.is_finite() or minimum <= 0 or not tick.is_finite() or tick < 0:
            raise ValueError(f"NBA 市场 {market_id} 的最小份额或价格步长无效")
        required_shares = 2 * minimum.quantize(Decimal("0.01"), rounding=ROUND_CEILING) if split_sell else minimum
        outcomes = (outcome,) if outcome else sample(YES_NO, len(YES_NO))
        for outcome in outcomes:
            token_id = options[outcome]
            asks = read_asks(client, token_id)
            if not asks:
                record(stage="nba_limit_quote_skipped", market_id=market_id, outcome=outcome, reason="无卖盘")
                continue
            best_ask = min(level[0] for level in asks)
            price = best_ask - tick * case["post_only_ticks"] if passive else best_ask
            depth_at_limit = sum(quantity for ask, quantity in asks if ask == price)
            minimum_budget = (required_shares * price * (1 + LIMIT_FEE_RESERVE)).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING,
            )
            insufficient = budget is not None and (minimum_budget > budget or (not passive and depth_at_limit < required_shares))
            if not 0 < price < 1 or (tick and price % tick) or (passive and price >= best_ask) or insufficient:
                record(stage="nba_limit_quote_skipped", market_id=market_id, outcome=outcome, limit_price=str(price),
                       minimum_budget=str(minimum_budget), reason="限价或盘口无法满足最小份额和本轮预算")
                continue
            detail = expect_json(get_market_event_detail(client, event["id"]), 200)
            neg_risk = detail.get("neg_risk")
            if str(detail.get("id")) != event["id"] or type(neg_risk) is not bool:
                raise ValueError(f"NBA 事件 {event['id']} 缺少有效的 neg_risk")
            if isinstance(market.get("is_negrisk"), bool) and market["is_negrisk"] != neg_risk:
                raise ValueError(f"NBA 事件 {event['id']} 的 neg_risk 与子市场不一致")
            end_time = int(event["end_time"])
            if market.get("market_end_date"):
                end_time = min(end_time, int(timestamp(market["market_end_date"]).timestamp()))
            selected = dict(event_id=event["id"], title=event["title"], market_id=market_id, market_title=market.get("question"),
                            condition_id=condition_id, position_id=token_id, outcome=outcome, end_time=end_time,
                            limit_price=str(price), best_ask=str(best_ask), minimum_shares=str(minimum),
                            required_buy_shares=str(required_shares), minimum_budget=str(minimum_budget),
                            depth_shares=str(depth_at_limit),
                            neg_risk=neg_risk)
            record(stage="market_selected", **selected)
            return selected
    raise ValueError("24hr Volume 列表里没有符合要求的 NBA 多元市场及限价报价")


def execute_nba_limit_buy(client, run, uid, signer, maker, record, case):
    passive = run.scenario == POLYMARKET_NBA_LIMIT_CANCEL
    budget = Decimal(shares(run.budget_raw))
    selected = select_nba_limit_quote(client, record, case, budget_raw=run.budget_raw, passive=passive,
                                      split_sell=not passive, market_ids=run.market_ids, outcome=run.outcome)
    balance_raw = check_cash_buy_capacity(client, run, selected, signer, record)
    if not passive:
        position_before, _ = read_cash_position(client, uid, selected)
    price = Decimal(selected["limit_price"])
    payload = dict(side="BUY", total_budget=str(run.budget_raw), price=selected["limit_price"], post_only=passive,
                   expiration=str(int(time()) + LIMIT_EXPIRATION_SECONDS))
    planned_raw = None
    plan_id = None
    estimated_fee = Decimal(0)

    def recheck_depth(plan):
        nonlocal planned_raw, plan_id, estimated_fee
        plan_id = plan["plan_id"]
        leg = plan["legs"][0]
        planned_raw = raw_amount(leg["quantity"])
        requested = Decimal(shares(planned_raw))
        estimated_fee = Decimal(shares(raw_amount(leg.get("est_fee") or "0")))
        expected_total = requested * price + estimated_fee
        if expected_total > budget + Decimal("0.000001"):
            raise ValueError(f"准备单预计成交额及手续费超过 {budget:f} U 预算")
        at_limit = sum(quantity for ask, quantity in read_asks(client, selected["position_id"]) if ask <= price)
        if requested < Decimal(selected["required_buy_shares"]):
            raise ValueError("准备单份额不足以满足本轮限价场景")
        if not passive and at_limit < requested:
            raise ValueError("准备单份额低于最小值，或提交前限价内的卖盘已不足")
        if passive and at_limit:
            raise ValueError("盘口已变动，post-only 限价会与卖盘交叉")
        record(stage="nba_limit_depth_checked", **dict(selected, depth_shares=str(at_limit)), requested_shares=shares(planned_raw))

    if passive:
        submission = submit_polymarket_order(client, run, signer, maker, record, selected, selected["neg_risk"],
                                              payload, before_submit=recheck_depth)
        return verify_passive_order_and_cancel(
            client, uid, selected, record, submission, planned_raw, Decimal(shares(balance_raw)), estimated_fee,
        )
    fill = execute_polymarket_order(client, run, uid, signer, maker, record, selected, selected["neg_risk"],
                                    payload, before_submit=recheck_depth)
    if fill_quantity_raw(fill) != planned_raw:
        raise ValueError(f"订单 {fill['logical_order_id']} 的成交份额与 3 U 限价计划不一致")
    if Decimal(fill["filled_quote"]) > price * Decimal(fill["filled_quantity"]) + Decimal("0.000001"):
        raise ValueError(f"订单 {fill['logical_order_id']} 的成交金额超过限价")
    record(stage="nba_limit_buy_confirmed", **selected, **fill, side="BUY", source=SOURCE, order_type="LIMIT",
           funding_source=run.funding, budget=shares(run.budget_raw), requested_shares=shares(planned_raw),
           position_before_buy_raw=position_before)
    return NbaLimitFill(selected, uid, plan_id, fill, position_before)


def execute_nba_limit_sell(client, run, uid, signer, maker, record, buy):
    if uid != buy.uid or buy.fill["status"] != "filled" or buy.fill["onchain_status"] not in {"confirmed", "none"}:
        raise ValueError("卖出需使用同一账号本轮已确认全成的 NBA 限价买单")

    selected = buy.selected
    baseline = buy.position_before_buy_raw
    bought_raw = fill_quantity_raw(buy.fill)
    remaining = bought_raw
    sellable_raw = bought_raw // SELL_STEP_RAW * SELL_STEP_RAW
    half_raw = sellable_raw // (2 * SELL_STEP_RAW) * SELL_STEP_RAW
    quantities = (half_raw, sellable_raw - half_raw)
    if Decimal(shares(half_raw)) < Decimal(selected["minimum_shares"]):
        raise ValueError("本轮买入份额不足以拆成两笔满足最小份额的限价卖单")
    logical_ids = {buy.fill["logical_order_id"]}
    total_quote = Decimal(0)
    total_minimum_quote = Decimal(0)

    for leg, quantity_raw in enumerate(quantities, start=1):
        phase = f"第 {leg} 笔 NBA 限价卖出前"
        ensure_market_trading(client, selected, phase, refresh=True)
        before, available = wait_for_cash_position(client, uid, selected, baseline + remaining, phase=phase)
        if quantity_raw > available:
            raise ValueError(f"{phase}可卖份额不足")
        quantity = Decimal(shares(quantity_raw))
        bids = read_depth_levels(client, selected["position_id"], "bids")
        if not bids:
            raise ValueError(f"{phase}盘口没有买盘")
        limit = max(price for price, _ in bids)
        if limit >= 1:
            raise ValueError(f"{phase}买一价超出可用限价范围")
        depth = sum(size for price, size in bids if price >= limit)
        if depth < quantity:
            raise ValueError(f"{phase}买一深度不足，需 {quantity} 份，实际 {depth} 份")
        selection = {**selected, "limit_price": str(limit), "depth_shares": str(depth)}
        minimum_quote = quantity * limit
        payload = dict(side="SELL", quantity=str(quantity_raw), price=format(limit, "f"), post_only=False,
                       expiration=str(int(time()) + LIMIT_EXPIRATION_SECONDS))
        # Best bid can change between quote and prepare. Check the executable depth again before submitting.
        def check_sell_plan(plan):
            if raw_amount(plan["legs"][0]["quantity"]) != quantity_raw:
                raise ValueError(f"{phase}准备单份额与本轮委托不一致")
            live_bids = read_depth_levels(client, selected["position_id"], "bids")
            if sum(size for price, size in live_bids if price >= limit) < quantity:
                raise ValueError(f"{phase}提交前限价内买盘已不足")

        record(stage="nba_limit_sell_quote", **selection,
               sell_leg=leg, best_bid=str(limit), requested_shares=shares(quantity_raw),
               buy_logical_order_id=buy.fill["logical_order_id"])
        fill = execute_polymarket_order(client, run, uid, signer, maker, record, selection, selected["neg_risk"],
                                        payload, before_submit=check_sell_plan)
        sold = fill_quantity_raw(fill)
        quote = Decimal(fill["filled_quote"])
        record(stage="nba_limit_half_sell_confirmed" if leg == 1 else "nba_limit_remaining_sell_confirmed",
               **selection, **fill,
               side="SELL", source=SOURCE, order_type="LIMIT", funding_source="cash", sell_leg=leg,
               buy_logical_order_id=buy.fill["logical_order_id"], requested_shares=shares(quantity_raw),
               minimum_filled_quote=str(minimum_quote), filled_avg_price=str(quote / Decimal(shares(sold))),
               position_before_sell=shares(before), remaining_bought_shares=shares(remaining - sold))
        if fill["logical_order_id"] in logical_ids or sold != quantity_raw:
            raise ValueError(f"第 {leg} 笔 NBA 限价卖单的成交份额或订单 ID 与委托不一致")
        if not minimum_quote - Decimal("0.000001") <= quote <= quantity:
            raise ValueError(f"第 {leg} 笔 NBA 限价卖单的成交金额 {quote} 与限价及份额不符")
        logical_ids.add(fill["logical_order_id"])
        remaining -= sold
        total_quote += quote
        total_minimum_quote += minimum_quote

    after, _ = wait_for_cash_position(client, uid, selected, baseline + remaining, baseline_raw=baseline)
    record(stage="nba_limit_sells_reconciled", **selected, buy_logical_order_id=buy.fill["logical_order_id"],
           bought_shares=shares(bought_raw), sold_shares=shares(bought_raw - remaining),
           filled_quote=str(total_quote), minimum_filled_quote=str(total_minimum_quote),
           position_before_buy=shares(baseline), position_after_sell=shares(after), remaining_bought_shares=shares(remaining))


def read_withdrawable_cash(client, uid):
    profile = expect_json(request(client, "GET", "/api/v1/user/info", operation="nba_limit_cash"), 200)
    withdrawable = Decimal(profile["max_withdrawable"])
    if str(profile["user_id"]) != uid or not withdrawable.is_finite() or withdrawable < 0:
        raise ValueError("可提现 Cash 回包与当前账号不符")
    return withdrawable, Decimal(shares(raw_amount(profile["polymarket"]["origin_pusd_balance"])))


def wait_limit_order(client, uid, selected, record, logical, order_id, started, *, cancelling=False, partial=False):
    params = {"source": SOURCE, "logical_order_ids": logical, "page": 1, "page_size": 100}
    deadline = started + CONFIRMATION_SECONDS
    status = None
    while (remaining := deadline - monotonic()) > 0:
        try:
            response = list_orders(client, params=params, timeout=min(2, remaining))
        except RuntimeError:
            response = None
        if response is not None and response.status_code < 500 and monotonic() < deadline:
            order = match_polymarket_order(response, logical, order_id, selected, uid, "BUY", "LIMIT")
            if order is not None:
                status = order.get("status")
                if status in TERMINAL:
                    return order
                if not cancelling and status in {"open", "partial"}:
                    if not partial or Decimal(order.get("filled_quantity") or "0") > 0:
                        return order
        sleep(min(1, max(0, deadline - monotonic())))
    phase = "撤单" if cancelling else "部分成交" if partial else "挂单"
    reason = f"{phase} {logical} 在 {CONFIRMATION_SECONDS} 秒内未查到预期状态（最后状态 {status or '未知'}）"
    record(stage="btc_confirmation_timeout", **selected, logical_order_id=logical, order_id=order_id,
           status=status, phase=phase, reason=reason, timeout_seconds=CONFIRMATION_SECONDS)
    raise OrderConfirmationTimeout(reason)


def wait_cash_snapshot(client, uid, expected, *, selected=None):
    deadline = monotonic() + CONFIRMATION_SECONDS
    while True:
        if selected is None:
            cash, _ = read_withdrawable_cash(client, uid)
        else:
            power = expect_json(get_buying_power(client, condition_id=selected["condition_id"],
                                                token_id=selected["position_id"]), 200)
            cash = Decimal(shares(raw_amount(power["balance"])))
        if abs(cash - expected) <= Decimal("0.000001"):
            return cash
        remaining = deadline - monotonic()
        if remaining <= 0:
            label = "可提现 Cash" if selected is None else "市场可用 Cash"
            raise ValueError(f"{label} 未对齐，预期 {expected}，实际 {cash}")
        sleep(min(1, remaining))


def verify_passive_order_and_cancel(client, uid, selected, record, submission, planned_raw, cash_before, estimated_fee):
    logical, order_id, started = submission
    ids = {"logical_order_id": logical, "order_id": order_id}
    opened = wait_limit_order(client, uid, selected, record, *submission)
    if opened["status"] not in {"open", "partial"}:
        raise ValueError(f"post-only 订单 {logical} 未形成挂单，状态 {opened['status']}")

    try:
        filled = Decimal(opened.get("filled_quantity") or "0")
        remaining = Decimal(opened["remaining_quantity"])
        record(stage="nba_limit_open", **selected, **ids, status=opened["status"], filled_quantity=str(filled),
               remaining_quantity=str(remaining), requested_shares=shares(planned_raw), cash_before=str(cash_before),
               requested_post_only=True, post_only=opened.get("post_only"), observed_limit_price=opened.get("price"))
        if filled != 0 or remaining != Decimal(shares(planned_raw)):
            raise ValueError(f"post-only 订单 {logical} 在撤单前已有成交")
        limit_matches = Decimal(opened["price"]) == Decimal(selected["limit_price"])
        reserve = remaining * Decimal(selected["limit_price"]) + estimated_fee
        cash_open = wait_cash_snapshot(client, uid, cash_before - reserve, selected=selected)
        record(stage="nba_limit_cash_reserved", **ids, cash_before=str(cash_before), cash_open=str(cash_open),
               reserved_cash=str(reserve))
    finally:
        record(stage="cancel_attempt", **ids, source=SOURCE)
        expect_json(request(client, "POST", BOOK + "order/cancel", operation="cancel_polymarket_limit_order",
                            json={"order_id": order_id, "source": SOURCE}), 200)
        closed = wait_limit_order(client, uid, selected, record, logical, order_id, monotonic(), cancelling=True)
        record(stage="nba_limit_cancelled", **selected, **ids, status=closed["status"],
               filled_quantity=closed.get("filled_quantity"), remaining_quantity=closed.get("remaining_quantity"))
        if closed["status"] not in CANCELLED or Decimal(closed.get("filled_quantity") or "0") != 0:
            raise ValueError(f"订单 {logical} 未确认零成交撤单，状态 {closed['status']}")
    cash_after = wait_cash_snapshot(client, uid, cash_before, selected=selected)
    record(stage="nba_limit_cash_released", **ids, cash_before=str(cash_before), cash_open=str(cash_open),
           cash_after=str(cash_after), reserved_cash=str(reserve))
    if not limit_matches:
        raise ValueError(f"订单 {logical} 的限价与本轮请求不一致")
