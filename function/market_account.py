import allure

from function.orders import list_orders
from function.positions import expect_positions_page, list_positions
from function.response_checks import expect_native
from function.trade_state import OPEN_STATES


def check_market_account_activity(client, market, case):
    """检查当前账号在所选子市场的持仓及在挂限价单。"""
    source = market.get("market_type")
    condition_id = market.get("condition_id")
    position_ids = [option.get("position_id") for option in market["options"]]
    assert source in {"orderbook", "polymarket"}, "市场交易来源无效"
    assert isinstance(condition_id, str) and condition_id, "详情未提供 condition_id"
    assert position_ids and all(
        isinstance(position_id, str) and position_id for position_id in position_ids
    ), "详情未提供选项的 position_id"

    params = {
        "source": source,
        "condition_id": condition_id,
        "page": case["page"],
        "page_size": case["page_size"],
    }
    with allure.step(f"查询所选市场 {market['id']} 的本人未平持仓"):
        position_params = {**params, "state": "open"}
        positions = expect_positions_page(
            list_positions(client, params=position_params), position_params,
        )
        for item in positions["items"]:
            assert item["source"] == source, "持仓来源与所选市场不一致"
            assert item["position"]["condition_id"] == condition_id, "持仓应属于所选市场"
            assert item["position"]["position_id"] in position_ids, "持仓应属于所选市场的选项"
        allure.attach(str(positions["pagination"]["total"]), "持仓总数", allure.attachment_type.TEXT)

    with allure.step(f"查询所选市场 {market['id']} 的本人在挂限价单"):
        orders = expect_native(
            list_orders(
                client,
                params={
                    **params,
                    "view": "open",
                    "order_types": case["order_type"],
                    "position_ids": ",".join(position_ids),
                },
            ),
            status=200,
            schema={
                "type": "object",
                "required": ["items", "source", "pagination"],
                "properties": {
                    "source": {"const": source},
                    "items": {
                        "type": "array",
                        "maxItems": case["page_size"],
                        "items": {
                            "type": "object",
                            "required": ["source", "order"],
                            "properties": {
                                "source": {"const": source},
                                "order": {
                                    "type": "object",
                                    "required": ["condition_id", "position_id", "order_type", "status"],
                                    "properties": {
                                        "condition_id": {"const": condition_id},
                                        "position_id": {"enum": position_ids},
                                        "order_type": {"const": case["order_type"]},
                                        "status": {"enum": sorted(OPEN_STATES)},
                                    },
                                },
                            },
                        },
                    },
                    "pagination": {
                        "type": "object",
                        "required": ["page_size", "has_more"],
                        "properties": {
                            "page_size": {"const": case["page_size"]},
                            "has_more": {"type": "boolean"},
                        },
                    },
                },
            },
        )
        items = orders["items"]
        assert items or not orders["pagination"]["has_more"], "挂单列表为空但分页标记仍有后续"
        allure.attach(str(len(items)), "本页在挂限价单数", allure.attachment_type.TEXT)
