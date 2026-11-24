from function.api_client import request


def get_buying_power(client, *, condition_id, token_id):
    """查询当前账号在所选市场选项的可用购买力。"""
    return request(
        client, "GET", "/api/v1/orderbook/buying-power",
        operation="buying_power",
        params={"condition_id": condition_id, "token_id": token_id},
    )
