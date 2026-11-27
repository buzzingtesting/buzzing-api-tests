from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from random import sample
from time import time

import pytest

from function.funds import get_buying_power
from function.market import get_orderbook_depth, get_orderbook_event, is_open_market_event
from function.market import is_single_binary_market_event, iter_market_events, list_market_events
from function.reporting import attach_market_sample
from function.response_checks import expect_json
from function.trade_run import BUY_BUDGET_RAW, LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, OWN_PARTIAL_EXTRA_RAW
from function.trade_run import check_depth, price_amount, raw_amount
from function.trade_state import BOOK, CashSnapshot, order_query


def expected_limit_price(quote, *, side):
    """Return the expected initial price for an empty ticket, or None without a quote."""
    if side not in {"BUY", "SELL"}:
        raise ValueError("限价单方向应为 BUY 或 SELL")
    if quote is None:
        return None
    percentage = (price_amount(quote, allow_one=True) * 100).to_integral_value(rounding=ROUND_HALF_UP)
    adjustment = 1 if side == "SELL" else -1
    adjusted = Decimal(min(99, max(1, percentage + adjustment))) / 100
    return f"{adjusted:.2f}"


def select_trade_market(client, api, cash, session, run, uid, maker, record, list_params):
    """Randomly choose an eligible market from this run's approved Preview pool."""
    approved = set(run.market_ids)
    candidates = {}
    now = time()
    for event in iter_market_events(client, list_params):
        if (event.get("type") != "orderbook" or not is_open_market_event(event, now)
                or not is_single_binary_market_event(event)):
            continue
        listed = event["markets"][0]
        market_id = listed["market"]["id"]
        if market_id in approved:
            candidates[market_id] = (event, listed)
            if candidates.keys() == approved:
                break
    for market_id in sample(list(candidates), len(candidates)):
        event, listed = candidates[market_id]
        market = read_trade_market(client, event, listed)
        try:
            read_trade_depth(api, cash, session, market, run)
            if run.side == "SELL":
                snapshot = CashSnapshot.read(api, cash, session, order_query(market), uid, maker)
                if snapshot.position - snapshot.position_frozen < run.quantity_raw:
                    raise ValueError("本轮账号的可卖份额不足")
        except ValueError as error:
            record(stage="market_ineligible", market_id=market_id, reason=str(error))
            continue
        attach_market_sample(event, market_id=market_id, position_id=market["token_id"])
        record(stage="market_selected", event_id=event["id"], market_id=market_id,
               condition_id=market["condition_id"], position_id=market["token_id"])
        return market
    raise ValueError(f"本轮列表中有 {len(candidates)} 个获准候选，没有满足盘口或持仓条件的市场")


def read_trade_event(client, event):
    detail = expect_json(get_orderbook_event(client, event["id"]), 200)
    if str(detail["event"]["id"]) != str(event["id"]) or detail["event"]["title"] != event["title"]:
        raise ValueError("交易盘口归属与本轮列表事件不一致")
    return detail


def checked_trade_options(event, listed, market):
    listed_market = listed["market"]
    options = {option["outcome_label"].upper(): option["position_id"] for option in market["options"]}
    listed_options = {option["outcome_label"].upper(): option["position_id"] for option in listed["options"]}
    condition_id = market["condition_id"]
    if (str(market["id"]) != str(listed_market["id"]) or str(market["event_id"]) != str(event["id"])
            or market["market_type"] != "orderbook" or market["outcome_kind"] != "binary"
            or not isinstance(condition_id, str) or not condition_id or condition_id != listed_market["condition_id"]
            or set(options) != {"YES", "NO"} or options != listed_options
            or any(not isinstance(token, str) or not token.isascii() or not token.isdecimal()
                   for token in options.values())):
        raise ValueError("交易盘口、条件 ID 或二元选项与本轮列表不一致")
    return options


def read_trade_market(client, event, listed):
    detail = read_trade_event(client, event)
    if len(detail["markets"]) != 1:
        raise ValueError("自营独立二元事件应只有一个盘口")
    market = detail["markets"][0]
    options = checked_trade_options(event, listed, market)
    return {"condition_id": market["condition_id"], "token_id": options["NO"]}


def read_trade_depth(api, cash, session, market, run):
    token = market["token_id"]
    depth = cash.data(api("GET", BOOK + "depth", 200, token=session,
                          params={"source": "orderbook", "position_id": token}))
    quantity = check_depth(depth, token, run.price, run.budget_raw if run.side == "BUY" else BUY_BUDGET_RAW,
                           scenario=run.scenario, quantity_raw=run.quantity_raw)
    asks = [price_amount(row["price"], allow_one=True) for row in depth.get("asks", [])]
    bids = [price_amount(row["price"], allow_one=True) for row in depth.get("bids", [])]
    return quantity, str(min(asks)) if asks else None, str(max(bids)) if bids else None



