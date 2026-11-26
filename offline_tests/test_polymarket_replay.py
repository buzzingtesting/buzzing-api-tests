import json
from types import SimpleNamespace

import httpx
import pytest

from function import polymarket_replay as replay
from function.polymarket_limit_trade import NbaLimitFill


@pytest.mark.parametrize("result", ["same", "not_replayed", "new_order", "debit", "timeout"])
def test_replay_checks_original_order_quantity_and_account_funds(monkeypatch, result):
    selection = {"market_id": "10", "condition_id": "condition", "position_id": "12345"}
    fill = {"logical_order_id": "logical", "order_id": "order", "status": "filled", "onchain_status": "confirmed",
            "filled_quantity": "17.23"}
    original = NbaLimitFill(selection, "10", "original-plan", fill)
    submitted, query_ids, evidence = [], [], []
    elapsed = 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def handle(request):
        match request.url.path:
            case "/api/v1/profile/funding":
                cash = "297000000" if submitted and result == "debit" else "300000000"
                body = {"cash": cash, "cash_degraded": False}
            case "/api/v1/user/info":
                body = {"user_id": 10, "max_withdrawable": "300", "polymarket": {"origin_pusd_balance": "20000000"}}
            case "/api/v1/orderbook/positions":
                body = {"source": "polymarket", "pagination": {"page": 1, "page_size": 100, "total": 1},
                        "items": [{"source": "polymarket", "position": {
                            "condition_id": "condition", "position_id": "12345", "quantity": "17230000",
                            "frozen_amount": "0", "ownership_type": "user", "funding_account_type": "user",
                        }}]}
            case "/api/v1/orderbook/orders":
                query = request.url.params
                if query.get("logical_order_ids"):
                    query_ids.append(query["logical_order_ids"])
                    body = {"items": [{"source": "polymarket", "market": {"id": 10, "condition_id": "condition"},
                                       "option": {"position_id": "12345"}, "order": {**fill, "side": "buy"}}]}
                else:
                    view = query["view"]
                    if view == "open":
                        order = {"source": "polymarket", "order": {"order_id": "unrelated-open-order"}}
                        duplicate = {"source": "polymarket", "order": {"order_id": "duplicate"}}
                        body = {"items": [order, duplicate] if submitted and result == "new_order" else [order],
                                "pagination": {"has_more": False}}
                    elif query.get("next_cursor"):
                        assert query["next_cursor"] == "next"
                        body = {"items": [{"source": "polymarket", "order": {"order_id": "order"}}],
                                "pagination": {"has_more": False}}
                    else:
                        body = {"items": [], "pagination": {"has_more": True, "next_cursor": "next"}}
            case "/api/v1/orderbook/orders/submit":
                submitted.append(json.loads(request.content))
                if result == "timeout":
                    raise httpx.ReadTimeout("simulated read timeout", request=request)
                body = {"logical_order_id": "logical", "replayed": result != "not_replayed", "results": [
                    {"order_id": "order", "success": True, "funding_source": "cash"},
                ]}
            case _:
                raise AssertionError(f"Unexpected request to {request.url.path}")
        return httpx.Response(200, json=body)

    monkeypatch.setattr(replay, "monotonic", lambda: elapsed)
    monkeypatch.setattr(replay, "sleep", advance)
    run = SimpleNamespace(check_window=lambda: None)
    with httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)) as client:
        def verify():
            replay.verify_nba_limit_replay(client, run, "10", lambda **row: evidence.append(row), original)

        if result == "same":
            verify()
            reconciliation = evidence[-2]
            assert reconciliation["before"] == reconciliation["after"]
            assert reconciliation["replayed"] is True and not reconciliation["new_order_ids"]
            assert query_ids == ["logical", "logical"]
            assert evidence[-1]["status"] == "filled"
        elif result == "timeout":
            with pytest.raises(RuntimeError, match="ReadTimeout"):
                verify()
            assert query_ids == ["logical", "logical"] and evidence[-1]["stage"] == "nba_limit_replay_readback"
        else:
            reason = "重放回包" if result == "not_replayed" else "重放前后"
            with pytest.raises(ValueError, match=reason):
                verify()
            assert evidence[-1]["stage"] == "nba_limit_replay_reconciled"
            if result == "new_order":
                assert evidence[-1]["new_order_ids"] == ["duplicate"]
            if result == "debit":
                assert evidence[-1]["before"]["cash"] == "300.000000"
                assert evidence[-1]["after"]["cash"] == "297.000000"

    assert submitted == [{"plan_id": "original-plan", "cash_signature": ""}]
