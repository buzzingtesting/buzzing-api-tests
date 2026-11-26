import logging
import time
from contextvars import ContextVar

import httpx

from function.settings import BASE_URLS, get_environment


logger = logging.getLogger("buzzing.api")
LOGIN_PATH = "/api/v1/login"
ORDER_SUBMIT_PATH = "/api/v1/orderbook/orders/submit"
PREVIEW_SUBMIT_ALERT_REASONS = {
    "trade-service unavailable, request result unknown, do not replay": "订单结果未知，请回查原单",
    "Could not add funds. Please try again.": "Polymarket pUSD 补资未就绪",
}
preview_submit_alert_handler = ContextVar("preview_submit_alert_handler", default=None)


def create_client(bearer_token=None):
    """创建匿名客户端，或注入由身份服务取得的 Bearer 凭证。"""
    environment = get_environment()
    base_url = httpx.URL(BASE_URLS[environment])

    def prepare_request(request):
        if request.url.scheme != "https" or request.url.netloc != base_url.netloc:
            raise ValueError("请求只能发送到当前环境的 HTTPS API")
        if environment == "prod" and request.method not in {"GET", "HEAD"}:
            if request.method != "POST" or request.url.raw_path != LOGIN_PATH.encode():
                raise ValueError("生产回归仅允许只读请求和 POST /api/v1/login")
        if bearer_token:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

    return httpx.Client(
        base_url=base_url,
        timeout=8.0 if environment == "preview" else 10.0,
        follow_redirects=False,
        event_hooks={"request": [prepare_request]},
    )


def request(client, method, path, *, operation, **kwargs):
    """日志使用固定操作名，避免记录 URL 中的参数和用户标识。"""
    started = time.monotonic()
    try:
        response = client.request(method, path, **kwargs)
    except httpx.RequestError as error:
        failure = type(error).__name__
        logger.error("%s %s %s", method, operation, failure)
        raise RuntimeError(f"{method} {operation} 请求失败（{failure}）") from None

    logger.info(
        "%s %s HTTP %s %.0fms",
        method,
        operation,
        response.status_code,
        (time.monotonic() - started) * 1000,
    )
    on_alert = preview_submit_alert_handler.get()
    if (on_alert is not None and method == "POST" and path == ORDER_SUBMIT_PATH
            and response.status_code == 500 and get_environment() == "preview"):
        _report_preview_submit_error(response, operation, kwargs.get("json"), on_alert)
    return response


def _report_preview_submit_error(response, operation, payload, on_alert):
    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    metadata = body.get("metadata")
    message = body.get("message")
    if (body.get("code") != 500 or body.get("reason") != "INTERNAL"
            or not isinstance(message, str) or message not in PREVIEW_SUBMIT_ALERT_REASONS
            or not isinstance(metadata, dict) or metadata.get("biz_code") not in ("1002", 1002)):
        return
    details = {
        "endpoint": ORDER_SUBMIT_PATH, "operation": operation, "http_status": response.status_code,
        "code": body["code"], "reason": body["reason"], "biz_code": metadata["biz_code"], "message": message,
        "alert_reason": PREVIEW_SUBMIT_ALERT_REASONS[message],
    }
    if isinstance(payload, dict) and isinstance(payload.get("plan_id"), str):
        details["plan_id"] = payload["plan_id"]
    on_alert(details)