@dataclass(frozen=True)
class PartialCancelQuote:
    price: Decimal
    quantity_raw: int

    @property
    def quote_raw(self):
        return int((self.price * self.quantity_raw).to_integral_value(rounding=ROUND_CEILING))

    @property
    def budget_raw(self):
        return self.quote_raw + 2_000_000


def read_partial_cancel_quote(client, token):
    depth = expect_json(get_orderbook_depth(client, token, source="orderbook"), 200)
    if depth.get("position_id") != token:
        raise ValueError("卖盘深度与所选方向不符")
    levels = {}
    for ask in depth.get("asks") or []:
        price = price_amount(ask["price"], allow_one=True)
        size = raw_amount(ask["total_qty"])
        if price < 1 and size:
            levels[price] = levels.get(price, 0) + size
    if not levels:
        return None
    price = min(levels)
    return PartialCancelQuote(price, levels[price])


def select_partial_cancel_market(client, list_params, record, *, max_budget_raw=None):
    for event in iter_market_events(client, list_params):
        if event.get("type") != "orderbook" or not is_open_market_event(event, time()):
            continue
        listed_markets = {str(item["market"]["id"]): item for item in event.get("markets") or []
                          if item["market"].get("outcome_kind") == "binary"}
        if not listed_markets:
            continue
        detail = read_trade_event(client, event)
        if detail["event"].get("neg_risk") is not False:
            continue
        for market in detail["markets"]:
            market_id = str(market["id"])
            if market_id not in listed_markets or market.get("accepting_orders") is not True or market.get("closed") is True:
                continue
            checked_trade_options(event, listed_markets[market_id], market)
            for option in market["options"]:
                token = option["position_id"]
                quote = read_partial_cancel_quote(client, token)
                if quote is None:
                    continue
                evidence = dict(event_id=event["id"], market_id=market_id, title=event["title"],
                                condition_id=market["condition_id"], position_id=token, outcome=option["outcome_label"],
                                last_trade_price=option.get("last_trade_price"), limit_price=str(quote.price),
                                eligible_quantity_raw=quote.quantity_raw, eligible_quote_raw=quote.quote_raw,
                                budget_raw=quote.budget_raw, max_budget_raw=max_budget_raw)
                if max_budget_raw is not None and quote.budget_raw > max_budget_raw:
                    record(stage="partial_quote_ineligible", **evidence, reason="卖一金额加 2 U 超过单笔上限")
                    continue
                record(stage="partial_quote_selected", **evidence)
                return {"condition_id": market["condition_id"], "token_id": token}, quote
    raise ValueError("实时筛选列表中没有符合条件的活跃自营卖盘")


def volume_limit_asks(client, token):
    depth = expect_json(get_orderbook_depth(client, token, source="orderbook"), 200)
    if depth.get("position_id") != token:
        raise ValueError("自营盘口深度与所选方向不符")
    levels = {}
    for ask in depth.get("asks") or []:
        price = price_amount(ask["price"], allow_one=True)
        size = raw_amount(ask["total_qty"])
        if size:
            levels[price] = levels.get(price, 0) + size
    return dict(sorted(levels.items()))


def limit_ask_quote(levels, limit):
    quote = sum((price * size for price, size in levels.items() if price <= limit), Decimal(0))
    return int(quote.to_integral_value(rounding=ROUND_CEILING))


def select_cash_plan_market(client, list_params, budget_raw):
    events = expect_json(list_market_events(client, params=list_params), 200)["items"]
    for event in events:
        if (event.get("type") != "orderbook" or not is_open_market_event(event, time() + 90)
                or not is_single_binary_market_event(event)):
            continue
        detail = read_trade_event(client, event)
        if detail["event"].get("neg_risk") is not False or len(detail["markets"]) != 1:
            continue
        market = detail["markets"][0]
        if market.get("accepting_orders") is not True or market.get("closed") is not False:
            continue
        options = checked_trade_options(event, event["markets"][0], market)
        for outcome, token in options.items():
            levels = volume_limit_asks(client, token)
            price = next(iter(levels), None)
            if price is None or price >= 1 or price * levels[price] < budget_raw:
                continue
            condition = market["condition_id"]
            power = expect_json(get_buying_power(client, condition_id=condition, token_id=token), 200)
            if min(raw_amount(power[key]) for key in ("balance", "buying_power")) < budget_raw:
                continue
            return dict(event_id=str(event["id"]), market_id=str(market["id"]), title=event["title"],
                        condition_id=condition, position_id=token, outcome=outcome, neg_risk=False,
                        limit_price=str(price))
    pytest.skip("Volume 列表中没有满足盘口和 Cash 预算的活跃自营二元市场")


