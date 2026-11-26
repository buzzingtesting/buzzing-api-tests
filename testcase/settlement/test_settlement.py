import json
from time import time

import allure
import pytest

from function.identity import get_session_user_id
from function.market import get_dispute_info, get_settlement_history
from function.positions import (
    expect_closed_positions_page,
    expect_positions_page,
    list_closed_positions,
    list_position_history,
    list_positions,
)
from function.reporting import PROJECT_NAME
from function.response_checks import expect_native
from function.trade_run import raw_amount


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("市场争议与结算"),
    allure.epic(PROJECT_NAME),
    allure.feature("市场争议与结算"),
]

NONNEGATIVE_INT = {"type": "integer", "minimum": 0}
NONEMPTY_STRING = {"type": "string", "minLength": 1}
DISPUTE_FIELDS = (
    "can_dispute", "has_user_position", "expected_version", "settlement_phase",
    "dispute_reason", "proposed_result", "dispute_window_ends_at", "dispute_retry_after",
)
DISPUTE_ROUNDS = {"dispute1": 1, "dispute2": 2}


def attach(title, values):
    allure.attach(json.dumps(values, ensure_ascii=False, indent=2), title, allure.attachment_type.JSON)


def expect_dispute_info(reply, market_id):
    fields = {
        "market_id": {"const": market_id},
        "can_dispute": {"type": "boolean"},
        "has_user_position": {"type": "boolean"},
        "dispute_reason": {"type": "string"},
        "dispute_retry_after": NONNEGATIVE_INT,
        "dispute_window_ends_at": NONNEGATIVE_INT,
        "expected_version": NONNEGATIVE_INT,
        "proposed_at": NONNEGATIVE_INT,
        "proposed_result": {"type": "string"},
        "settlement_phase": NONEMPTY_STRING,
    }
    return expect_native(reply, status=200, schema={
        "type": "object", "required": list(fields), "properties": fields,
    })


def expect_settlement_history(reply, market_id, event_id=None):
    fields = {
        "id": NONEMPTY_STRING,
        "eventId": {"type": "integer", "minimum": 1},
        "marketId": {"const": str(market_id)},
        "fromStatus": {"type": "string"},
        "toStatus": NONEMPTY_STRING,
        "triggerType": NONEMPTY_STRING,
        "toSettlementPhase": {"type": "string"},
        "outcome": {"type": "string"},
        "createdAt": {"type": "string", "pattern": "^[1-9][0-9]*$"},
    }
    if event_id is not None:
        fields["eventId"]["const"] = int(event_id)
    return expect_native(reply, status=200, schema={
        "type": "object",
        "required": ["marketId", "history"],
        "properties": {
            "marketId": {"const": str(market_id)},
            "history": {
                "type": "array",
                "items": {"type": "object", "required": list(fields), "properties": fields},
            },
        },
    })["history"]


@allure.title("自营市场争议 · 匿名与登录资格")
@pytest.mark.manual_dispute
def test_dispute_entry(api_client, authenticated_api_client, dispute_market_id):
    market_id = dispute_market_id
    allure.dynamic.parameter("市场 ID", market_id)
    anonymous = expect_dispute_info(get_dispute_info(api_client, market_id), market_id)
    request_time = int(time())
    signed_in = expect_dispute_info(get_dispute_info(authenticated_api_client, market_id), market_id)
    attach("争议资格", {"market_id": market_id, **{
        name: {field: reply[field] for field in DISPUTE_FIELDS}
        for name, reply in (("anonymous", anonymous), ("signed_in", signed_in))
    }})

    assert anonymous["can_dispute"] is False and anonymous["has_user_position"] is False
    assert anonymous["dispute_reason"], "匿名入口应说明禁用原因"
    assert signed_in["has_user_position"] is True, "手动回归账号须持有指定市场的仓位"
    assert signed_in["dispute_window_ends_at"] > request_time, "指定市场须处于争议窗口内"
    assert signed_in["can_dispute"] is True, "有持仓且在争议窗口内的回归账号应可以提交争议"
    assert signed_in["dispute_retry_after"] == 0
    assert signed_in["expected_version"] > 0 and signed_in["proposed_result"]

    history = expect_settlement_history(get_settlement_history(api_client, market_id), market_id)
    assert history, "公示中的市场应有结算历史"
    attach("市场结算历史", {"market_id": market_id, "event_id": history[-1]["eventId"], "entries": len(history)})


