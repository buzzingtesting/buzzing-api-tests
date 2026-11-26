import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from function.trade_preview import BOOK, check_buying_capacity, checked_api, execute_trade, prepare_payload
from function.trade_cash import CashPlan, execute_cash
from function.trade_market import expected_limit_price, read_partial_cancel_quote, select_partial_cancel_market
from function.trade_market import select_trade_market
from function.trade_run import (
    LIMIT_BUY, LIMIT_CANCEL, LIMIT_PARTIAL_CANCEL, MARKET_BUY, MARKET_SELL,
    TradeRun, check_depth, signing_route, trade_journal,
)


@pytest.fixture
def run(tmp_path):
    now = datetime.now(timezone.utc)
    spec = dict(environment="preview", venue="buzzing", market_id="4828026", outcome="NO", side="BUY",
                order_type="LIMIT", limit="0.88", budget_raw="2000000", account="A2", funding="credit",
                batch_id="123", run_id=str(uuid4()), start=(now - timedelta(minutes=1)).isoformat(),
                end=(now + timedelta(minutes=1)).isoformat())
    path = tmp_path / "run.json"
    path.write_text(json.dumps(spec))
    return TradeRun.from_file(path)


@pytest.mark.parametrize("source,venue,mode,expected", [
    ("cash", "buzzing", "", "client"), ("cash", "polymarket", "client", "client"),
    ("cash", "polymarket", "server", "server"), ("credit", "buzzing", "", "credit"),
])
def test_signing_route(source, venue, mode, expected):
    plan = dict(venue=venue, cash_signing=mode, cash_leg={"order_hash": "cash"} if source == "cash" else None,
                legs=[{"funding_source": source}])
    assert signing_route(plan) == expected
    with pytest.raises(ValueError):
        signing_route(dict(plan, offset_netting={"applied": True}))
    with pytest.raises(ValueError):
        signing_route(dict(plan, legs=[{"funding_source": "cash"}, {"funding_source": "credit"}]))


def test_depth_uses_only_marketable_raw_quantity():
    depth = dict(position_id="NO", asks=[dict(price="0.88", total_qty="2000000"),
                                         dict(price="0.87", total_qty="300000"),
                                         dict(price="0.89", total_qty="10000000")])
    assert check_depth(depth, "NO", Decimal("0.88"), 2000000) == 2300000
    with pytest.raises(ValueError):
        check_depth(depth, "NO", Decimal("0.87"), 2000000)
    assert check_depth(depth, "NO", Decimal("0.86"), 2000000, scenario=LIMIT_CANCEL) == 0
    with pytest.raises(ValueError, match="post-only"):
        check_depth(depth, "NO", Decimal("0.87"), 2000000, scenario=LIMIT_CANCEL)
    depth["bids"] = [dict(price="0.87", total_qty="10000"), dict(price="0.86", total_qty="50000")]
    assert check_depth(depth, "NO", Decimal("0.87"), 0, scenario=MARKET_SELL, quantity_raw=10000) == 10000
    with pytest.raises(ValueError, match="买盘深度不足"):
        check_depth(depth, "NO", Decimal("0.87"), 0, scenario=MARKET_SELL, quantity_raw=10001)


def test_market_sell_uses_raw_shares_and_omits_limit_terms(run):
    sell = replace(run, scenario=MARKET_SELL, funding="cash", batch_id="", quantity_raw=10_000)
    market = {"token_id": "NO", "condition_id": "condition"}
    request = prepare_payload(sell, market, "signer", "maker")
    assert request["side"] == "SELL" and request["order_type"] == "MARKET"
    assert request["quantity"] == "10000" and request["use_credit"] is False and request["post_only"] is False
    assert not {"price", "total_budget", "expiration"} & request.keys()


