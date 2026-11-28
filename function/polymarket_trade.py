from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from random import sample
from re import fullmatch, search
from time import monotonic, sleep, time

from function.api_client import request
from function.funds import get_buying_power
from function.identity import get_session_user_id
from function.market import get_orderbook_depth, is_open_market_event, is_single_binary_market_event
from function.market import iter_market_events, list_event_markets
from function.orders import list_orders
from function.positions import expect_positions_page, list_positions
from function.response_checks import expect_json
from function.trade_run import price_amount, raw_amount, signing_route, timestamp
from function.trade_state import BOOK


SOURCE = "polymarket"
CONFIRMATION_SECONDS = 60
POSITION_SYNC_SECONDS = 10
BTC_OUTCOMES = ("UP", "DOWN")
SELL_STEP_RAW = 10_000  # Polymarket accepts two decimal places for sell shares.
ASSET_TITLES = {"BTC": r"\b(?:BTC|BITCOIN)\b", "ETH": r"\b(?:ETH|ETHEREUM)\b"}
CLOSED_STATES = {"closed", "settling", "settled", "resolved", "proposed", "disputed", "final", "finalized"}
CLOSED_CODES = {"MARKET_CLOSED", "MARKET_SETTLED", "MARKET_RESOLVED", "RESULT_JUST_PROPOSED"}


class OrderConfirmationTimeout(ValueError):
    pass


class MarketClosed(ValueError):
    def __init__(self, selected, phase, reason, logical_order_id=None):
        super().__init__(f"市场 {selected['market_id']} 在{phase}不可交易（{reason}）")
        self.selected = selected
        self.phase = phase
        self.reason = reason
        self.logical_order_id = logical_order_id


@dataclass(frozen=True)
class MarketBuyFill:
    selection: dict
    neg_risk: bool
    uid: str
    logical_order_id: str
    order_id: str
    quantity_raw: int
    position_before_buy_raw: int


def read_btc_trade_wallet(client):
    uid = get_session_user_id(client)
    profile = expect_json(request(client, "GET", "/api/v1/user/info", operation="trade_wallet_profile"), 200)
    signer = profile.get("wallet_address")
    maker = profile.get("smart_wallet_address")
    if (str(profile.get("user_id")) != uid
            or not all(isinstance(address, str) and fullmatch(r"0x[0-9a-fA-F]{40}", address)
                       for address in (signer, maker))):
        raise ValueError("CI 登录会话与交易钱包资料不符")
    return uid, signer, maker


def select_btc_market(client, run, record, list_params, *, asset="BTC"):
    approved = set(run.market_ids) if asset == "BTC" else set()
    candidates = {}
    for event in iter_market_events(client, list_params):
        if (event.get("type") != SOURCE or not search(ASSET_TITLES[asset], event["title"].upper())
                or not event.get("cadence") or event.get("is_sport") or event.get("is_props")
                or not is_open_market_event(event, time()) or not is_single_binary_market_event(event)):
            continue
        listed = event["markets"][0]
        market_id = str(listed["market"]["id"])
        if not approved or market_id in approved:
            candidates[market_id] = (event, listed)
            if approved == candidates.keys():
                break

    closures = []
    for market_id in sample(list(candidates), len(candidates)):
        event, listed = candidates[market_id]
        selected = dict(asset=asset, end_time=int(event["end_time"]), event_id=event["id"], market_id=market_id,
                        title=event["title"], cadence=event["cadence"])
        children = expect_json(list_event_markets(client, event["id"]), 200)["markets"]
        if len(children) != 1 or str(children[0]["market"]["id"]) != market_id:
            raise ValueError(f"市场 {market_id} 的详情与列表不一致")
        child = children[0]
        market = child["market"]
        options = {option["outcome_label"].upper(): option["position_id"] for option in child["options"]}
        listed_options = {option["outcome_label"].upper(): option["position_id"] for option in listed["options"]}
        condition_id = market["condition_id"]
        if (market["market_type"] != SOURCE or market["outcome_kind"] != "binary" or not condition_id
                or condition_id != listed["market"]["condition_id"] or options != listed_options):
            raise ValueError(f"市场 {market_id} 的条件 ID 或结果选项与列表不一致")
        if reason := market_closed_reason(market):
            closures.append(MarketClosed(selected, "市场预检", reason))
            record(stage="market_ineligible", market_id=market_id, reason=reason)
            continue
        if set(options) != set(BTC_OUTCOMES):
            record(stage="market_ineligible", market_id=market_id, reason="市场缺少 UP/DOWN 选项")
            continue
        outcomes = (run.outcome,) if run.outcome else sample(BTC_OUTCOMES, len(BTC_OUTCOMES))
        for outcome in outcomes:
            token_id = options[outcome]
            eligible_quote = read_eligible_quote(client, token_id, run.price)
            if eligible_quote < Decimal(run.budget_raw).scaleb(-6):
                record(stage="market_ineligible", market_id=market_id, outcome=outcome,
                       reason="预检价格范围内的卖盘不足以覆盖买入预算")
                continue
            if not is_open_market_event(event, time()):
                closures.append(MarketClosed(selected, "市场预检", "已达到市场结束时间"))
                break
            selected.update(outcome=outcome, condition_id=condition_id, position_id=token_id, eligible_quote=str(eligible_quote))
            record(stage="market_selected", **selected)
            return selected, event.get("neg_risk") is True

    if closures and len(closures) == len(candidates):
        raise closures[-1]
    raise ValueError(f"Volume 列表中找到 {len(candidates)} 个 {asset} 二元循环候选，未找到足够的可执行卖盘")


