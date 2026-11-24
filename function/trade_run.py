import fcntl
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID


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

    @classmethod
    def from_file(cls, path):
        spec = json.loads(path.read_text())
        if (spec["environment"], spec["venue"], spec["market_id"], spec["outcome"],
                spec["side"], spec["order_type"], spec["limit"], spec["budget_raw"]) != (
                "preview", "buzzing", "4828026", "NO", "BUY", "LIMIT", "0.88", "2000000"):
            raise ValueError("本入口只支持已核对的自有盘口 NO 限价买入组合")
        run_id = str(UUID(spec["run_id"]))
        account, funding, batch = spec["account"], spec["funding"], spec.get("batch_id", "")
        if account not in {"A", "A2", "B2", "B3"} or funding not in {"cash", "credit"}:
            raise ValueError("请选择交易专用账号和已支持的资金路径")
        if (funding == "credit" and not re.fullmatch("[1-9][0-9]*", batch)) or (funding == "cash" and batch):
            raise ValueError("纯 Credit 必须指定本轮批准的批次；Cash 不使用批次")
        result = cls(run_id, account, funding, batch, timestamp(spec["start"]), timestamp(spec["end"]))
        result.check_window()
        return result

    def check_window(self):
        if not self.start <= datetime.now(timezone.utc) < self.end:
            raise ValueError("当前时间不在本轮获批窗口内")


@contextmanager
def trade_journal(root, run):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "4828026.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root / f"{run.run_id}.jsonl").open("x", encoding="utf-8") as stream:
            def record(**fields):
                row = dict(fields, run_id=run.run_id, account=run.account,
                           utc=datetime.now(timezone.utc).isoformat())
                print(json.dumps(row, ensure_ascii=False), file=stream, flush=True)

            record(stage="start", funding=run.funding, market="4828026", batch_id=run.batch_id)
            try:
                yield record
            except BaseException as error:
                record(stage="incomplete", error_type=type(error).__name__)
                raise
            else:
                record(stage="complete")


def check_depth(depth, token, limit, budget):
    if depth.get("position_id") != token:
        raise ValueError("盘口深度与 NO token 不匹配")
    quantity = 0
    for ask in depth["asks"]:
        price, size = Decimal(ask["price"]), ask["total_qty"]
        if not price.is_finite() or not 0 < price <= 1 or not re.fullmatch("[0-9]+", size):
            raise ValueError("盘口价格或总份额格式错误")
        if price <= limit:
            quantity += int(size)
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