def test_market_buy_uses_budget_without_sending_a_limit_price(run):
    buy = replace(run, scenario=MARKET_BUY, funding="cash", batch_id="")
    request = prepare_payload(buy, {"token_id": "NO", "condition_id": "condition"}, "signer", "maker")
    assert request["side"] == "BUY" and request["order_type"] == "MARKET"
    assert request["total_budget"] == "2000000"
    assert not {"price", "quantity", "expiration"} & request.keys()
    depth = {"position_id": "NO", "asks": [{"price": "0.88", "total_qty": "3000000"}]}
    assert check_depth(depth, "NO", buy.price, 2_000_000, scenario=MARKET_BUY) == 3_000_000


@pytest.mark.parametrize(("side", "quote", "expected"), [
    ("BUY", "0.64", "0.63"),
    ("SELL", "0.62", "0.63"),
    ("BUY", "0.635", "0.63"),
    ("BUY", "0.01", "0.01"),
    ("SELL", "0.99", "0.99"),
    ("BUY", None, None),
    ("SELL", None, None),
])
def test_limit_ticket_initial_price(side, quote, expected):
    assert expected_limit_price(quote, side=side) == expected


def test_limit_ticket_records_expected_prices_from_one_depth_snapshot(run):
    evidence = []
    market = {"token_id": "NO", "condition_id": "condition"}
    support = SimpleNamespace(trade=SimpleNamespace(TRADE_NO_MARKET=market),
                              cash=SimpleNamespace(data=lambda reply: reply))

    def handle(request):
        assert request.method == "GET" and request.url.path == BOOK + "depth"
        return httpx.Response(200, json={
            "position_id": "NO",
            "asks": [{"price": "0.64", "total_qty": "3000000"}],
            "bids": [{"price": "0.61", "total_qty": "1000000"}],
        })

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
        _, depth, _ = checked_api(client, run, support, lambda **row: evidence.append(row))
        depth("session", initialize=True)
        depth("session")

    initial = [row for row in evidence if row["stage"] == "limit_ticket_expected"]
    assert initial == [{
        "stage": "limit_ticket_expected", "best_ask": "0.64", "best_bid": "0.61",
        "expected_buy_limit": "0.63", "expected_sell_limit": "0.62",
    }]
    assert run.price == Decimal("0.88")


def test_cash_capacity_uses_both_available_amounts():
    cash = SimpleNamespace(data=lambda reply: reply)
    recorded = []

    def api(method, path, status, **kwargs):
        assert method == "GET" and path == BOOK + "buying-power"
        assert kwargs["params"] == {"condition_id": "condition", "token_id": "NO"}
        return {"balance": "1999999", "buying_power": "4000000"}

    with pytest.raises(ValueError, match="Cash 余额或购买力不足"):
        check_buying_capacity(cash, api, lambda **row: recorded.append(row), "session",
                              {"condition_id": "condition", "token_id": "NO"})
    assert not recorded


def test_dynamic_market_is_approved_and_confirmed_by_detail(run):
    trade = replace(run, funding="cash", batch_id="", scenario=MARKET_BUY, market_ids=("20",))
    market20 = {"id": "20", "condition_id": "condition20", "outcome_kind": "binary", "outcome_count": 2}
    options = [
        {"outcome_label": "Yes", "position_id": "2"},
        {"outcome_label": "No", "position_id": "3"},
    ]
    event = {
        "id": "10", "title": "Eligible", "type": "orderbook", "status": "active", "market_count": 1,
        "end_time": "9999999999", "markets": [{"market": market20, "options": options}],
    }
    unapproved = {**event, "id": "11", "markets": [{"market": {**market20, "id": "21"}, "options": options}]}
    evidence = []
    detail_paths = []

    def reply(request):
        if request.url.path.endswith("/filter"):
            return httpx.Response(200, json={"items": [unapproved, event]})
        detail_paths.append(request.url.path)
        detail = {**market20, "id": 20, "event_id": 10, "market_type": "orderbook", "options": options}
        return httpx.Response(200, json={"event": {"id": 10, "title": "Eligible"}, "markets": [detail]})

    def api(method, path, status, **kwargs):
        assert method == "GET" and path == BOOK + "depth"
        assert kwargs["params"]["position_id"] == "3"
        return {"position_id": "3", "asks": [{"price": "0.88", "total_qty": "3000000"}]}

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(reply)) as client:
        selected = select_trade_market(
            client, api, SimpleNamespace(data=lambda body: body), "session", trade, 51, "maker",
            lambda **row: evidence.append(row), {"sort_by": "top_picks"},
        )
    assert selected == {"condition_id": "condition20", "token_id": "3"}
    assert detail_paths == [BOOK + "events/10"]
    assert evidence[0]["market_id"] == "20"


