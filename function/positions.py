from function.api_client import request
from function.response_checks import expect_native


IDENTIFIER_SCHEMA = {"type": "string", "minLength": 1}
POSITION_ITEM_SCHEMA = {
    "type": "object",
    "required": ["source", "position"],
    "properties": {
        "source": IDENTIFIER_SCHEMA,
        "position": {
            "type": "object",
            "required": ["condition_id", "position_id", "quantity"],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "condition_id": IDENTIFIER_SCHEMA,
                "position_id": IDENTIFIER_SCHEMA,
                "quantity": {"type": "string", "minLength": 1},
            },
        },
        "market": {
            "type": ["object", "null"],
            "required": ["condition_id"],
            "properties": {"condition_id": IDENTIFIER_SCHEMA},
        },
        "option": {
            "type": ["object", "null"],
            "required": ["position_id"],
            "properties": {"position_id": IDENTIFIER_SCHEMA},
        },
        "cycle_summary": {
            "type": ["object", "null"],
            "required": ["user_id"],
            "properties": {"user_id": IDENTIFIER_SCHEMA},
        },
    },
}

CLOSED_POSITION_ITEM_SCHEMA = {
    "type": "object",
    "required": ["source", "condition_id", "position_id", "end_kind", "ended_at", "realized_pnl"],
    "properties": {
        "source": IDENTIFIER_SCHEMA,
        "condition_id": IDENTIFIER_SCHEMA,
        "position_id": IDENTIFIER_SCHEMA,
        "end_kind": IDENTIFIER_SCHEMA,
        "ended_at": IDENTIFIER_SCHEMA,
        "realized_pnl": {"type": "string"},
        "market": {
            "type": ["object", "null"],
            "properties": {"market_type": IDENTIFIER_SCHEMA},
        },
    },
}


def list_positions(client, *, params):
    """查询当前账号持仓，可按用户或市场条件筛选。"""
    return request(
        client, "GET", "/api/v1/orderbook/positions",
        operation="positions_list", params=params,
    )


def list_closed_positions(client, *, params):
    """查询当前账号的已关闭持仓。"""
    return request(
        client, "GET", "/api/v1/orderbook/positions/closed",
        operation="closed_positions_list", params=params,
    )


def expect_positions_page(response, params):
    result = _expect_positions_page(
        response, params, POSITION_ITEM_SCHEMA, {"type": "integer", "minimum": 0},
        extra_properties={
            "pretrading": {
                "type": ["object", "null"],
                "required": ["uid"],
                "properties": {"uid": {"type": "integer"}},
            },
        },
    )
    for item in result["items"]:
        position = item["position"]
        if item.get("market") is not None:
            assert position["condition_id"] == item["market"]["condition_id"], "持仓与市场的 condition_id 不一致"
        if item.get("option") is not None:
            assert position["position_id"] == item["option"]["position_id"], "持仓与选项的 position_id 不一致"
    return result


def expect_closed_positions_page(response, params):
    result = _expect_positions_page(
        response, params, CLOSED_POSITION_ITEM_SCHEMA, {"type": "string", "pattern": "^[0-9]+$"},
        extra_properties={"complete": {"type": "boolean"}},
    )
    for item in result["items"]:
        market_type = (item.get("market") or {}).get("market_type")
        if market_type is not None:
            assert market_type == item["source"], "已关闭持仓与市场的交易来源不一致"
    return result


def _expect_positions_page(response, params, item_schema, total_schema, *, extra_properties):
    result = expect_native(
        response,
        status=200,
        schema={
            "type": "object",
            "required": ["items", "pagination", "source"],
            "properties": {
                "items": {"type": "array", "items": item_schema},
                "source": {"const": params["source"]},
                "pagination": {
                    "type": "object",
                    "required": ["page", "page_size", "total"],
                    "properties": {
                        "page": {"const": params["page"]},
                        "page_size": {"const": params["page_size"]},
                        "total": total_schema,
                    },
                },
                **extra_properties,
            },
        },
    )
    items = result["items"]
    total = int(result["pagination"]["total"])
    remaining = max(0, total - (params["page"] - 1) * params["page_size"])
    assert len(items) <= min(params["page_size"], remaining), "持仓条数超出分页范围"
    assert bool(items) == (remaining > 0), "持仓列表与分页总数不一致"
    return result
