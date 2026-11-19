import logging

import pytest

from function.api_client import create_client, request
from function.response_checks import expect_envelope, expect_native
from function.settings import get_environment


def main():
    environment = get_environment()
    logging.basicConfig(format="%(message)s")
    logging.getLogger("buzzing.api").setLevel(logging.INFO)

    with create_client() as client:
        print(
            f"{environment} {client.base_url} "
            f"UA={client.headers['user-agent']} "
            f"timeout={client.timeout.read:g}s "
            f"follow_redirects={client.follow_redirects}",
            flush=True,
        )
        session = request(
            client, "GET", "/api/v1/auth/session", operation="anonymous_session"
        )
        expect_native(
            session,
            status=200,
            schema={
                "type": "object",
                "required": ["auth_level"],
                "properties": {"auth_level": {"type": "integer", "const": 0}},
            },
        )
        if environment == "preview":
            catalog = request(
                client, "GET", "/api/v1/errorcode", operation="preview_legacy_error_catalog"
            )
            expect_envelope(catalog, status=200, code=0)
            user = request(
                client, "GET", "/api/v1/user/info", operation="preview_user_info_anonymous"
            )
            expect_envelope(user, status=401, code=1005)
    print(f"{environment} 预检通过，匿名会话 S0")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, pytest.fail.Exception) as error:
        raise SystemExit(f"API 预检失败，{error}") from None
