from time import time
from urllib.parse import quote

import allure

from function.api_client import request
from function.reporting import attach_market_sample
from function.response_checks import expect_native


MARKET_EVENT_ITEMS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "required": ["id", "title"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
        },
    },
}

MARKET_EVENT_PAGE_SCHEMA = {
    "type": "object",
    "required": ["items", "has_more"],
    "properties": {
        "items": MARKET_EVENT_ITEMS_SCHEMA,
        "has_more": {"type": "boolean"},
    },
    "if": {"properties": {"has_more": {"const": True}}},
    "then": {
        "required": ["next_cursor"],
        "properties": {"next_cursor": {"type": "string", "minLength": 1}},
    },
}


def list_market_events(client, *, params=None):
    """查询市场事件列表，支持排序和游标翻页。"""
    return request(
        client, "GET", "/api/v1/events/filter",
        operation="market_event_list", params=params,
    )


def find_market_event(client, list_params, matches):
    """按需翻页，返回本轮第一个符合条件的事件。"""
    params = dict(list_params)
    seen_cursors = set()
    while True:
        page = expect_native(
            list_market_events(client, params=params),
            status=200,
            schema=MARKET_EVENT_PAGE_SCHEMA,
        )
        for event in page["items"]:
            if matches(event):
                return event
        if not page["has_more"]:
            return None
        cursor = page["next_cursor"]
        assert cursor not in seen_cursors, "市场列表翻页游标应向前推进"
        seen_cursors.add(cursor)
        params["cursor"] = cursor


def is_open_market_event(event, now):
    end_time = str(event.get("end_time", ""))
    return (
        event.get("status") == "active"
        and end_time.isascii()
        and end_time.isdecimal()
        and int(end_time) > now
    )


def is_single_binary_market_event(event):
    markets = event.get("markets") or []
    return (
        event.get("market_count") == 1
        and len(markets) == 1
        and markets[0]["market"].get("outcome_kind") == "binary"
        and markets[0]["market"].get("outcome_count") == 2
    )


def list_esports_props(client, league_slug, *, params):
    """按联赛查询电竞 Props。"""
    league_slug = quote(league_slug, safe="")
    return request(
        client, "GET", f"/api/v1/polymarket/sports/{league_slug}/props",
        operation="esports_props_list", params=params,
    )


def get_sports_navigation(client):
    """查询体育导航及分类数量。"""
    return request(client, "GET", "/api/v1/sport/nav", operation="sports_navigation")


def _detail_path(resource, resource_id):
    resource_id = str(resource_id)
    if not resource_id.isascii() or not resource_id.isdecimal():
        raise ValueError("详情 ID 必须是数字")
    return f"/api/v1/{resource}/{resource_id}"


def get_market_event_detail(client, event_id):
    """用列表返回的事件 ID 查询详情。"""
    return request(
        client, "GET", _detail_path("event", event_id),
        operation="market_event_detail",
    )


def get_orderbook_event(client, event_id):
    """按事件 ID 查询 Orderbook 事件、市场和最优报价字段。"""
    return request(
        client, "GET", _detail_path("orderbook/events", event_id),
        operation="orderbook_event_detail",
    )


def get_orderbook_depth(client, position_id, *, source):
    """按选项的 position_id 查询买卖盘深度。"""
    return request(
        client, "GET", "/api/v1/orderbook/depth",
        operation="orderbook_depth",
        params={"source": source, "position_id": position_id},
    )


def list_event_markets(client, event_id):
    """按事件 ID 查询该事件的子市场。"""
    return request(
        client, "GET", f"{_detail_path('event', event_id)}/markets",
        operation="market_event_children",
    )


def get_sports_match_detail(client, match_id, *, params):
    """按比赛 ID 查询体育市场详情。"""
    return request(
        client, "GET", _detail_path("sport/match", match_id),
        operation="sports_match_detail", params=params,
    )


def load_binary_orderbook_market(api_client, list_params):
    """从市场列表选取独立二元市场并核对 Orderbook 详情。"""
    with allure.step("从本轮市场列表选取尚未结束的独立二元市场"):
        now = time()
        listed_event = find_market_event(
            api_client,
            list_params,
            lambda event: (
                event.get("type") == "orderbook"
                and is_open_market_event(event, now)
                and is_single_binary_market_event(event)
            ),
        )
        assert listed_event is not None, "市场列表中未找到尚未结束的独立二元市场"
        event_id = int(listed_event["id"])
        listed_market = listed_event["markets"][0]
        market_id = int(listed_market["market"]["id"])
        attach_market_sample(listed_event, market_id=market_id)

    with allure.step("查询 Orderbook 事件并核对市场、选项归属和最优报价字段"):
        book = expect_native(
            get_orderbook_event(api_client, event_id),
            status=200,
            schema={
                "type": "object",
                "required": ["event", "markets"],
                "properties": {
                    "event": {
                        "type": "object",
                        "required": ["id", "title"],
                        "properties": {
                            "id": {"const": event_id},
                            "title": {"const": listed_event["title"]},
                        },
                    },
                    "markets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "items": {
                            "type": "object",
                            "required": [
                                "id", "event_id", "market_type", "outcome_kind",
                                "condition_id", "best_bid", "best_ask", "options",
                            ],
                            "properties": {
                                "id": {"const": market_id},
                                "event_id": {"const": event_id},
                                "market_type": {"const": "orderbook"},
                                "outcome_kind": {"const": "binary"},
                                "condition_id": {"type": "string", "minLength": 1},
                                "best_bid": {"type": "string"},
                                "best_ask": {"type": "string"},
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "items": {
                                        "type": "object",
                                        "required": ["market_id", "outcome_label", "position_id"],
                                        "properties": {
                                            "market_id": {"const": market_id},
                                            "outcome_label": {"type": "string", "minLength": 1},
                                            "position_id": {"type": "string", "minLength": 1},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        )
        market = book["markets"][0]
        assert market["condition_id"] == listed_market["market"]["condition_id"], "市场条件 ID 应与列表一致"
        listed_options = {
            option["outcome_label"]: option["position_id"] for option in listed_market["options"]
        }
        orderbook_options = {
            option["outcome_label"]: option["position_id"] for option in market["options"]
        }
        assert len(orderbook_options) == 2 and orderbook_options == listed_options, "二元选项应与列表一致"
    return market
