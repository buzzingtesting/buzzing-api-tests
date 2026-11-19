import logging
import time

import httpx

from function.settings import BASE_URLS, get_environment


logger = logging.getLogger("buzzing.api")
LOGIN_PATH = "/api/v1/login"


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
    return response