def test_preserve_failed_run_and_serialize_market(tmp_path, run):
    with pytest.raises(RuntimeError), trade_journal(tmp_path, run):
        with pytest.raises(BlockingIOError), trade_journal(tmp_path, run):
            pytest.fail("同一盘口不可并发")
        raise RuntimeError("offline")
    with pytest.raises(FileExistsError), trade_journal(tmp_path, run):
        pytest.fail("不可覆盖首次结果")
    rows = [json.loads(row) for row in (tmp_path / f"{run.run_id}.jsonl").read_text().splitlines()]
    assert [row["stage"] for row in rows] == ["start", "incomplete"]
    assert all(row["run_id"] == run.run_id for row in rows)


def test_refuse_production_before_loading_credentials(monkeypatch, run):
    monkeypatch.setenv("BUZZING_ENV", "prod")
    with pytest.raises(ValueError, match="Preview"):
        execute_trade(run, None)


@pytest.mark.parametrize("outcome", ["ok", "timeout", "wrong_reserve", "server", "prepare_timeout"])
def test_submit_once_and_keep_plan_identity(run, outcome):
    requests, events = [], []
    plan = dict(venue="buzzing", plan_id="plan1", logical_order_id="logical1",
                legs=[{"funding_source": "credit", "batch_expires_at": run.end.isoformat()}],
                plan_expires_at=run.end.isoformat(), credit_reserve_amount="100")
    if outcome == "server":
        plan.update(venue="polymarket", cash_leg={"order_hash": "hash"}, cash_signing="server",
                    legs=[{"funding_source": "cash"}])

    def handle(req):
        requests.append(req.url.path)
        if req.url.path.endswith("/prepare"):
            if outcome == "prepare_timeout":
                raise httpx.ReadTimeout("offline")
            return httpx.Response(200, json=plan)
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline")
        return httpx.Response(200, json=dict(logical_order_id="logical1", results=[{"success": True}],
                                             credit_reserved="99" if outcome == "wrong_reserve" else "100"))

    market = {"token_id": "NO", "condition_id": "condition"}
    support = SimpleNamespace(trade=SimpleNamespace(TRADE_NO_MARKET=market),
                              cash=SimpleNamespace(data=lambda reply: reply, plan_ids=lambda reply: ("plan1", "logical1")))
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
        api, _, verify = checked_api(client, run, support, lambda **row: events.append(row))
        order = prepare_payload(run, market, "signer", "maker")
        with pytest.raises(ValueError):
            api("POST", BOOK + "orders/prepare", 200, token="session", json=dict(order, side="SELL"))
        if outcome == "prepare_timeout":
            with pytest.raises(pytest.fail.Exception):
                api("POST", BOOK + "orders/prepare", 200, token="session", json=order)
            with pytest.raises(ValueError):
                api("POST", BOOK + "orders/prepare", 200, token="session", json=order)
            assert requests == [BOOK + "orders/prepare"]
            assert [event["stage"] for event in events] == ["prepare_attempt", "prepare_result_unknown"]
            return
        if outcome == "server":
            with pytest.raises(ValueError):
                api("POST", BOOK + "orders/prepare", 200, token="session", json=order)
            assert len(requests) == 1
            return
        api("POST", BOOK + "orders/prepare", 200, token="session", json=order)
        payload = {"plan_id": "plan1", "cash_signature": ""}
        with pytest.raises(ValueError):
            api("POST", BOOK + "orders/submit", 200, token="other", json=payload)
        if outcome in {"timeout", "wrong_reserve"}:
            with pytest.raises(pytest.fail.Exception):
                api("POST", BOOK + "orders/submit", 200, token="session", json=payload)
        else:
            api("POST", BOOK + "orders/submit", 200, token="session", json=payload)
        with pytest.raises(ValueError):
            api("POST", BOOK + "orders/submit", 200, token="session", json=payload)
        if outcome == "wrong_reserve":
            with pytest.raises(ValueError):
                verify()
        assert requests == [BOOK + "orders/prepare", BOOK + "orders/submit"]
        assert events[1] == dict(stage="prepared", plan_id="plan1", logical_order_id="logical1")