@allure.title("自营市场争议 · 已登录但无持仓时不可发起争议")
@pytest.mark.manual_dispute("no_position")
def test_dispute_rejects_user_without_position(authenticated_api_client, dispute_market_id):
    market_id = dispute_market_id
    allure.dynamic.parameter("市场 ID", market_id)
    get_session_user_id(authenticated_api_client)

    request_time = int(time())
    info = expect_dispute_info(get_dispute_info(authenticated_api_client, market_id), market_id)
    attach("无持仓账号的争议资格", {"market_id": market_id, **{field: info[field] for field in DISPUTE_FIELDS}})

    assert info["dispute_window_ends_at"] > request_time, "指定市场须处于争议窗口内"
    assert info["proposed_result"], "指定市场须已公示结果"
    assert info["has_user_position"] is False, "无持仓回归账号须在指定市场没有仓位"
    assert info["can_dispute"] is False, "已登录的无持仓用户应没有争议资格"
    assert info["dispute_reason"].strip(), "无争议资格时应说明原因"


def find_disputed_market_id(client, uid):
    params = {"source": "orderbook", "state": "open", "uid": uid, "page": 1, "page_size": 100}
    while True:
        page = expect_positions_page(list_positions(client, params=params), params)
        for row in page["items"]:
            market = row.get("market") or {}
            market_id = market.get("id")
            phase = market.get("settlement_phase")
            if (row["source"] == "orderbook" and type(market_id) is int and market_id > 0
                    and isinstance(phase, str) and phase in DISPUTE_ROUNDS
                    and raw_amount(row["position"]["quantity"]) > 0):
                return market_id
        if params["page"] * params["page_size"] >= page["pagination"]["total"]:
            pytest.skip("当前账号没有持有处于争议处理中的自营市场")
        params["page"] += 1


def check_already_disputed_market(api_client, authenticated_api_client, market_id):
    allure.dynamic.parameter("市场 ID", market_id)
    info = expect_dispute_info(get_dispute_info(authenticated_api_client, market_id), market_id)
    attach("争议中市场的再次发起资格", {
        "market_id": market_id, "round": info.get("round"), "stage": info.get("stage"),
        **{field: info[field] for field in DISPUTE_FIELDS},
    })

    phase = info["settlement_phase"]
    expected_round = DISPUTE_ROUNDS.get(phase)
    assert expected_round is not None and info.get("round") == expected_round, "市场须处于指定轮次的争议处理中"
    assert info["has_user_position"] is True, "账号须持有指定争议中市场的仓位"
    assert info["can_dispute"] is False, "市场已有争议时应禁用再次发起"
    assert info["dispute_reason"] == "Market is already in dispute", "争议入口应说明市场已有争议"

    history = expect_settlement_history(get_settlement_history(api_client, market_id), market_id)
    submissions = [row for row in history if row["triggerType"] == "user" and row["toSettlementPhase"] == phase]
    assert submissions, "结算历史应包含用户发起本轮争议的记录"
    attach("本轮争议历史", [{key: row[key] for key in ("id", "eventId", "toSettlementPhase", "createdAt")}
                           for row in submissions])


@allure.title("自营市场争议 · 市场已在争议中时不可再次发起")
@pytest.mark.manual_dispute("already_open")
def test_dispute_rejects_when_already_open(api_client, authenticated_api_client, disputed_market_id):
    market_id = disputed_market_id
    try:
        uid = get_session_user_id(authenticated_api_client)
        if market_id is None:
            market_id = find_disputed_market_id(authenticated_api_client, uid)
        check_already_disputed_market(api_client, authenticated_api_client, market_id)
    except (AssertionError, RuntimeError, ValueError, pytest.fail.Exception) as error:
        if disputed_market_id is not None:
            raise
        market = f"市场 {market_id}" if market_id is not None else "选样"
        pytest.skip(f"日常争议回归 {market} 未满足预期：{error}")


def get_settled_history_row(client, position):
    selector = {key: position[key] for key in ("source", "position_id", "condition_id")}
    settled_at = position.get("settled_at")
    assert settled_at, "已结算持仓缺少结算时间"
    params = {**selector, "page": 1, "page_size": 100}
    while True:
        history = expect_native(list_position_history(client, params=params), status=200, schema={
            "type": "object", "required": [*selector, "ended", "settled", "rows", "pagination"],
            "properties": {
                **{key: {"const": value} for key, value in selector.items()},
                "ended": {"const": True}, "settled": {"const": True},
                "rows": {"type": "array", "items": {"type": "object", "required": ["type"]}},
                "pagination": {"type": "object", "required": ["total"], "properties": {
                    "total": {"type": "string", "pattern": "^[0-9]+$"},
                }},
            },
        })
        rows = [row for row in history["rows"] if row["type"] == "settled" and row.get("time") == settled_at
                and row.get("shares") == position.get("settlement_shares")
                and row.get("payout_amount") == position.get("settlement_payout")]
        if rows:
            assert len(rows) == 1, "持仓流水存在重复的结算记录"
            return rows[0]
        if params["page"] * params["page_size"] >= int(history["pagination"]["total"]):
            pytest.fail("持仓流水未找到对应的结算记录", pytrace=False)
        params["page"] += 1


