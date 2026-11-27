import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from function.settings import get_testdata


LIMIT_BUY = "limit_buy"
LIMIT_CANCEL = "limit_cancel"
LIMIT_PARTIAL_CANCEL = "limit_partial_cancel"
MARKET_BUY = "market_buy"
MARKET_SELL = "market_sell"
OWN_ORDERBOOK_SCENARIOS = (MARKET_BUY, MARKET_SELL, LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL)
OWN_VOLUME_LIMIT_SCENARIOS = (LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL)
OWN_FULL_LIMIT_BUDGET_RAW = 3_000_000
OWN_PARTIAL_EXTRA_RAW = 1_000_000
POLYMARKET_BTC_BUY = "polymarket_btc_buy"
POLYMARKET_SPORTS_DRAW_BUY = "polymarket_sports_draw_buy"
POLYMARKET_NBA_LIMIT_BUY = "polymarket_nba_limit_buy"
POLYMARKET_NBA_LIMIT_CANCEL = "polymarket_nba_limit_cancel"
POLYMARKET_NBA_SCENARIOS = {POLYMARKET_NBA_LIMIT_BUY, POLYMARKET_NBA_LIMIT_CANCEL}
POLYMARKET_SCENARIOS = {POLYMARKET_BTC_BUY, POLYMARKET_SPORTS_DRAW_BUY, *POLYMARKET_NBA_SCENARIOS}
MARKET_ID = "4828026"
BUY_BUDGET_RAW = 2_000_000
BTC_BUY_BUDGET_RAW = 1_500_000
NBA_LIMIT_BUDGET_RAW = 3_000_000
NBA_LIMIT_CANCEL_BUDGET_RAW = 2_000_000
CI_TRADE_BUDGETS_RAW = {
    POLYMARKET_BTC_BUY: BTC_BUY_BUDGET_RAW,
    POLYMARKET_NBA_LIMIT_BUY: NBA_LIMIT_BUDGET_RAW,
    POLYMARKET_NBA_LIMIT_CANCEL: NBA_LIMIT_CANCEL_BUDGET_RAW,
    POLYMARKET_SPORTS_DRAW_BUY: 1_300_000,
}
CI_TRADE_SCENARIOS = tuple(CI_TRADE_BUDGETS_RAW)
TRADE_SHAPES = {
    LIMIT_BUY: ("BUY", "LIMIT"),
    LIMIT_CANCEL: ("BUY", "LIMIT"),
    LIMIT_PARTIAL_CANCEL: ("BUY", "LIMIT"),
    MARKET_BUY: ("BUY", "MARKET"),
    MARKET_SELL: ("SELL", "MARKET"),
    POLYMARKET_BTC_BUY: ("BUY", "MARKET"),
    POLYMARKET_SPORTS_DRAW_BUY: ("BUY", "MARKET"),
    POLYMARKET_NBA_LIMIT_BUY: ("BUY", "LIMIT"),
    POLYMARKET_NBA_LIMIT_CANCEL: ("BUY", "LIMIT"),
}


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
    price: Decimal | None = Decimal("0.88")
    quantity_raw: int = 0
    market_ids: tuple[str, ...] = ()
    outcome: str = "NO"
    partial_budget_cap_raw: int = 0
    selected_budget_raw: int = 0
    select_from_volume: bool = False

    @property
    def budget_raw(self):
        if self.selected_budget_raw:
            return self.selected_budget_raw
        if self.select_from_volume and self.scenario == LIMIT_BUY:
            return OWN_FULL_LIMIT_BUDGET_RAW
        return CI_TRADE_BUDGETS_RAW.get(self.scenario, BUY_BUDGET_RAW if self.side == "BUY" else 0)

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
        volume_selection = spec.get("select_from_volume", False)
        if type(volume_selection) is not bool or (volume_selection and scenario not in OWN_VOLUME_LIMIT_SCENARIOS):
            raise ValueError("Volume 动态选样仅适用于自营限价场景")
        dynamic_partial = scenario == LIMIT_PARTIAL_CANCEL and "max_budget_raw" in spec
        outcome = spec.get("outcome", "" if scenario in POLYMARKET_SCENARIOS or dynamic_partial or volume_selection else None)
        if scenario in POLYMARKET_SCENARIOS:
            outcomes = {
                POLYMARKET_BTC_BUY: {"", "UP", "DOWN"},
                POLYMARKET_SPORTS_DRAW_BUY: {"", "DRAW"},
            }.get(scenario, {"", "YES", "NO"})
            if (spec["environment"], spec["venue"]) != ("preview", "polymarket") or outcome not in outcomes:
                raise ValueError("Polymarket 用例仅在 Preview 运行，方向必须符合所选场景")
        elif (spec["environment"], spec["venue"], outcome) != (
            "preview", "buzzing", "" if dynamic_partial or volume_selection else "NO"
        ):
            raise ValueError("自有订单簿用例仅在 Preview 运行，方向必须符合所选场景")
        dynamic_market = volume_selection or dynamic_partial or (
            scenario in POLYMARKET_SCENARIOS and "market_id" not in spec
        )
        market_ids = spec.get("market_ids")
        if market_ids is None:
            if not dynamic_market and spec.get("market_id") != MARKET_ID:
                raise ValueError("请提供已核对的盘口或本轮批准的候选市场 ID 列表")
            market_ids = []
        elif (
            "market_id" in spec or not isinstance(market_ids, list) or not market_ids
            or any(not isinstance(market, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", market) for market in market_ids)
            or len(set(market_ids)) != len(market_ids)
        ):
            raise ValueError("market_ids 应为互不重复的候选市场 ID 字符串列表")
        if TRADE_SHAPES.get(scenario) != (spec["side"], spec["order_type"]):
            raise ValueError("订单方向和类型必须与本轮场景一致")
        run_id = str(UUID(spec["run_id"]))
        account, funding, batch = get_testdata(), spec["funding"], spec.get("batch_id", "")
        if funding not in {"cash", "credit"}:
            raise ValueError("请选择已支持的资金路径")
        if (funding == "credit" and not re.fullmatch("[1-9][0-9]*", batch)) or (funding == "cash" and batch):
            raise ValueError("纯 Credit 必须指定本轮批准的批次；Cash 不使用批次")
        if funding == "credit" and (market_ids or scenario != LIMIT_BUY):
            raise ValueError("纯 Credit 当前使用已核对的固定盘口限价路径")
        partial_budget_cap = 0
        if dynamic_partial:
            partial_budget_cap = raw_amount(spec["max_budget_raw"])
            increment = OWN_PARTIAL_EXTRA_RAW if volume_selection else BUY_BUDGET_RAW
            if (spec.get("funding") != "cash" or partial_budget_cap <= increment
                    or any(key in spec for key in ("market_id", "market_ids", "limit", "budget_raw",
                                                   "quantity_raw", "min_price", "max_price"))):
                raise ValueError("动态部分成交需要 Cash 与大于 2 U 的单笔上限，盘口和实际 Amount 由当轮计算")
        elif "max_budget_raw" in spec:
            raise ValueError("max_budget_raw 只适用于动态部分成交")
        budget = 0 if dynamic_partial else raw_amount(spec["budget_raw"])
        if volume_selection:
            price, quantity = None, 0
            expected = {LIMIT_BUY: OWN_FULL_LIMIT_BUDGET_RAW, LIMIT_CANCEL: BUY_BUDGET_RAW, LIMIT_PARTIAL_CANCEL: 0}
            if (funding != "cash" or budget != expected[scenario] or (scenario == LIMIT_PARTIAL_CANCEL and not dynamic_partial)
                    or any(key in spec for key in ("market_id", "market_ids", "limit", "quantity_raw", "min_price", "max_price"))):
                raise ValueError("Volume 自营限价仅使用 Cash，由本轮盘口决定限价")
        elif scenario == POLYMARKET_BTC_BUY:
            price, quantity = price_amount(spec.get("max_price", "1"), allow_one=True), 0
            if (funding != "cash" or budget != BTC_BUY_BUDGET_RAW or (not market_ids and not dynamic_market)
                    or any(key in spec for key in ("market_id", "limit", "quantity_raw", "min_price"))):
                raise ValueError("BTC 市价买入使用 1.5 Cash 含费预算，由 CI 动态选样或使用指定的候选列表")
        elif scenario in POLYMARKET_NBA_SCENARIOS:
            price, quantity = None, 0
            if (funding != "cash" or budget != CI_TRADE_BUDGETS_RAW[scenario]
                    or any(key in spec for key in ("market_id", "limit", "quantity_raw", "min_price", "max_price"))):
                raise ValueError("NBA 限价场景使用 CI 测试账号和本场景规定的含费 Cash 预算")
        elif scenario == POLYMARKET_SPORTS_DRAW_BUY:
            price, quantity = None, 0
            if (funding != "cash" or budget != CI_TRADE_BUDGETS_RAW[scenario]
                    or any(key in spec for key in ("market_id", "market_ids", "limit", "quantity_raw", "min_price", "max_price"))):
                raise ValueError("Sports Moneyline Draw 使用 CI 测试账号和 1.3 U 含费 Cash 预算，从体育列表实时选样")
        elif dynamic_partial:
            price, quantity = None, 0
        elif scenario == MARKET_SELL:
            price = price_amount(spec["min_price"])
            quantity = raw_amount(spec["quantity_raw"])
            if funding != "cash" or budget or spec.get("limit") or not 10_000 <= quantity <= 2_000_000:
                raise ValueError("市价卖出仅允许专用 Cash 仓位，数量为 0.01 至 2 份，无买入预算")
        elif scenario == MARKET_BUY:
            price, quantity = price_amount(spec["max_price"]), 0
            if budget != BUY_BUDGET_RAW or any(key in spec for key in ("limit", "quantity_raw", "min_price")):
                raise ValueError("市价买入使用 2 测试 USDB 含费预算和 max_price")
        else:
            price, quantity = price_amount(spec["limit"]), 0
            if budget != BUY_BUDGET_RAW or any(key in spec for key in ("quantity_raw", "min_price", "max_price")):
                raise ValueError("限价买入使用 2 测试 USDB 含费预算")
            if scenario == LIMIT_BUY and not market_ids and price != Decimal("0.88"):
                raise ValueError("原固定盘口限价成交使用 0.88")
        result = cls(run_id, account, funding, batch, timestamp(spec["start"]), timestamp(spec["end"]),
                     scenario, price, quantity, tuple(market_ids), outcome, partial_budget_cap,
                     select_from_volume=volume_selection)
        if result.start >= result.end:
            raise ValueError("交易窗口的起点必须早于终点")
        if check_window:
            result.check_window()
        return result

    def check_window(self):
        if not self.start <= datetime.now(timezone.utc) < self.end:
            raise ValueError("当前时间不在本轮获批窗口内")


@contextmanager
def trade_journal(root, run, *, selling=False, nba_replay=False):
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    markets = run.market_ids
    dynamic_market = run.scenario in POLYMARKET_SCENARIOS or run.select_from_volume or run.partial_budget_cap_raw
    if not markets and not dynamic_market:
        markets = (MARKET_ID,)
    with ExitStack() as locks:
        for lock_name in sorted((*markets, f"account-{run.account}")):
            lock = locks.enter_context((root / f"{lock_name}.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        suffix = "-sell" if selling else "-replay" if nba_replay else ""
        with (root / f"{run.run_id}{suffix}.jsonl").open("x", encoding="utf-8") as stream:
            def record(**fields):
                row = dict(fields, run_id=run.run_id, account=run.account,
                           utc=datetime.now(timezone.utc).isoformat())
                print(json.dumps(row, ensure_ascii=False), file=stream, flush=True)

            market_scope = {"candidate_markets": list(markets)} if dynamic_market or run.market_ids else {"market": MARKET_ID}
            record(stage="start", funding=run.funding, **market_scope, batch_id=run.batch_id,
                   scenario=run.scenario, side="SELL" if selling else run.side, order_type=run.order_type,
                   price=str(run.price) if run.price is not None else None, quantity_raw=run.quantity_raw,
                   budget_raw=0 if selling or nba_replay or run.partial_budget_cap_raw else run.budget_raw,
                   max_budget_raw=run.partial_budget_cap_raw or None)
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
    if scenario == LIMIT_PARTIAL_CANCEL:
        if not quantity:
            raise ValueError("部分成交场景的限价内暂无卖盘")
        if quantity * limit >= budget:
            raise ValueError("限价内的卖盘足以覆盖整笔预算，需选取可控的部分成交盘口")
        return quantity
    if scenario == MARKET_BUY:
        quote = sum(price * size for price, size in levels if price <= limit)
        if quote < budget:
            raise ValueError("预检价格上限内的卖盘金额不足")
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
    if sources == {"cash"} and len(legs) == 1 and signing == "server":
        if plan.get("venue") == "buzzing" or (plan.get("venue") == "polymarket" and isinstance(cash, dict) and cash):
            return "server"
    if sources == {"cash"} and len(legs) == 1 and isinstance(cash, dict) and cash:
        if signing == "client" or (plan["venue"] == "buzzing" and not signing):
            return "client"
    raise ValueError("本轮未批准该分腿或签名组合")