@pytest.mark.parametrize("scenario", [
    "filled", "partial_timeout", "foreign", "unresolved",
    "post_only_cancel", "cancel_timeout", "freeze_missing", "market_sell", "wrong_remaining", "wrong_fee",
    "market_buy", "partial_cancel", "partial_fully_filled", "dynamic_partial_cancel",
])
def test_cash_reconciles_against_existing_position(run, monkeypatch, scenario):
    clock, grants, events, writes = [0], [], [], []
    monkeypatch.setattr("function.trade_cash.monotonic", lambda: clock[0])
    monkeypatch.setattr("function.trade_cash.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    order_id = str(uuid4())
    sale = scenario == "market_sell"
    dynamic = scenario == "dynamic_partial_cancel"
    post_only = scenario in {"post_only_cancel", "cancel_timeout", "freeze_missing"}
    trade_scenario = {
        "market_sell": MARKET_SELL, "market_buy": MARKET_BUY,
        "partial_cancel": LIMIT_PARTIAL_CANCEL, "partial_fully_filled": LIMIT_PARTIAL_CANCEL,
        "dynamic_partial_cancel": LIMIT_PARTIAL_CANCEL,
    }.get(scenario, LIMIT_CANCEL if post_only else LIMIT_BUY)
    run = replace(run, funding="cash", batch_id="", scenario=trade_scenario,
                  price=Decimal("0.87" if sale or post_only else "0.88"),
                  quantity_raw=10_000 if sale else 0)
    if dynamic:
        run = replace(run, partial_budget_cap_raw=5_000_000, selected_budget_raw=2_400_000)
    partial = dynamic or scenario in {"partial_timeout", "partial_cancel"}
    quantity = 10_000 if sale else 2_000_000 if post_only else 6 if partial else 5
    filled = 0 if post_only else 10_000 if sale else 5
    price = "0.87" if sale else "0.4"
    quote, fee = (8700, 100) if sale else (0, 0) if post_only else (2, 1)
    if scenario == "wrong_fee":
        fee = 2
    cash_start, position_start = run.budget_raw + 2, 15_000
    cash_end = cash_start + (quote - fee if sale else 0 if post_only else -3)
    position_end = position_start + (-filled if sale else filled)
    plan = CashPlan("plan", "logical", "hash", "signature", quantity)
    frozen_cash = 1_740_000 if post_only and scenario != "freeze_missing" else 0
    order = dict(uid=51, logical_order_id="logical", order_id=order_id, condition_id="condition",
                 position_id="NO", side=run.side, order_type=run.order_type, order_hash="hash",
                 post_only=post_only, quantity=str(quantity), filled_quantity=str(filled),
                 filled_quote=str(quote), remaining_quantity=str(quantity - filled), had_fill=filled > 0)

    def state():
        if not writes or scenario == "unresolved":
            return None
        if scenario == "foreign":
            return dict(order, uid=99, status="partial", onchain_status="pending")
        if post_only or partial:
            if len(writes) == 1:
                return dict(order, status="pending" if post_only else "partial",
                            onchain_status="none" if post_only else "pending")
            return dict(order, status="cancelled", onchain_status="none" if post_only else "confirmed")
        if scenario == "wrong_remaining":
            return dict(order, status="filled", onchain_status="confirmed", remaining_quantity="1")
        return dict(order, status="filled", onchain_status="confirmed")

    def api(method, path, status, **kwargs):
        if method == "GET":
            params = kwargs["params"]
            if path.endswith("/positions"):
                assert params["ownership_type"] == "user"
                return [dict(position=dict(position_id="NO", quantity=str(position_end if writes else position_start),
                                           frozen_amount="0"))]
            if path.endswith("/trades"):
                return [] if post_only else [dict(order_hash="hash", uid=51, condition_id="condition", position_id="NO",
                                                  side=run.side, price=price, status="success", tx_hash="tx",
                                                  fill_key="key", quantity=str(filled), quote_amount=str(quote),
                                                  fee_amount=str(fee))]
            if params.get("view"):
                assert "order_hash" not in params
                if params["view"] == "open":
                    opened = [{"order": state()}] if post_only and len(writes) == 1 else []
                    return {"items": opened, "pagination": {"has_more": False}}
                if "next_cursor" not in params and filled:
                    return {"items": [{"order": {"order_id": "unrelated"}}],
                            "pagination": {"has_more": True, "next_cursor": "1"}}
                history = state()
                return {"items": [{"order": dict(history, ended_reason=history["status"])}] if filled else [],
                        "pagination": {"has_more": False}}
            if "statuses" in params:
                return []
            assert params["logical_order_ids"] == "logical"
            row = state()
            return [{"order": row}] if row else []
        writes.append((path, kwargs["json"]))
        if scenario in {"unresolved", "partial_timeout"} and path.endswith("/submit"):
            pytest.fail("offline timeout", pytrace=False)
        if scenario == "cancel_timeout" and path.endswith("/cancel"):
            pytest.fail("offline timeout", pytrace=False)
        return dict(logical_order_id="logical", results=[dict(success=True, funding_source="cash", order_hash="hash",
                                                               order_id=order_id)])

    def profile(*args):
        pending = post_only and len(writes) == 1
        return dict(usdb_balance=str(cash_end if writes and not pending else cash_start),
                    usdb_frozen_amount=str(frozen_cash if pending else 0), is_approve_orderbook_all=dynamic or grants == [True])

    cash = SimpleNamespace(data=lambda reply: reply, list_rows=lambda rows: rows.get("items", []) if isinstance(rows, dict) else rows,
                           wait_approval=lambda *args: None,
                           trading_profile=profile,
                           set_approvals=lambda *args, grant: grants.append(grant),
                           chain_approvals=lambda *args: (5_000_000, 1, 1, 1, 1, 1) if dynamic else (2000000, 1, 1, 1, 1, 1) if grants == [True] else (0,) * 6)
    trade = SimpleNamespace(TRADE_NO_MARKET=dict(condition_id="condition", token_id="NO"))
    args = (SimpleNamespace(cash=cash, trade=trade), api, lambda **row: events.append(row), None,
            "session", run, 51, "signer", "maker", lambda: plan)
    if scenario in {"foreign", "unresolved", "wrong_remaining", "wrong_fee", "freeze_missing", "partial_fully_filled"}:
        with pytest.raises(ValueError):
            execute_cash(*args)
        assert len(writes) == (2 if scenario == "freeze_missing" else 1)
        if scenario in {"foreign", "unresolved"}:
            assert grants == [True] and events[-1]["stage"] == "check_order_and_grants"
        else:
            assert grants == [True, False] and events[-1]["stage"] == "grants_cleared"
        assert all(entry["stage"] != "reconciled" for entry in events)
    else:
        execute_cash(*args)
        assert grants == ([] if dynamic else [True, False])
        reconciled = next(entry for entry in events if entry["stage"] == "reconciled")
        assert reconciled["position_raw"] == position_end and reconciled["quantity_raw"] == filled
        assert reconciled["remaining_raw"] == quantity - filled
        assert events[-1]["stage"] == ("preexisting_grants_kept" if dynamic else "grants_cleared")
        if partial or post_only:
            assert len(writes) == 2 and writes[1] == (BOOK + "order/cancel", {"order_id": order_id})
        else:
            assert len(writes) == 1
        if post_only:
            frozen = next(entry for entry in events if entry["stage"] == "cash_frozen")
            assert frozen["cash_frozen_raw"] == 1_740_000 and reconciled["cash_frozen_raw"] == 0
            assert next(entry for entry in events if entry["stage"] == "limit_views_checked")["history_status"] is None
        if scenario == "cancel_timeout":
            assert any(entry["stage"] == "cancel_result_unknown" for entry in events)
        if scenario == "partial_cancel":
            assert any(entry["stage"] == "partial_fill_observed" for entry in events)
        if scenario in {"market_buy", "market_sell"}:
            assert next(row for row in events if row["stage"] == "market_history_checked")["history_status"] == "filled"



def test_dynamic_partial_budget_aggregates_only_best_ask():
    def depth(request):
        assert request.url.params.get("source") == "orderbook"
        return httpx.Response(200, json={"position_id": "NO", "asks": [
            {"price": "0.39", "total_qty": "30000000"},
            {"price": "0.38", "total_qty": "1000000"},
            {"price": "0.380", "total_qty": "100000"},
            {"price": "0.37", "total_qty": "0"},
        ]})

    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(depth)) as client:
        quote = read_partial_cancel_quote(client, "NO")
    assert (quote.price, quote.quantity_raw, quote.quote_raw, quote.budget_raw) == (
        Decimal("0.38"), 1_100_000, 418_000, 2_418_000,
    )



