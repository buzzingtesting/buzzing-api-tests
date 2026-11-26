from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from random import sample
from time import time

from function.market import get_orderbook_depth, get_orderbook_event, is_open_market_event
from function.market import is_single_binary_market_event, iter_market_events
from function.reporting import attach_market_sample
from function.response_checks import expect_json
from function.trade_run import BUY_BUDGET_RAW, check_depth, price_amount, raw_amount
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
