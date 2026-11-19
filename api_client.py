import logging
import os
import time

import httpx


logger = logging.getLogger("buzzing.api")
PREVIEW_URL = "https://preview-api.buzzing.app"
PROD_URL = "https://prod-api.buzzing.app"
BASE_URLS = {"preview": PREVIEW_URL, "prod": PROD_URL}


def create_client(bearer_token=None):
    """创建匿名客户端，或注入由身份服务取得的 Bearer 凭证。"""
    environment = os.getenv("BUZZING_ENV", "preview")
    if environment not in BASE_URLS:
        raise ValueError("BUZZING_ENV 只接受 preview 或 prod")
    base_url = httpx.URL(BASE_URLS[environment])

    def prepare_request(request):
        if request.url.scheme != "https" or request.url.netloc != base_url.netloc:
            raise ValueError("请求只能发送到当前环境的 HTTPS API")
        if environment == "prod" and request.method not in {"GET", "HEAD"}:
            raise ValueError("生产环境只允许 GET 和 HEAD 请求")
        if bearer_token:
            request.headers["Authorization"] = f"Bearer {bearer_token}"

    return httpx.Client(
        base_url=base_url,
        timeout=10.0,
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
    return response
