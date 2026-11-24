from dataclasses import dataclass
from decimal import ROUND_DOWN
from uuid import UUID

from function.trade_run import price_amount, raw_amount


BOOK = "/api/v1/orderbook/"
OPEN_STATES = {"init", "pending", "partial", "open"}
TERMINAL_STATES = {"filled", "cancelled", "expired", "rejected"}


def order_query(market):
    return {"source": "orderbook", "condition_id": market["condition_id"], "position_id": market["token_id"]}


def own_order(api, cash, session, query, logical, uid, run, *, order_hash=None):
    params = {**query, "page_size": 20, "logical_order_ids": logical}
    rows = cash.list_rows(api("GET", BOOK + "orders", 200, token=session, params=params))
    if not rows:
        return None
    order = rows[0].get("order")
    if (len(rows) != 1 or not isinstance(order, dict) or str(order.get("uid")) != str(uid)
            or order.get("logical_order_id") != logical or order.get("condition_id") != query["condition_id"]
            or order.get("position_id") != query["position_id"] or str(order.get("side")).upper() != run.side
            or str(order.get("order_type")).upper() != run.order_type
            or str(order.get("leg_count") or "0") not in {"0", "1"}
            or (order_hash and str(order.get("order_hash")).lower() != order_hash)):
        raise ValueError("订单查询返回的归属、逻辑 ID 或下单参数与本轮不符")
    if str(UUID(order["order_id"])) != order["order_id"]:
        raise ValueError("自有订单 ID 必须是规范 UUID")
    return order


def profile_order(api, cash, session, view, order_id):
    # Profile views use their own offset cursor and ignore exact order ID/hash filters.
    params = {"source": "orderbook", "view": view, "page_size": 100}
    cursors = set()
    while True:
        reply = cash.data(api("GET", BOOK + "orders", 200, token=session, params=params))
        if reply.get("truncated"):
            raise ValueError("Profile 订单视图数据被截断")
        for row in cash.list_rows(reply):
            order = row.get("order")
            if isinstance(order, dict) and order.get("order_id") == order_id:
                return order
        pagination = reply["pagination"]
        if not pagination.get("has_more"):
            return None
        cursor = pagination.get("next_cursor")
        if not cursor or cursor in cursors:
            raise ValueError("Profile 订单视图未返回可继续翻页的游标")
        cursors.add(cursor)
        params["next_cursor"] = cursor


@dataclass(frozen=True)
class OrderAmounts:
    quantity: int
    filled: int
    remaining: int
    quote: int

    @classmethod
    def read(cls, order, expected_quantity):
        amounts = cls(*(raw_amount(order[field]) for field in
                        ("quantity", "filled_quantity", "remaining_quantity", "filled_quote")))
        if (amounts.quantity != expected_quantity or amounts.filled + amounts.remaining != amounts.quantity
                or (order["status"] == "filled" and amounts.remaining != 0)):
            raise ValueError("订单原始量、成交量和剩余量无法对平")
        return amounts


@dataclass(frozen=True)
class CashSnapshot:
    cash: int
    frozen: int
    position: int
    position_frozen: int
    approvals: bool

    @classmethod
    def read(cls, api, cash, session, query, uid, maker):
        profile = cash.trading_profile(api, session, uid, maker)
        params = {**query, "ownership_type": "user", "funding_account_type": "user"}
        rows = cash.list_rows(api("GET", BOOK + "positions", 200, token=session, params=params))
        position = rows[0].get("position") if rows else {"quantity": "0", "frozen_amount": "0"}
        if len(rows) > 1 or (rows and (not isinstance(position, dict)
                                      or position.get("position_id") != query["position_id"])):
            raise ValueError("Cash 持仓快照与本轮方向或资金账户不符")
        return cls(raw_amount(profile["usdb_balance"]), raw_amount(profile["usdb_frozen_amount"]),
                   raw_amount(position["quantity"]), raw_amount(position["frozen_amount"]),
                   profile.get("is_approve_orderbook_all"))

    def evidence(self):
        return {"cash_raw": self.cash, "cash_frozen_raw": self.frozen, "position_raw": self.position,
                "position_frozen_raw": self.position_frozen}


def order_fills(api, cash, session, query, order, run, uid):
    params = {"condition_id": query["condition_id"], "position_ids": query["position_id"], "limit": 100}
    rows = cash.list_rows(api("GET", BOOK + "trades", 200, token=session, params=params))
    keys = set()
    fills = []
    for row in rows:
        if str(row.get("order_hash")).lower() not in {order["order_id"], order["order_hash"].lower()}:
            continue
        key = row.get("fill_key")
        if (str(row.get("uid")) != str(uid) or row.get("condition_id") != query["condition_id"]
                or row.get("position_id") != query["position_id"] or str(row.get("side")).upper() != run.side
                or not key or key in keys):
            raise ValueError("成交记录归属错误或重复")
        keys.add(key)
        price = price_amount(row["price"])
        if (run.side == "BUY" and price > run.price) or (run.side == "SELL" and price < run.price):
            raise ValueError("成交价格超出本轮断言范围")
        quantity, quote = raw_amount(row["quantity"]), raw_amount(row["quote_amount"])
        if quantity <= 0 or not quote or abs(quote - quantity * price) >= 1:
            raise ValueError("成交额与成交数量及价格不符")
        fills.append(row)
    return fills


def raw_at_price(quantity_raw, price):
    return int((quantity_raw * price).to_integral_value(rounding=ROUND_DOWN))
