import pytest
from jsonschema import Draft7Validator


def expect_json(response, status):
    if response.status_code == 403 and "error code: 1010" in response.text.lower():
        pytest.fail("Preview 网关拒绝请求（HTTP 403 / 1010）", pytrace=False)
    if response.status_code != status:
        pytest.fail(f"期望 HTTP {status}，实际 HTTP {response.status_code}", pytrace=False)
    try:
        return response.json()
    except ValueError:
        pytest.fail("响应应为 JSON", pytrace=False)


def expect_native(response, *, status, schema):
    body = expect_json(response, status)
    error = next(Draft7Validator(schema).iter_errors(body), None)
    if error is not None:
        # 只输出来自测试契约的规则名称，避免输出响应原值。
        pytest.fail(f"原生响应未通过 schema 校验（{error.validator}）", pytrace=False)
    return body


def expect_envelope(response, *, status, code):
    body = expect_json(response, status)
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        pytest.fail("EnvelopeReply 应包含整数业务 code", pytrace=False)
    if body["code"] != code:
        pytest.fail(f"EnvelopeReply 的业务 code 应为 {code}", pytrace=False)
    return body