def select_volume_limit_market(client, run, list_params, record):
    events = expect_json(list_market_events(client, params=list_params), 200)["items"]
    candidates = [event for event in events if event["type"] == "orderbook" and is_open_market_event(event, time())]
    for event in sample(candidates, len(candidates)):
        detail = read_trade_event(client, event)
        if detail["event"].get("neg_risk") is not False or not detail["markets"]:
            continue
        market = max(detail["markets"], key=lambda item: raw_amount(item.get("volume_clob") or "0"))
        market_id = str(market["id"])
        listed = next((row for row in event["markets"] if str(row["market"]["id"]) == market_id), None)
        if (listed is None or market.get("outcome_kind") != "binary"
                or market.get("accepting_orders") is not True or market.get("closed") is True):
            continue
        checked_trade_options(event, listed, market)
        for option in sample(market["options"], len(market["options"])):
            last_trade_price = option.get("last_trade_price")
            try:
                limit = price_amount(last_trade_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except ValueError:
                continue
            if not 0 < limit < 1:
                continue
            token = option["position_id"]
            levels = volume_limit_asks(client, token)
            best_ask = next(iter(levels), None)
            if run.scenario == LIMIT_CANCEL and best_ask is not None:
                limit = min(limit, best_ask - Decimal("0.01"))
            if limit <= 0:
                continue
            eligible_quantity = sum(size for price, size in levels.items() if price <= limit)
            book_quote = limit_ask_quote(levels, limit)
            budget = run.budget_raw
            if run.scenario == LIMIT_BUY and eligible_quantity * limit < budget:
                continue
            if run.scenario == LIMIT_PARTIAL_CANCEL:
                budget = book_quote + OWN_PARTIAL_EXTRA_RAW
                if not book_quote or budget > run.partial_budget_cap_raw or eligible_quantity * limit >= budget:
                    continue
            power = expect_json(get_buying_power(client, condition_id=market["condition_id"], token_id=token), 200)
            if min(raw_amount(power[key]) for key in ("balance", "buying_power")) < budget:
                continue
            evidence = dict(event_id=event["id"], market_id=market_id, title=event["title"],
                            condition_id=market["condition_id"], position_id=token, outcome=option["outcome_label"],
                            volume_clob=market["volume_clob"], last_trade_price=last_trade_price,
                            best_ask=str(best_ask) if best_ask is not None else None,
                            limit_price=str(limit), eligible_quantity_raw=eligible_quantity,
                            eligible_quote_raw=book_quote, budget_raw=budget)
            record(stage="market_selected", **evidence)
            return {"condition_id": market["condition_id"], "token_id": token}, replace(
                run, price=limit, selected_budget_raw=budget,
            )
    pytest.skip(f"本轮 Volume 列表的 {len(candidates)} 个活跃自营事件中没有满足盘口和预算的候选")


def expected_volume_limit_fills(client, market, run, quantity, record):
    levels = volume_limit_asks(client, market["token_id"])
    quote = limit_ask_quote(levels, run.price)
    expected = {}
    remaining = quantity
    for price, available in levels.items():
        if price > run.price or remaining == 0:
            break
        consumed = min(available, remaining)
        expected[str(price.normalize())] = consumed
        remaining -= consumed
    filled = quantity - remaining
    record(stage="volume_limit_depth_rechecked", limit_price=str(run.price), ordered_raw=quantity,
           eligible_quote_raw=quote, expected_fills=expected, remaining_raw=remaining)
    valid = {
        LIMIT_BUY: filled == quantity,
        LIMIT_CANCEL: filled == 0,
        LIMIT_PARTIAL_CANCEL: 0 < filled < quantity and quote + OWN_PARTIAL_EXTRA_RAW == run.budget_raw,
    }
    if not valid[run.scenario]:
        pytest.skip("提交前盘口已变动或准备单份额不满足本轮成交预期")
    return expected


def verify_volume_limit_fills(fills, expected, record):
    actual = {}
    for row in fills:
        price = str(price_amount(row["price"]).normalize())
        actual[price] = actual.get(price, 0) + raw_amount(row["quantity"])
    record(stage="volume_limit_fills_checked", expected_fills=expected, actual_fills=actual)
    if actual != expected:
        raise ValueError("本轮订单的实际吃单价格或份额与提交前盘口快照不符")
