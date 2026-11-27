from function.api_client import request


def list_orders(client, *, params, **request_options):
    """查询当前账号订单，支持账户视图和市场筛选。"""
    return request(
        client, "GET", "/api/v1/orderbook/orders",
        operation="orders_list", params=params, **request_options,
    )
