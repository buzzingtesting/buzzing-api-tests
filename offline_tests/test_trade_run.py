import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from function.trade_preview import BOOK, checked_api, execute_trade
from function.trade_cash import execute_cash
from function.trade_run import TradeRun, check_depth, signing_route, trade_journal


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


@pytest.mark.parametrize("outcome", ["ok", "timeout", "wrong_reserve", "server"])
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
            return httpx.Response(200, json=plan)
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline")
        return httpx.Response(200, json=dict(logical_order_id="logical1", results=[{"success": True}],
                                             credit_reserved="99" if outcome == "wrong_reserve" else "100"))

    support = SimpleNamespace(trade=SimpleNamespace(TRADE_NO_MARKET={"token_id": "NO"}),
                              cash=SimpleNamespace(data=lambda reply: reply, plan_ids=lambda reply: ("plan1", "logical1")))
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
        api, _, verify = checked_api(client, run, support, lambda **row: events.append(row))
        if outcome == "server":
            with pytest.raises(ValueError):
                api("POST", BOOK + "orders/prepare", 200, token="session", json={})
            assert len(requests) == 1
            return
        api("POST", BOOK + "orders/prepare", 200, token="session", json={})
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
        assert events[0] == dict(stage="prepared", plan_id="plan1", logical_order_id="logical1")


@pytest.mark.parametrize("scenario", ["filled", "partial_timeout", "foreign", "unresolved"])
def test_cash_reconciles_against_existing_position(run, monkeypatch, scenario):
    clock, grants, events, writes = [0], [], [], []
    monkeypatch.setattr("function.trade_cash.monotonic", lambda: clock[0])
    monkeypatch.setattr("function.trade_cash.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    order_id = str(uuid4())
    order = dict(uid=51, logical_order_id="logical", order_id=order_id, condition_id="condition",
                 position_id="NO", side="BUY", filled_quantity="5")
    def state(*args):
        if not writes or scenario == "unresolved":
            return None
        if scenario == "foreign":
            return dict(order, uid=99, status="partial", onchain_status="pending")
        if scenario == "partial_timeout":
            if len(writes) == 1:
                return dict(order, status="partial", onchain_status="pending")
            return dict(order, status="cancelled", onchain_status="confirmed")
        return dict(order, status="filled", onchain_status="confirmed")

    def api(method, path, status, **kwargs):
        if method == "GET":
            return []
        writes.append((path, kwargs["json"]))
        if scenario in {"unresolved", "partial_timeout"} and path.endswith("/submit"):
            pytest.fail("offline timeout", pytrace=False)
        return dict(logical_order_id="logical", results=[dict(success=True, funding_source="cash")])

    cash = SimpleNamespace(raw=int, data=lambda reply: reply, list_rows=lambda rows: rows, order_state=state,
                           wait_approval=lambda *args: None,
                           position_quantity=lambda *args: 15 if writes else 10,
                           trading_profile=lambda *args: dict(usdb_balance="1999999" if writes else "2000002",
                                                              usdb_frozen_amount="0", is_approve_orderbook_all=False),
                           set_approvals=lambda *args, grant: grants.append(grant),
                           chain_approvals=lambda *args: (2000000, 1, 1, 1, 1, 1) if grants == [True] else (0,) * 6,
                           matching_fills=lambda *args: [dict(status="success", tx_hash="tx", fill_key="key",
                                                             quantity="5", quote_amount="2", fee_amount="1")])
    trade = SimpleNamespace(TRADE_NO_MARKET=dict(condition_id="condition", token_id="NO"), TRADE_BUDGET=2000000,
                            TRADE_LIMIT=Decimal("0.88"))
    args = (SimpleNamespace(cash=cash, trade=trade), api, lambda **row: events.append(row), None,
            "session", run, 51, "signer", "maker", lambda: ("plan", "logical", "signature", "hash"))
    if scenario in {"foreign", "unresolved"}:
        with pytest.raises(ValueError):
            execute_cash(*args)
        assert grants == [True] and len(writes) == 1
        assert events[-1]["stage"] == "check_order_and_grants"
    else:
        execute_cash(*args)
        assert grants == [True, False]
        assert events[-2]["after_position_raw"] == 15 and events[-2]["quantity_raw"] == 5
        assert events[-1]["stage"] == "grants_cleared"
        if scenario == "partial_timeout":
            assert len(writes) == 2 and writes[1] == (BOOK + "order/cancel", {"order_id": order_id})
        else:
            assert len(writes) == 1