def market_closed_reason(market):
    for field in ("settlement_phase", "settlement_stage", "status"):
        state = str(market.get(field)).lower()
        if state in CLOSED_STATES:
            return f"{field}={state}"
    if market.get("closed") is True or market.get("accepting_orders") is False:
        return "市场已停止接单"
    return None


def ensure_market_trading(client, selected, phase, *, refresh=False, logical_order_id=None):
    if time() >= selected["end_time"]:
        raise MarketClosed(selected, phase, "已达到市场结束时间", logical_order_id)
    if refresh:
        markets = expect_json(list_event_markets(client, selected["event_id"]), 200)["markets"]
        market = next((item["market"] for item in markets if str(item["market"]["id"]) == selected["market_id"]), None)
        if market is None or market.get("condition_id") != selected["condition_id"]:
            raise ValueError("市场状态查询回包与原市场不一致")
        if reason := market_closed_reason(market):
            raise MarketClosed(selected, phase, reason, logical_order_id)


def closed_error_code(body):
    for parent in (body, body.get("metadata")):
        if not isinstance(parent, dict):
            continue
        for field in ("error_code", "code"):
            code = str(parent.get(field)).upper()
            if code in CLOSED_CODES:
                return code
    return None


def reject_closed_response(response, selected, phase, logical_order_id=None):
    if response.status_code not in (400, 409):
        return
    try:
        body = response.json()
    except ValueError:
        return
    if isinstance(body, dict) and (code := closed_error_code(body)):
        raise MarketClosed(selected, phase, code, logical_order_id)


def read_eligible_quote(client, token_id, max_price):
    depth = expect_json(get_orderbook_depth(client, token_id, source=SOURCE), 200)
    if depth.get("position_id") != token_id:
        raise ValueError("盘口深度未返回所选方向")
    quote = Decimal(0)
    for ask in depth.get("asks") or []:
        price = price_amount(ask["price"], allow_one=True)
        size = Decimal(ask["total_qty"])
        if not size.is_finite() or size < 0:
            raise ValueError("盘口深度返回了无效卖盘数量")
        if price <= max_price:
            quote += price * size
    return quote


def check_cash_buy_capacity(client, run, selected, signer, record):
    funds = expect_json(get_buying_power(client, condition_id=selected["condition_id"], token_id=selected["position_id"]), 200)
    balance, capacity = (raw_amount(funds[field]) for field in ("balance", "buying_power"))
    record(stage="buying_power_checked", balance_raw=balance, buying_power_raw=capacity)
    if min(balance, capacity) < run.budget_raw:
        raise ValueError(f"市场 {selected['market_id']} 所选方向的可用 Cash 余额或购买力不足 {shares(run.budget_raw)}")

    grant = expect_json(request(client, "GET", "/api/v1/user/delegated-signer", operation="delegated_signer"), 200)
    if (grant.get("status") != "granted" or not grant.get("signer_id")
            or str(grant.get("wallet_address", "")).lower() != signer.lower()):
        raise ValueError("当前测试钱包的代签授权未就绪")
    return balance


