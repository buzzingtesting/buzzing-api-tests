import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from function import polymarket_limit_trade as limit_trade, polymarket_trade, trade_preview
from function.trade_run import TradeRun


@pytest.mark.parametrize(("eligible_no", "result"), [(False, "filled"), (True, "filled"),
                                                   (True, "depth_moved"), (True, "partial"),
                                                   (True, "cancelled"), (True, "cancel_partial"),
                                                   (True, "cancel_postflag"), (True, "cancel_price")])
def test_nba_limit_buy_uses_first_listed_market_and_latest_ask(tmp_path, monkeypatch, eligible_no, result):
    now = datetime.now(timezone.utc)
    passive = result.startswith("cancel")
    budget_raw = "2000000" if passive else "3000000"
    path = tmp_path / "trade.json"
    path.write_text(json.dumps({
        "scenario": "polymarket_nba_limit_cancel" if passive else "polymarket_nba_limit_buy",
        "account": "ci_tester", "funding": "cash", "environment": "preview",
        "venue": "polymarket", "side": "BUY", "order_type": "LIMIT", "budget_raw": budget_raw,
        "run_id": str(uuid4()), "start": now.isoformat(), "end": (now + timedelta(minutes=3)).isoformat(),
    }))
    run = TradeRun.from_file(path)
    signer, maker = "0x" + "1" * 40, "0x" + "2" * 40
    options = [{"outcome_label": "Yes", "position_id": "111"}, {"outcome_label": "No", "position_id": "222"}]
    first = {"market": {"id": "10", "market_type": "polymarket", "outcome_kind": "binary", "condition_id": "c10",
                        "order_min_size": "5", "order_price_min_tick_size": "0.01", "question": "NBA first row?",
                        "is_negrisk": True, "accepting_orders": True}, "options": options}
    second = {"market": {**first["market"], "id": "11", "condition_id": "c11"}, "options": options}
    event = {"id": "20", "title": "NBA: Season champion", "market_count": 30, "type": "polymarket", "status": "active",
             "end_time": str(int(now.timestamp()) + 3600), "markets": [first, second]}
    token, price = ("222", "0.30") if eligible_no else ("111", "0.24")
    if passive:
        price = "0.29"
    requested = "6.3" if passive else "9.17" if eligible_no else "11.46"
    fee_raw = 164_430 if passive else 0
    plan = {"venue": "polymarket", "cash_signing": "server", "cash_leg": {"typed_data_json": ""},
            "legs": [{"funding_source": "cash", "quantity": str(int(Decimal(requested).scaleb(6))),
                      "est_fee": str(fee_raw)}],
            "plan_id": "plan-limit", "logical_order_id": "logical-limit",
            "plan_expires_at": (now + timedelta(minutes=2)).isoformat()}
    checked_tokens, prepared, submitted, evidence = [], [], [], []
    cancels = []
    reserve = Decimal(price) * Decimal(requested) + Decimal(fee_raw).scaleb(-6)
    elapsed = 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def handle(request):
        match request.url.path:
            case "/api/v1/auth/session":
                body = {"user_id": "123"}
            case "/api/v1/user/info":
                body = {"user_id": 123, "wallet_address": signer, "smart_wallet_address": maker,
                        "max_withdrawable": "10", "polymarket": {"origin_pusd_balance": "2080312" if passive else "5000000"}}
            case "/api/v1/events/filter":
                assert request.url.params["sort_by"] == "24hr_volume"
                body = {"items": [dict(event, title="NFL winner"), dict(event, market_count=1), event]}
            case "/api/v1/event/20/markets":
                body = {"markets": [second, first]}
            case "/api/v1/event/20":
                body = {"id": 20, "neg_risk": True}
            case "/api/v1/orderbook/depth":
                position = request.url.params["position_id"]
                checked_tokens.append(position)
                quote = ("0.30" if eligible_no else "0.80") if position == "222" else "0.24"
                if prepared and result == "depth_moved":
                    quote = str(Decimal(quote) + Decimal("0.01"))
                body = {"position_id": position, "asks": [{"price": quote, "total_qty": "50"}]}
            case "/api/v1/orderbook/buying-power":
                assert dict(request.url.params) == {"condition_id": "c10", "token_id": token}
                cash_raw = 4_500_000 - int(reserve.scaleb(6)) if submitted and passive and not cancels else 4_500_000
                body = {"balance": str(cash_raw), "buying_power": str(cash_raw)}
            case "/api/v1/user/delegated-signer":
                body = {"status": "granted", "signer_id": "qa", "wallet_address": signer}
            case "/api/v1/orderbook/orders/prepare":
                payload = json.loads(request.content)
                assert payload.pop("signer_address") == signer and payload.pop("maker_address") == maker
                assert int(payload.pop("expiration")) >= int(now.timestamp()) + 240
                assert payload == {"venue": "polymarket", "condition_id": "c10", "token_id": token,
                                   "side": "BUY", "order_type": "LIMIT", "total_budget": budget_raw, "price": price,
                                   "post_only": passive, "use_credit": False, "is_neg_risk": True, "signature_type": 2}
                prepared.append(payload)
                body = plan
            case "/api/v1/orderbook/orders/submit":
                submitted.append(json.loads(request.content))
                body = {"logical_order_id": "logical-limit", "replayed": False, "results": [
                    {"success": True, "funding_source": "cash", "order_id": "order-limit", "status": "open"},
                ]}
            case "/api/v1/orderbook/orders":
                assert request.url.params["logical_order_ids"] == "logical-limit"
                filled = elapsed > 0 and result == "filled"
                body = {"items": [{"source": "polymarket", "market": {"id": 10, "condition_id": "c10"},
                                   "option": {"position_id": token},
                                   "order": {"order_id": "order-limit", "logical_order_id": "logical-limit", "side": "buy",
                                             "order_type": "limit", "status": "filled" if filled else "partial",
                                             "onchain_status": "confirmed", "filled_quantity": requested if filled else "1",
                                             "filled_quote": str(Decimal(requested) * Decimal(price)),
                                             "remaining_quantity": "0" if filled else "8.17"}}]}
                if passive:
                    order = body["items"][0]["order"]
                    status = "partial" if result == "cancel_partial" else "open"
                    filled_quantity = "1" if result == "cancel_partial" else "0"
                    order.update(status="cancelled" if cancels else status,
                                 price="0.28" if result == "cancel_price" else price, post_only=result != "cancel_postflag",
                                 filled_quantity=filled_quantity, remaining_quantity="0" if cancels else requested)
            case "/api/v1/orderbook/order/cancel":
                cancels.append(json.loads(request.content))
                body = {}
            case _:
                raise AssertionError(f"Unexpected request {request.url.path}")
        return httpx.Response(200, json=body)

    monkeypatch.setenv("BUZZING_ENV", "preview")
    monkeypatch.setattr(limit_trade, "sample", lambda _, __: ("NO", "YES"))
    monkeypatch.setattr(polymarket_trade, "monotonic", lambda: elapsed)
    monkeypatch.setattr(polymarket_trade, "sleep", advance)
    monkeypatch.setattr(limit_trade, "monotonic", lambda: elapsed)
    monkeypatch.setattr(limit_trade, "sleep", advance)
    monkeypatch.setattr(trade_preview, "get_test_jwt", lambda *_: "test-session")
    monkeypatch.setattr(trade_preview, "create_client",
                        lambda _: httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle)))

    def record(**row):
        evidence.append(row)

    if result == "depth_moved":
        with pytest.raises(ValueError, match="卖盘已不足"):
            trade_preview.execute_trade(run, record)
        assert not submitted
    elif result == "partial":
        with pytest.raises(polymarket_trade.OrderConfirmationTimeout, match="60 秒内未确认成交"):
            trade_preview.execute_trade(run, record)
        assert evidence[-1]["status"] == "partial"
    elif passive:
        if result == "cancel_partial":
            with pytest.raises(ValueError, match="零成交撤单"):
                trade_preview.execute_trade(run, record)
            assert evidence[-1]["status"] == "cancelled" and evidence[-1]["filled_quantity"] == "1"
        else:
            if result == "cancel_price":
                with pytest.raises(ValueError, match="限价"):
                    trade_preview.execute_trade(run, record)
            else:
                trade_preview.execute_trade(run, record)
            stages = {row["stage"]: row for row in evidence}
            assert stages["nba_limit_open"]["status"] == "open"
            assert stages["nba_limit_open"]["requested_post_only"] is True
            assert stages["nba_limit_open"]["post_only"] is (result != "cancel_postflag")
            assert stages["nba_limit_cancelled"]["status"] == "cancelled"
            released = stages["nba_limit_cash_released"]
            assert Decimal(released["cash_before"]) - Decimal(released["cash_open"]) == reserve
            assert released["cash_after"] == released["cash_before"]
        assert cancels == [{"order_id": "order-limit", "source": "polymarket"}]
    else:
        trade_preview.execute_trade(run, record)
        fill = evidence[-1]
        assert (fill["stage"], fill["market_id"], fill["outcome"], fill["limit_price"]) == (
            "nba_limit_buy_confirmed", "10", "NO" if eligible_no else "YES", price,
        )
        assert Decimal(fill["requested_shares"]) == Decimal(fill["filled_quantity"]) and fill["remaining_quantity"] == "0"

    assert len(prepared) == 1
    if not passive:
        assert not cancels
    if result != "depth_moved":
        assert submitted == [{"plan_id": "plan-limit", "cash_signature": ""}]
    assert checked_tokens == (["222", "222"] if eligible_no else ["222", "111", "111"])


def test_partial_fill_wait_ignores_unfilled_open_order(monkeypatch):
    pending = iter([
        {"status": "open", "filled_quantity": "0"},
        {"status": "open", "filled_quantity": "2.5", "remaining_quantity": "8"},
    ])
    reads = []

    def read(client, *, params, timeout):
        reads.append(params["logical_order_ids"])
        return httpx.Response(200)

    monkeypatch.setattr(limit_trade, "list_orders", read)
    monkeypatch.setattr(limit_trade, "match_polymarket_order", lambda *_: next(pending))
    monkeypatch.setattr(limit_trade, "sleep", lambda _: None)
    row = limit_trade.wait_limit_order(
        None, "3", {}, lambda **_: None, "logical", "order", limit_trade.monotonic(), partial=True,
    )
    assert reads == ["logical", "logical"]
    assert row["filled_quantity"] == "2.5"
