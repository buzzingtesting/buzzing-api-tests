import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from function import polymarket_trade as trade
from function import trade_preview
from function.trade_run import CI_TRADE_ACCOUNT, POLYMARKET_BTC_BUY, TradeRun


@pytest.mark.parametrize(("outcomes", "thin_outcome", "buy_status", "sell_status", "close_at"), [
    (("UP", "DOWN"), None, "confirmed", "confirmed", None),
    (("UP", "DOWN"), None, "slow_confirmed", "confirmed", None),
    (("UP", "DOWN"), "UP", "confirmed", "pending", None),
    (("DOWN", "UP"), None, "pending", None, None),
    (("UP", "DOWN"), None, "confirmed", "confirmed", "prepare"),
    (("DOWN", "UP"), None, "confirmed", "confirmed", "second_sell"),
])
def test_ci_btc_market_buy_and_two_sells(tmp_path, monkeypatch, outcomes, thin_outcome, buy_status, sell_status, close_at):
    now = datetime.now(timezone.utc)
    path = tmp_path / "trade.json"
    path.write_text(json.dumps({
        "environment": "preview", "venue": "polymarket",
        "side": "BUY", "order_type": "MARKET", "scenario": POLYMARKET_BTC_BUY, "budget_raw": "1500000",
        "funding": "cash", "account": CI_TRADE_ACCOUNT, "run_id": str(uuid4()),
        "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(minutes=1)).isoformat(),
    }))
    run = TradeRun.from_file(path)
    options = [{"outcome_label": "Up", "position_id": "100"}, {"outcome_label": "Down", "position_id": "101"}]
    selected_outcome = outcomes[1] if thin_outcome else outcomes[0]
    outcome_tokens = {option["outcome_label"].upper(): option["position_id"] for option in options}
    selected_token = outcome_tokens[selected_outcome]
    market = {"id": "10", "market_type": "polymarket", "outcome_kind": "binary", "outcome_count": 2,
              "condition_id": "condition"}
    listed = {"id": "20", "title": "Will BTC close above the target?", "status": "active", "type": "polymarket",
              "end_time": str(int(now.timestamp()) + 120), "market_count": 1,
              "markets": [{"market": market, "options": options}]}
    loop = {**listed, "id": "21", "title": "BTC Up or Down 1h", "cadence": "1h",
            "markets": [{"market": dict(market, id="11"), "options": options}]}
    eth_options = [dict(option, position_id=str(int(option["position_id"]) + 100)) for option in options]
    eth = {**loop, "id": "22", "title": "Ethereum Up or Down 1h", "markets": [
        {"market": dict(market, id="12", condition_id="eth-condition"), "options": eth_options},
    ]}
    selected_eth_token = str(int(selected_token) + 100)
    plan = {"venue": "polymarket", "cash_signing": "server", "cash_leg": {"typed_data_json": ""},
            "legs": [{"funding_source": "cash"}],
            "plan_expires_at": (now + timedelta(minutes=1)).isoformat()}
    evidence, calls = [], []
    prepared, submitted, polled, confirmed = [], set(), set(), set()
    depth_checks = []
    elapsed = 0
    positions = {selected_token: 3_000_000, selected_eth_token: 3_000_000}
    bought = 2_034_567
    stale_positions = {}
    signer, maker = "0x" + "1" * 40, "0x" + "2" * 40

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def handle(request):
        calls.append((request.method, request.url.path))
        match request.url.path:
            case "/api/v1/auth/session":
                body = {"user_id": "123"}
            case "/api/v1/user/info":
                body = {"user_id": 123, "wallet_address": signer, "smart_wallet_address": maker}
            case "/api/v1/events/filter":
                assert request.url.params["sort_by"] == "volume"
                body = {"items": [loop, listed, eth], "has_more": False}
            case "/api/v1/event/21/markets":
                body = {"markets": loop["markets"]}
                if close_at == "second_sell" and 2 in confirmed:
                    body = {"markets": [{"market": dict(market, id="11", settlement_phase="proposed"), "options": options}]}
            case "/api/v1/event/22/markets":
                body = {"markets": eth["markets"]}
            case "/api/v1/orderbook/depth":
                token = request.url.params["position_id"]
                assert request.url.params["source"] == "polymarket"
                depth_checks.append(token)
                quantity = "0.1" if token == outcome_tokens.get(thin_outcome) else "2.1"
                body = {"position_id": token, "asks": [{"price": "0.74", "total_qty": quantity}],
                        "bids": [{"price": "0.73", "total_qty": "3"}]}
            case "/api/v1/orderbook/buying-power":
                body = {"balance": "2000000", "buying_power": "2000000"}
            case "/api/v1/user/delegated-signer":
                body = {"status": "granted", "signer_id": "qa", "wallet_address": signer}
            case "/api/v1/orderbook/positions":
                token = request.url.params["position_id"]
                condition = "eth-condition" if token == selected_eth_token else "condition"
                assert request.url.params["condition_id"] == condition
                snapshot = stale_positions.pop(token, positions[token])
                body = {"source": "polymarket", "pagination": {"page": 1, "page_size": 100, "total": 1},
                        "items": [{"source": "polymarket", "position": {
                            "condition_id": condition, "position_id": token, "quantity": str(snapshot),
                            "frozen_amount": "0", "ownership_type": "user", "funding_account_type": "user",
                        }}]}
            case "/api/v1/orderbook/orders/prepare":
                payload = json.loads(request.content)
                assert request.headers["x-wallet-address"] == maker
                assert payload["signer_address"] == signer and payload["maker_address"] == maker
                token = payload["token_id"]
                assert token in positions
                assert (payload["venue"], payload["order_type"]) == ("polymarket", "MARKET")
                assert payload["use_credit"] is False and not {"price", "expiration"} & payload.keys()
                if close_at == "prepare" and token == selected_token:
                    return httpx.Response(409, json={"error_code": "RESULT_JUST_PROPOSED"})
                market_orders = [order for order in prepared if order["token_id"] == token]
                prepared.append(payload)
                order_number = len(prepared)
                if not market_orders:
                    assert payload["side"] == "BUY" and payload["total_budget"] == "1500000"
                    assert "quantity" not in payload
                else:
                    assert payload["side"] == "SELL" and "total_budget" not in payload
                    assert payload["quantity"] == ("1010000" if len(market_orders) == 1 else "1020000")
                body = dict(plan, plan_id=f"plan-{order_number}", logical_order_id=f"logical-{order_number}")
            case "/api/v1/orderbook/orders/submit":
                order_number = len(prepared)
                assert order_number not in submitted
                submitted.add(order_number)
                assert json.loads(request.content) == {"plan_id": f"plan-{order_number}", "cash_signature": ""}
                body = {"logical_order_id": f"logical-{order_number}", "replayed": False, "results": [
                    {"funding_source": "cash", "success": True, "order_id": f"order-{order_number}", "status": "submitted"},
                ]}
            case "/api/v1/orderbook/orders":
                order_number = len(prepared)
                assert dict(request.url.params) == {
                    "source": "polymarket", "logical_order_ids": f"logical-{order_number}", "page": "1", "page_size": "100",
                }
                if order_number not in polled:
                    polled.add(order_number)
                    body = {"items": []}
                else:
                    payload = prepared[-1]
                    token = payload["token_id"]
                    is_buy = payload["side"] == "BUY"
                    onchain = buy_status if is_buy else sell_status
                    if onchain == "slow_confirmed":
                        onchain = "confirmed" if elapsed >= 25 else "pending"
                    quantity = bought if is_buy else int(payload["quantity"])
                    if onchain == "confirmed":
                        assert order_number not in confirmed
                        confirmed.add(order_number)
                        stale_positions[token] = positions[token]
                        positions[token] += quantity if is_buy else -quantity
                    body = {"items": [{
                        "source": "polymarket", "market": {"id": 12 if token == selected_eth_token else 11,
                                                           "condition_id": payload["condition_id"]},
                        "option": {"position_id": token},
                        "order": {"logical_order_id": f"logical-{order_number}", "order_id": f"order-{order_number}",
                                  "side": payload["side"].lower(), "status": "filled", "onchain_status": onchain,
                                  "filled_quantity": trade.shares(quantity), "filled_quote": "1.48" if is_buy else "0.74",
                                  "remaining_quantity": "0"},
                    }]}
            case _:
                raise AssertionError("Unexpected request path")
        return httpx.Response(200, json=body)

    monkeypatch.setattr(trade, "monotonic", lambda: elapsed)
    monkeypatch.setattr(trade, "sleep", advance)
    monkeypatch.setattr(trade, "sample", lambda population, count: outcomes if population == trade.BTC_OUTCOMES else population)
    monkeypatch.setenv("BUZZING_ENV", "preview")
    monkeypatch.delenv("BUZZING_TRADE_BACKEND", raising=False)

    def get_ci_token(account, profile):
        assert account == CI_TRADE_ACCOUNT and profile["environment"] == "preview"
        return "session"

    def create_client(token):
        assert token == "session"
        return httpx.Client(base_url="https://offline.invalid", transport=httpx.MockTransport(handle))

    monkeypatch.setattr(trade_preview, "get_test_jwt", get_ci_token)
    monkeypatch.setattr(trade_preview, "create_client", create_client)

    def record(**row):
        evidence.append(row)

    if buy_status == "pending":
        with pytest.raises(trade.OrderConfirmationTimeout, match="60 秒内未确认成交"):
            trade_preview.execute_trade(run, record)
        assert elapsed == 60
        assert submitted == {1} and evidence[-1]["stage"] == "btc_confirmation_timeout"
        assert evidence[-1]["timeout_seconds"] == 60 and evidence[-1]["onchain_status"] == "pending"
    else:
        buy = trade_preview.execute_trade(run, record)
        assert elapsed == (25 if buy_status == "slow_confirmed" else 1)
        assert buy.quantity_raw == bought and buy.position_before_buy_raw == 3_000_000
        bought_fill = evidence[-1]
        assert bought_fill["budget"] == "1.500000" and bought_fill["remaining_quantity"] == "0"
        if sell_status == "pending":
            with pytest.raises(trade.OrderConfirmationTimeout, match="订单 logical-2 在 60 秒内未确认成交"):
                trade_preview.execute_trade(run, record, btc_buy=buy)
            assert submitted == {1, 2} and evidence[-1]["side"] == "SELL"
            assert evidence[-1]["timeout_seconds"] == 60
        else:
            trade_preview.execute_trade(run, record, btc_buy=buy)
            assert submitted == (set(range(1, 6)) if close_at == "second_sell" else {1, 2, 3})
            legs = [row for row in evidence if row["stage"] in {"btc_half_sell_confirmed", "btc_remaining_sell_confirmed"}]
            expected_buy = "logical-3" if close_at == "second_sell" else "logical-1"
            assert [(leg["requested_shares"], leg["buy_logical_order_id"]) for leg in legs[-2:]] == [
                ("1.010000", expected_buy), ("1.020000", expected_buy),
            ]
            assert evidence[-1]["stage"] == "btc_sell_position_checked"
            assert evidence[-1]["remaining_bought_shares"] == "0.004567"
            assert evidence[-1]["position_after_sell"] == "3.004567"

    assert calls.count(("POST", "/api/v1/orderbook/orders/submit")) == len(submitted)
    initial_depth = [outcome_tokens[outcome] for outcome in (outcomes if thin_outcome else outcomes[:1])]
    fallback = [row for row in evidence if row["stage"] == "market_fallback"]
    if close_at:
        trigger = "第 2 笔卖出前" if close_at == "second_sell" else "BUY 下单准备"
        assert [(row["market_id"], row["fallback_asset"], row["phase"]) for row in fallback] == [("11", "ETH", trigger)]
        assert depth_checks == initial_depth + [selected_token] * (close_at == "second_sell") + [selected_eth_token] * 3
        assert evidence[-1]["market_id"] == "12"
        assert [order["side"] for order in prepared if order["token_id"] == selected_eth_token] == ["BUY", "SELL", "SELL"]
        assert positions[selected_token] == (4_024_567 if close_at == "second_sell" else 3_000_000)
    else:
        assert fallback == []
        assert depth_checks == initial_depth + [selected_token] * (len(submitted) - 1)
    assert evidence[0] == {"stage": "identity_checked", "uid": "123"}
    selected = next(row for row in evidence if row["stage"] == "market_selected")
    assert (selected["market_id"], selected["cadence"], selected["outcome"]) == ("11", "1h", selected_outcome)
    assert selected["eligible_quote"] == "1.554"


def test_final_cash_position_allows_missing_dust_but_requires_baseline(monkeypatch):
    selected = {"market_id": "11"}
    baseline, remainder = 3_000_000, 4_445
    monkeypatch.setattr(trade, "read_cash_position", lambda *_: (baseline, baseline))
    monkeypatch.setattr(trade, "monotonic", iter((0, 12)).__next__)
    with pytest.raises(ValueError, match="持仓未对齐"):
        trade.wait_for_cash_position(None, "123", selected, baseline + remainder)

    monkeypatch.setattr(trade, "monotonic", lambda: 0)
    assert trade.wait_for_cash_position(None, "123", selected, baseline + remainder, baseline_raw=baseline) == (baseline, baseline)

    monkeypatch.setattr(trade, "read_cash_position", lambda *_: (baseline - 1, baseline - 1))
    monkeypatch.setattr(trade, "monotonic", iter((0, 12)).__next__)
    with pytest.raises(ValueError, match="持仓未对齐"):
        trade.wait_for_cash_position(None, "123", selected, baseline + remainder, baseline_raw=baseline)