def execute_btc_buy(client, run, uid, signer, maker, record, list_params, *, asset="BTC"):
    selected, neg_risk = select_btc_market(client, run, record, list_params, asset=asset)
    check_cash_buy_capacity(client, run, selected, signer, record)

    before_buy, _ = read_cash_position(client, uid, selected)
    record(stage="cash_baseline", **selected, position_raw=before_buy)
    payload = {"side": "BUY", "total_budget": str(run.budget_raw)}
    fill = execute_polymarket_order(client, run, uid, signer, maker, record, selected, neg_risk, payload)
    bought = fill_quantity_raw(fill)
    record(stage="btc_fill_confirmed", **selected, **fill, source=SOURCE, side="BUY", order_type="MARKET",
           funding_source=run.funding, budget=shares(run.budget_raw), position_before_buy_raw=before_buy)
    return MarketBuyFill(selected, neg_risk, uid, fill["logical_order_id"], fill["order_id"], bought, before_buy)


def execute_polymarket_order(client, run, uid, signer, maker, record, selected, neg_risk, payload, *, before_submit=None):
    logical, order_id, started = submit_polymarket_order(client, run, signer, maker, record, selected, neg_risk,
                                                         payload, before_submit=before_submit)
    return confirm_polymarket_order(client, record, logical, order_id, selected, uid, payload["side"], run.order_type, started)


