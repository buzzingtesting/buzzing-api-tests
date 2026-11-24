import fcntl
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID


LIMIT_BUY = "limit_buy"
LIMIT_CANCEL = "limit_cancel"
MARKET_SELL = "market_sell"
MARKET_ID = "4828026"
BUY_BUDGET_RAW = 2_000_000
TRADE_SHAPES = {LIMIT_BUY: ("BUY", "LIMIT"), LIMIT_CANCEL: ("BUY", "LIMIT"), MARKET_SELL: ("SELL", "MARKET")}


def raw_amount(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("期望六位精度的 raw 整数字符串")
    return int(value)


def price_amount(value, *, allow_one=False):
    try:
        price = Decimal(value) if isinstance(value, str) else Decimal("NaN")
    except InvalidOperation:
        raise ValueError("期望 USDB 每份的十进制价格") from None
    if not price.is_finite() or not 0 < price <= 1 or (price == 1 and not allow_one):
        raise ValueError("期望 USDB 每份的十进制价格")
    return price


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("交易窗口必须包含时区")
    return result


@dataclass(frozen=True)
class TradeRun:
    run_id: str
    account: str
    funding: str
    batch_id: str
    start: datetime
    end: datetime
    scenario: str = LIMIT_BUY
    price: Decimal = Decimal("0.88")
    quantity_raw: int = 0

    @property
    def side(self):
        return TRADE_SHAPES[self.scenario][0]

    @property
    def order_type(self):
        return TRADE_SHAPES[self.scenario][1]

    @classmethod
    def from_file(cls, path, *, check_window=True):
        spec = json.loads(path.read_text())
        scenario = spec.get("scenario", LIMIT_BUY)
        if (spec["environment"], spec["venue"], spec["market_id"], spec["outcome"]) != (
                "preview", "buzzing", MARKET_ID, "NO"):
            raise ValueError("本入口只支持已核对的 Preview 自有盘口 NO")
        if TRADE_SHAPES.get(scenario) != (spec["side"], spec["order_type"]):
            raise ValueError("订单方向和类型必须与本轮场景一致")
        run_id = str(UUID(spec["run_id"]))
        account, funding, batch = spec["account"], spec["funding"], spec.get("batch_id", "")
        if account not in {"A", "A2", "B2", "B3"} or funding not in {"cash", "credit"}:
            raise ValueError("请选择交易专用账号和已支持的资金路径")
        if (funding == "credit" and not re.fullmatch("[1-9][0-9]*", batch)) or (funding == "cash" and batch):
            raise ValueError("纯 Credit 必须指定本轮批准的批次；Cash 不使用批次")
        budget = raw_amount(spec["budget_raw"])
        if scenario == MARKET_SELL:
            price = price_amount(spec["min_price"])
            quantity = raw_amount(spec["quantity_raw"])
            if funding != "cash" or budget or spec.get("limit") or not 10_000 <= quantity <= 2_000_000:
                raise ValueError("市价卖出仅允许专用 Cash 仓位，数量为 0.01 至 2 份，无买入预算")
        else:
            price, quantity = price_amount(spec["limit"]), 0
            if budget != BUY_BUDGET_RAW or "quantity_raw" in spec or "min_price" in spec:
                raise ValueError("限价买入使用 2 测试 USDB 含费预算")
            if (scenario == LIMIT_BUY and price != Decimal("0.88")) or (scenario == LIMIT_CANCEL and funding != "cash"):
                raise ValueError("代表性限价成交使用 0.88；主动挂单撤单使用 Cash")
        result = cls(run_id, account, funding, batch, timestamp(spec["start"]), timestamp(spec["end"]),
                     scenario, price, quantity)
        if result.start >= result.end:
            raise ValueError("交易窗口的起点必须早于终点")
        if check_window:
            result.check_window()
        return result

    def check_window(self):
        if not self.start <= datetime.now(timezone.utc) < self.end:
            raise ValueError("当前时间不在本轮获批窗口内")


@contextmanager
def trade_journal(root, run):
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{MARKET_ID}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root / f"{run.run_id}.jsonl").open("x", encoding="utf-8") as stream:
            def record(**fields):
                row = dict(fields, run_id=run.run_id, account=run.account,
                           utc=datetime.now(timezone.utc).isoformat())
                print(json.dumps(row, ensure_ascii=False), file=stream, flush=True)

            record(stage="start", funding=run.funding, market=MARKET_ID, batch_id=run.batch_id,
                   scenario=run.scenario, side=run.side, order_type=run.order_type, price=str(run.price),
                   quantity_raw=run.quantity_raw, budget_raw=BUY_BUDGET_RAW if run.side == "BUY" else 0)
            try:
                yield record
            except BaseException as error:
                record(stage="incomplete", error_type=type(error).__name__)
                raise
            else:
                record(stage="complete")


def check_depth(depth, token, limit, budget, *, scenario=LIMIT_BUY, quantity_raw=0):
    if depth.get("position_id") != token:
        raise ValueError("盘口深度与 NO token 不匹配")
    side = "bids" if scenario == MARKET_SELL else "asks"
    levels = [(price_amount(row["price"], allow_one=True), raw_amount(row["total_qty"]))
              for row in depth.get(side, [])]
    if scenario == MARKET_SELL:
        quantity = sum(size for price, size in levels if price >= limit)
        if quantity < quantity_raw:
            raise ValueError("预检价格下限内的买盘深度不足")
        return quantity
    quantity = sum(size for price, size in levels if price <= limit)
    if scenario == LIMIT_CANCEL:
        if quantity:
            raise ValueError("撤单用例的 post-only 限价会与卖盘交叉")
        return quantity
    if quantity * limit < budget:
        raise ValueError("批准限价内的卖盘深度不足")
    return quantity


def signing_route(plan):
    legs = plan.get("legs")
    if not isinstance(legs, list) or not legs or plan.get("offset_netting"):
        raise ValueError("缺少分腿或出现 offset")
    sources = {leg["funding_source"] for leg in legs}
    cash = plan.get("cash_leg")
    signing = plan.get("cash_signing")
    if sources == {"credit"} and not cash and not signing:
        return "credit"
    if sources == {"cash"} and len(legs) == 1 and isinstance(cash, dict) and cash:
        if plan["venue"] == "polymarket" and signing == "server":
            return "server"
        if signing == "client" or (plan["venue"] == "buzzing" and not signing):
            return "client"
    raise ValueError("本轮未批准该分腿或签名组合")
