import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from re import fullmatch
from time import time
from uuid import UUID

import pytest

from function.api_client import create_client, request
from function.identity import get_test_jwt
from function.market import get_market_event_detail, list_event_markets
from function.orders import list_orders
from function.polymarket_trade import market_closed_reason, read_btc_trade_wallet
from function.response_checks import expect_json
from function.settings import get_environment, load_test_data
from function.trade_run import price_amount, raw_amount, timestamp
from function.trade_state import BOOK


FIXTURES_ENV = "BUZZING_B3_FIXTURES"


def expect_trade_problem(response, *, status, reason, biz_code):
    body = expect_json(response, status)
    metadata = body.get("metadata") if isinstance(body, dict) else None
    assert isinstance(metadata, dict), "交易错误应返回 metadata"
    assert body.get("code") == status and body.get("reason") == reason, f"原生交易错误应为 {status} / {reason}"
    assert str(metadata.get("biz_code")) == str(biz_code), f"原生交易业务码应为 {biz_code}"
    return {"http_status": status, "reason": reason, "biz_code": biz_code}


class TradeFixture:
    order_type = "LIMIT"

    def __init__(self, case):
        if get_environment() != "preview":
            pytest.skip("本用例仅适用于 Preview")
        if os.getenv("GITHUB_RUN_ATTEMPT", "1") != "1":
            pytest.skip("同一次 Actions 运行的重试跳过受控交易资料")
        fixtures = json.loads(os.getenv(FIXTURES_ENV) or "{}")
        if not isinstance(fixtures, dict):
            raise ValueError(f"{FIXTURES_ENV} 应为 JSON 对象")
        if case not in fixtures:
            pytest.skip(f"缺少 Preview 专用数据 {FIXTURES_ENV}.{case}")
        spec = fixtures[case]
        self.case = case
        self.venue = "polymarket" if case.startswith("polymarket_") else "buzzing"
        self.source = "polymarket" if self.venue == "polymarket" else "orderbook"
        self.account = spec["account"]
        self.uid = str(spec["uid"])
        self.event_id = str(spec["event_id"])
        self.market_id = str(spec["market_id"])
        self.outcome = spec["outcome"].upper()
        self.start, self.end = timestamp(spec["start"]), timestamp(spec["end"])
        self.price = price_amount(spec["price"])
        self.budget_raw = raw_amount(spec["budget_raw"])
        if (not all(fullmatch(r"[1-9][0-9]*", value) for value in (self.uid, self.event_id, self.market_id))
                or self.outcome not in {"YES", "NO", "UP", "DOWN"} or not 0 < self.budget_raw <= 3_000_000
                or self.start >= self.end):
            raise ValueError(f"{case} 的身份、市场、预算或执行窗口配置无效")
        if case in {"polymarket_credit", "polymarket_mixed"}:
            batches = spec.get("batch_ids")
            if (not isinstance(batches, list) or not batches
                    or any(not isinstance(batch, str) or not fullmatch(r"[1-9][0-9]*", batch) for batch in batches)
                    or len(set(batches)) != len(batches)):
                raise ValueError(f"{case} 需提供非空、互不重复的 Credit 批次 ID")
        self.spec = spec
        self.check_window()

    def check_window(self):
        if not self.start <= datetime.now(timezone.utc) < self.end:
            pytest.skip(f"当前时间不在 {self.case} 获准窗口内")

    @contextmanager
    def connect(self):
        account = load_test_data().get(self.account)
        if not isinstance(account, dict) or account.get("environment") != "preview" or not account.get("email"):
            raise ValueError(f"{self.case} 需要已配置邮箱自动登录的 Preview 专用账号")
        token = get_test_jwt(self.account, account)
        with create_client(token) as client:
            uid, signer, maker = read_btc_trade_wallet(client)
            if uid != self.uid:
                raise ValueError("登录 UID 与专用测试数据不一致")
            client.headers["X-Wallet-Address"] = maker
            yield client, signer, maker

    def select_market(self, client):
        selected = read_scoped_market(client, self.source, self.event_id, self.market_id, self.outcome)
        return dict(selected, limit_price=str(self.price))

    def prepare(self, client, selected, signer, maker, *, use_credit=False):
        self.check_window()
        return request(client, "POST", BOOK + "orders/prepare", operation=f"prepare_{self.case}", json={
            "total_budget": str(self.budget_raw), "venue": self.venue, "side": "BUY",
            "order_type": "LIMIT", "price": str(self.price),
            "condition_id": selected["condition_id"], "token_id": selected["position_id"],
            "use_credit": use_credit, "is_neg_risk": selected["neg_risk"], "signer_address": signer,
            "maker_address": maker, "signature_type": 2, "expiration": str(int(time()) + 300),
        })