def test_partial_cancel_selects_affordable_binary_child_in_multi_event():
    children = [
        {"id": str(market_id), "condition_id": f"condition-{market_id}", "outcome_kind": "binary",
         "options": [{"outcome_label": "Yes", "position_id": str(100 + market_id)},
                     {"outcome_label": "No", "position_id": str(200 + market_id)}]}
        for market_id in (12, 13)
    ]
    event = {"id": "10", "title": "Two independent outcomes", "type": "orderbook", "status": "active",
             "end_time": "9999999999", "market_count": 2,
             "markets": [{"market": child, "options": child["options"]} for child in children]}
    detail_reads = []

    def reply(request):
        if request.url.path.endswith("/filter"):
            return httpx.Response(200, json={"items": [event]})
        if request.url.path == BOOK + "events/10":
            detail_reads.append(request.url.path)
            return httpx.Response(200, json={
                "event": {"id": 10, "title": event["title"], "neg_risk": False},
                "markets": [dict(child, event_id=10, market_type="orderbook", accepting_orders=True)
                            for child in children],
            })
        token = request.url.params["position_id"]
        sell_amount = {"112": "10000000", "212": "12000000", "113": "20200000"}[token]
        price = "0.05" if token == "113" else "0.4"
        return httpx.Response(200, json={"position_id": token, "asks": [{"price": price, "total_qty": sell_amount}]})

    evidence = []
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(reply)) as client:
        market, quote = select_partial_cancel_market(
            client, {"sort_by": "newest"}, lambda **row: evidence.append(row), max_budget_raw=5_000_000,
        )
    assert market == {"condition_id": "condition-13", "token_id": "113"}
    assert quote.budget_raw == 3_010_000 and len(detail_reads) == 1
    assert [row["stage"] for row in evidence] == [
        "partial_quote_ineligible", "partial_quote_ineligible", "partial_quote_selected",
    ]