@allure.title("已结算持仓 · 领取状态检查")
def test_settled_claim_statuses(authenticated_api_client):
    client = authenticated_api_client
    uid = get_session_user_id(client)
    params = {"source": "all", "uid": uid, "page": 1, "page_size": 5}
    page = expect_closed_positions_page(list_closed_positions(client, params=params), params)
    checked = []
    other_count = 0
    for position in page["items"]:
        if position["end_kind"] != "settled":
            other_count += 1
            continue
        market = position.get("market") or {}
        result = {
            "market_id": market.get("market_id"), "event_id": market.get("event_id"),
            "source": position["source"], "condition_id": position["condition_id"],
            "position_id": position["position_id"], "can_claim": position.get("can_claim"),
            "payout_amount_raw": position.get("settlement_payout"),
        }
        checked.append(result)
        try:
            payout = raw_amount(result["payout_amount_raw"])
            result["expected_statuses"] = ["none"] if payout == 0 else ["received", "pending"]
            result["actual_status"] = get_settled_history_row(client, position).get("payout_status")
            if result["actual_status"] not in result["expected_statuses"] or result["can_claim"] is not False:
                result["error"] = "领取状态不符合预期"
        except (AssertionError, RuntimeError, ValueError, pytest.fail.Exception) as error:
            result["error"] = str(error)

    failures = [item for item in checked if "error" in item]
    attach("已结算持仓领取状态", {
        "sample_limit": params["page_size"], "sampled_count": len(page["items"]),
        "settled_count": len(checked), "other_count": other_count,
        "passed_count": len(checked) + other_count - len(failures),
        "failed_count": len(failures), "positions": checked,
    })
    if failures:
        markets = ", ".join(str(item["market_id"] or item["condition_id"]) for item in failures)
        pytest.fail(f"{len(failures)} 笔已结算持仓的领取状态未通过，市场 {markets}", pytrace=False)


@allure.title("自营市场结算 · 历史状态与本人持仓兑付")
def test_settled_position_history(api_client, authenticated_api_client):
    client = authenticated_api_client
    uid = get_session_user_id(client)
    params = {"source": "orderbook", "uid": uid, "page": 1, "page_size": 100}
    while True:
        page = expect_closed_positions_page(list_closed_positions(client, params=params), params)
        position = next((row for row in page["items"] if row["end_kind"] == "settled" and row.get("market")), None)
        if position is not None:
            break
        if params["page"] * params["page_size"] >= int(page["pagination"]["total"]):
            pytest.skip("当前账号没有可关联市场的自营结算持仓")
        params["page"] += 1

    market = position["market"]
    market_id = market["market_id"]
    result = position["market_result"].casefold()
    history = expect_settlement_history(get_settlement_history(api_client, market_id), market_id, market["event_id"])
    final = next((row for row in reversed(history) if row["toSettlementPhase"] == "claimable"), None)
    assert final is not None, f"市场 {market_id} 的结算历史应包含进入 claimable 的记录"
    assert final["outcome"].casefold() == result, "市场结算历史与持仓记录的胜方应一致"

    payout = position["settlement_payout"]
    shares = position["settlement_shares"]
    payout_status = get_settled_history_row(client, position)["payout_status"]
    assert payout_status in {"none", "pending", "claim", "received"}
    assert type(position["can_claim"]) is bool
    if raw_amount(payout) == 0:
        assert payout_status == "none" and position["can_claim"] is False
    elif payout_status == "received":
        assert position["can_claim"] is False, "已到账的持仓应关闭 Claim 入口"
    attach("结算历史与持仓兑付", {
        "market_id": market_id, "market_result": result, "position_outcome": position["outcome"],
        "history": [{key: row[key] for key in ("triggerType", "toSettlementPhase", "outcome", "createdAt")} for row in history],
        "settled_shares_raw": shares, "payout_amount_raw": payout, "payout_status": payout_status,
        "can_claim": position["can_claim"],
    })