def read_scoped_market(client, source, event_id, market_id, outcome):
    event_id, market_id = str(event_id), str(market_id)
    event = expect_json(get_market_event_detail(client, event_id), 200)
    rows = expect_json(list_event_markets(client, event_id), 200)["markets"]
    matched = [row for row in rows if str(row["market"]["id"]) == market_id]
    if str(event["id"]) != event_id or len(matched) != 1:
        raise ValueError("专用子市场 ID 与事件 API 不一致")
    market = matched[0]["market"]
    options = [option for option in matched[0]["options"] if option["outcome_label"].upper() == outcome]
    if (market.get("market_type") != source or len(options) != 1
            or not fullmatch(r"0x[0-9a-fA-F]{64}", market["condition_id"])
            or not fullmatch(r"[0-9]+", options[0]["position_id"])):
        raise ValueError("专用市场的场所、交易状态或方向不符合本轮资料")
    end_time = int(event["end_date"])
    if market.get("market_end_date"):
        end_time = min(end_time, int(timestamp(market["market_end_date"]).timestamp()))
    if (reason := market_closed_reason(market)) or end_time <= time():
        pytest.skip(f"专用市场 {market_id} 已停止交易（{reason or '已达到结束时间'}）")
    neg_risk = event.get("neg_risk")
    if type(neg_risk) is not bool:
        raise ValueError("专用事件缺少有效的 neg_risk")
    return dict(event_id=event_id, market_id=market_id, title=event.get("title"),
                condition_id=market["condition_id"], position_id=options[0]["position_id"],
                outcome=outcome, end_time=end_time, neg_risk=neg_risk,
                minimum_shares=market.get("order_min_size") or "5")


def plan_identifiers(plan):
    plan_id = plan["plan_id"]
    if not fullmatch(r"[0-9a-f]{32}", plan_id):
        raise ValueError("下单方案未返回有效 plan_id")
    return {"plan_id": plan_id, "logical_order_id": str(UUID(plan["logical_order_id"]))}


def require_no_order(client, source, logical_order_id):
    response = list_orders(client, params={"source": source, "logical_order_ids": logical_order_id, "page_size": 100})
    page = expect_json(response, 200)
    assert page["items"] == [], f"逻辑订单 {logical_order_id} 不应有订单记录"


def require_empty_market_orders(client, source, selected):
    filters = {"source": source, "condition_id": selected["condition_id"],
               "position_ids": selected["position_id"], "page_size": 100}
    for view in ("open", "history"):
        page = expect_json(list_orders(client, params={**filters, "view": view}), 200)
        assert not page["items"], "该账号和方向已有订单，请分配本轮独立数据"
    return filters


def funding_snapshot(client):
    funding = expect_json(request(client, "GET", "/api/v1/profile/funding", operation="trade_funding_snapshot"), 200)
    if funding.get("cash_degraded") is not False or funding.get("credit_degraded") is not False:
        raise ValueError("资金查询降级，无法核对交易前后的 Cash 与 Credit")
    if type(funding.get("credit_locked")) is not bool:
        raise ValueError("Credit 锁定状态应为布尔值")
    amounts = {key: raw_amount(funding[key]) for key in ("cash", "credit_available", "credit_in_position")}
    return {**amounts, "credit_locked": funding["credit_locked"]}


def verify_funding_plan(plan, fixture):
    ids = plan_identifiers(plan)
    expected = {"credit", "cash"} if fixture.case == "polymarket_mixed" else {"credit"}
    legs = plan["legs"]
    credit_legs = [leg for leg in legs if leg["funding_source"] == "credit"]
    assert not plan.get("offset_netting") and plan["venue"] == fixture.venue and plan["credit_applied"] is True, "出资计划未选择本轮场所与 Credit"
    assert set(leg["funding_source"] for leg in legs) == expected, "实际资金组合与预期不符"
    assert {str(leg["batch_id"]) for leg in credit_legs} == set(fixture.spec["batch_ids"]), "Credit 批次与本轮资料不符"
    assert all(leg["side"] == "BUY" for leg in legs), "资金腿方向与本轮买单不符"
    assert sum(raw_amount(leg["quote_amount"]) for leg in credit_legs) == raw_amount(plan["credit_quote_amount"]) > 0
    assert sum(raw_amount(leg["quote_amount"]) for leg in legs if leg["funding_source"] == "cash") == raw_amount(
        plan["cash_quote_amount"]
    )
    assert sum(raw_amount(leg["quote_amount"]) + raw_amount(leg["est_fee"]) for leg in legs) <= fixture.budget_raw
    if "cash" in expected:
        assert plan.get("cash_signing") == "server" and plan.get("cash_leg"), "混合计划的 Cash 腿应支持外部场所代签"
        assert raw_amount(plan["cash_quote_amount"]) > 0
    else:
        assert not plan.get("cash_leg"), "纯 Credit 计划无需 Cash 签名"
    return ids, [{"funding_source": leg["funding_source"], "batch_id": leg.get("batch_id"),
                  "quote_raw": leg["quote_amount"], "fee_raw": leg["est_fee"]} for leg in legs]