def submit_polymarket_order(client, run, signer, maker, record, selected, neg_risk, payload, *, before_submit=None):
    run.check_window()
    side, order_type = payload["side"], run.order_type
    ensure_market_trading(client, selected, f"{side} 下单准备")
    payload = dict(payload, condition_id=selected["condition_id"], token_id=selected["position_id"],
                   venue=SOURCE, order_type=order_type, use_credit=False, is_neg_risk=neg_risk,
                   signer_address=signer, maker_address=maker, signature_type=2)
    record(stage="prepare_attempt", side=side)
    response = request(client, "POST", BOOK + "orders/prepare", operation="prepare_polymarket_order", json=payload)
    reject_closed_response(response, selected, f"{side} 下单准备")
    if response.status_code in (400, 409):
        ensure_market_trading(client, selected, f"{side} 下单准备", refresh=True)
    plan = expect_json(response, 200)
    if (plan.get("venue") != SOURCE or signing_route(plan) != "server"
            or not plan.get("plan_id") or not plan.get("logical_order_id")):
        raise ValueError(f"{side} {order_type} 计划应为 Polymarket 单腿 Cash 代签，并返回计划与逻辑订单 ID")

    plan_id, logical = plan["plan_id"], plan["logical_order_id"]
    record(stage="prepared", plan_id=plan_id, logical_order_id=logical, side=side)
    if before_submit is not None:
        before_submit(plan)
    run.check_window()
    ensure_market_trading(client, selected, f"{side} 提交前", logical_order_id=logical)
    if timestamp(plan["plan_expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError(f"{side} {order_type} 计划已过期")
    record(stage="submit_attempt", plan_id=plan_id, logical_order_id=logical, side=side)
    response = request(client, "POST", BOOK + "orders/submit", operation="submit_polymarket_order",
                       json={"plan_id": plan_id, "cash_signature": ""}, timeout=30)
    confirmation_started = monotonic()
    reject_closed_response(response, selected, f"{side} 提交被拒绝", logical)
    submitted = expect_json(response, 200)
    legs = submitted.get("results", [])
    if submitted.get("logical_order_id") != logical or submitted.get("replayed") is not False or len(legs) != 1:
        raise ValueError(f"订单 {logical} 的提交回执与本轮计划不一致")
    leg = legs[0]
    if leg.get("success") is False and not leg.get("order_id") and (code := closed_error_code(leg)):
        raise MarketClosed(selected, f"{side} 提交被拒绝", code, logical)
    if leg.get("funding_source") != "cash" or leg.get("success") is not True or not leg.get("order_id"):
        raise ValueError(f"订单 {logical} 未成功提交 Cash 腿，业务码 {leg.get('error_code') or '空'}")
    record(stage="submitted", logical_order_id=logical, order_id=leg["order_id"], status=leg.get("status"), side=side)
    return logical, leg["order_id"], confirmation_started


def match_polymarket_order(response, logical, order_id, selected, uid, side, order_type):
    matches = expect_json(response, 200)["items"]
    if not matches:
        return None
    row = matches[0]
    order = row["order"]
    market = row.get("market") or {}
    option = row.get("option") or {}
    if (len(matches) != 1 or row.get("source") != SOURCE or order.get("logical_order_id") != logical
            or order.get("order_id") != order_id or str(order.get("uid", uid)) != str(uid)
            or str(market.get("id")) != selected["market_id"] or order.get("side") != side.lower()
            or str(order.get("order_type", order_type)).upper() != order_type
            or order.get("condition_id", market.get("condition_id")) != selected["condition_id"]
            or order.get("position_id", option.get("position_id")) != selected["position_id"]):
        raise ValueError(f"订单 {logical} 的确认回包与本轮 {side} 参数不一致")
    return order


def confirm_polymarket_order(client, record, logical, order_id, selected, uid, side, order_type, started):
    params = {"source": SOURCE, "logical_order_ids": logical, "page": 1, "page_size": 100}
    deadline = started + CONFIRMATION_SECONDS
    status, onchain = None, None
    while (remaining := deadline - monotonic()) > 0:
        try:
            response = list_orders(client, params=params, timeout=min(remaining, 2))
        except RuntimeError:
            response = None
        if response is not None and monotonic() < deadline and response.status_code < 500:
            order = match_polymarket_order(response, logical, order_id, selected, uid, side, order_type)
            if order is not None:
                status, onchain = order.get("status"), order.get("onchain_status")
                if onchain == "failed" or status in {"cancelled", "canceled", "rejected", "expired", "failed"}:
                    if status in {"cancelled", "canceled", "rejected", "expired", "failed"} and fullmatch(
                        r"0+(?:\.0+)?", str(order.get("filled_quantity")),
                    ):
                        ensure_market_trading(client, selected, f"{side} 订单已终止且未成交", refresh=True, logical_order_id=logical)
                    raise ValueError(f"订单 {logical} 成交失败（订单 {status}，链上 {onchain}）")
                if status == "filled" and onchain in {"confirmed", "none"}:
                    try:
                        filled, quote, unfilled = (Decimal(order[key]) for key in
                                                   ("filled_quantity", "filled_quote", "remaining_quantity"))
                    except (KeyError, TypeError, InvalidOperation):
                        raise ValueError(f"订单 {logical} 成交金额或份额格式不符") from None
                    if not (filled.is_finite() and filled > 0 and quote.is_finite() and quote > 0 and unfilled == 0):
                        raise ValueError(f"订单 {logical} 全成状态与成交量或剩余量不符")
                    elapsed = monotonic() - started
                    if elapsed < CONFIRMATION_SECONDS:
                        return dict(logical_order_id=logical, order_id=order_id, status=status, onchain_status=onchain,
                                    filled_quantity=str(filled), filled_quote=str(quote), remaining_quantity=str(unfilled),
                                    elapsed_seconds=round(elapsed, 3))
        sleep(min(1, max(0, deadline - monotonic())))
    reason = (f"订单 {logical} 在 {CONFIRMATION_SECONDS} 秒内未确认成交"
              f"（订单 {status or '未知'}，链上 {onchain or '未知'}）")
    record(stage="btc_confirmation_timeout", **selected, logical_order_id=logical, order_id=order_id,
           status=status, onchain_status=onchain, side=side, elapsed_seconds=round(monotonic() - started, 3),
           timeout_seconds=CONFIRMATION_SECONDS, reason=reason)
    raise OrderConfirmationTimeout(reason)


def shares(raw):
    return str(Decimal(raw).scaleb(-6))


def fill_quantity_raw(fill):
    raw = Decimal(fill["filled_quantity"]).scaleb(6)
    if raw != raw.to_integral_value():
        raise ValueError("成交份额超过 6 位精度")
    return int(raw)


def read_cash_position(client, uid, selected):
    filters = dict(uid=uid, source=SOURCE, state="open", condition_id=selected["condition_id"],
                   position_id=selected["position_id"], ownership_type="user", funding_account_type="user",
                   page=1, page_size=100)
    page = expect_positions_page(list_positions(client, params=filters), filters)
    if page["pagination"]["total"] > filters["page_size"]:
        raise ValueError("本轮选项的 Cash 持仓查询未返回完整数据")
    quantity = frozen = 0
    for item in page["items"]:
        position = item["position"]
        if (item["source"] != SOURCE or position["condition_id"] != selected["condition_id"]
                or position["position_id"] != selected["position_id"] or position.get("ownership_type") != "user"
                or position.get("funding_account_type") != "user"):
            continue
        quantity += raw_amount(position["quantity"])
        frozen += raw_amount(position["frozen_amount"])
    if frozen > quantity:
        raise ValueError("Cash 持仓冻结份额超过持仓总量")
    return quantity, quantity - frozen


def wait_for_cash_position(client, uid, selected, expected_raw, *, phase=None, baseline_raw=None, tolerance_raw=0):
    deadline = monotonic() + POSITION_SYNC_SECONDS
    expected_totals = {expected_raw}
    # The open positions view can omit the untradeable remainder after both sells.
    if baseline_raw is not None and 0 < expected_raw - baseline_raw < SELL_STEP_RAW:
        expected_totals.add(baseline_raw)
    while True:
        total, available = read_cash_position(client, uid, selected)
        if total in expected_totals or abs(total - expected_raw) <= tolerance_raw:
            return total, available
        delay = deadline - monotonic()
        if delay <= 0:
            if phase:
                ensure_market_trading(client, selected, phase, refresh=True)
            raise ValueError(f"市场 {selected['market_id']} 的持仓未对齐，期望 {shares(expected_raw)}，实际 {shares(total)}")
        sleep(min(1, delay))


def execute_btc_sell(client, run, uid, signer, maker, record, buy):
    if uid != buy.uid:
        raise ValueError("买卖用例的登录账号不一致")
    selected = buy.selection
    record(stage="market_selected", **selected)
    baseline = buy.position_before_buy_raw
    remaining = buy.quantity_raw
    for leg, stage in enumerate(("btc_half_sell_confirmed", "btc_remaining_sell_confirmed"), start=1):
        phase = f"第 {leg} 笔卖出前"
        ensure_market_trading(client, selected, phase, refresh=True)
        before, available = wait_for_cash_position(client, uid, selected, baseline + remaining, phase=phase)
        target = remaining // 2 if leg == 1 else before - baseline
        quantity = target // SELL_STEP_RAW * SELL_STEP_RAW
        if quantity <= 0 or quantity > available:
            raise ValueError(f"第 {leg} 笔 {selected['asset']} 卖单缺少足够的可卖份额")
        token_id = selected["position_id"]
        depth = expect_json(get_orderbook_depth(client, token_id, source=SOURCE), 200)
        bids = Decimal(0)
        for bid in depth.get("bids") or []:
            price_amount(bid["price"], allow_one=True)
            size = Decimal(bid["total_qty"])
            if not size.is_finite() or size < 0:
                raise ValueError(f"第 {leg} 笔 {selected['asset']} 卖单的买盘数量无效")
            bids += size
        if depth.get("position_id") != token_id:
            raise ValueError("盘口深度未返回所选方向")
        if bids < Decimal(shares(quantity)):
            ensure_market_trading(client, selected, phase, refresh=True)
            raise ValueError(f"第 {leg} 笔 {selected['asset']} 卖单的买盘不足")
        payload = {"side": "SELL", "quantity": str(quantity)}
        fill = execute_polymarket_order(client, run, uid, signer, maker, record, selected, buy.neg_risk, payload)
        sold = fill_quantity_raw(fill)
        if sold != quantity:
            raise ValueError(f"第 {leg} 笔卖单的实成交份额与委托不一致")
        remaining -= sold
        record(stage=stage, **selected, **fill, side="SELL", source=SOURCE, order_type="MARKET", funding_source="cash",
               buy_logical_order_id=buy.logical_order_id, bought_shares=shares(buy.quantity_raw), requested_shares=shares(quantity),
               position_before_sell=shares(before), remaining_bought_shares=shares(remaining))
    if not 0 <= remaining < SELL_STEP_RAW:
        raise ValueError("第二笔卖单结束后仍有可卖的本轮买入份额")
    after, _ = wait_for_cash_position(client, uid, selected, baseline + remaining, baseline_raw=baseline)
    record(stage="btc_sell_position_checked", **selected, buy_logical_order_id=buy.logical_order_id,
           bought_shares=shares(buy.quantity_raw), sold_shares=shares(buy.quantity_raw - remaining),
           position_before_buy=shares(baseline), position_after_sell=shares(after), remaining_bought_shares=shares(remaining))


def execute_market_cycle(client, run, uid, signer, maker, record, list_params, buy=None):
    try:
        if buy is None:
            return execute_btc_buy(client, run, uid, signer, maker, record, list_params)
        return execute_btc_sell(client, run, uid, signer, maker, record, buy)
    except MarketClosed as error:
        if error.selected["asset"] != "BTC":
            raise
        record(stage="market_fallback", **error.selected, fallback_asset="ETH", phase=error.phase,
               reason=error.reason, logical_order_id=error.logical_order_id,
               buy_logical_order_id=buy.logical_order_id if buy else None)
    replacement = execute_btc_buy(client, run, uid, signer, maker, record, list_params, asset="ETH")
    if buy is None:
        return replacement
    return execute_btc_sell(client, run, uid, signer, maker, record, replacement)
