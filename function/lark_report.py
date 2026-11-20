"""把 Preview 的 JUnit 用例统计和报告链接发到 Lark。"""

import base64
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx


def junit_summary(reports_dir):
    files = list(reports_dir.glob("run-*/junit.xml"))
    if not files:
        return "本轮暂无用例统计，请查看 Actions 日志。"

    junit_file = max(files, key=lambda path: path.stat().st_mtime_ns)
    try:
        root = ElementTree.parse(junit_file).getroot()
    except (OSError, ElementTree.ParseError):
        return "本轮 JUnit 报告无法读取，请查看 Actions 日志。"

    outcomes = ("error", "failure", "skipped")
    counts = dict.fromkeys((*outcomes, "passed"), 0)
    for test in root.iter("testcase"):
        result = next((tag for tag in outcomes if test.find(tag) is not None), "passed")
        counts[result] += 1

    return (
        f"**用例 {sum(counts.values())} 条**\n"
        f"通过 {counts['passed']} · 失败 {counts['failure']} · "
        f"错误 {counts['error']} · 跳过 {counts['skipped']}"
    )


def build_card(summary, outcome, run_url, report_url):
    label, color = {
        "success": ("通过", "green"),
        "failure": ("失败", "red"),
    }.get(outcome, ("场景未执行", "grey"))
    report = f"[下载 Allure 报告]({report_url})" if report_url else "本轮暂无 Allure 报告"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"Buzzing Preview 接口自动化 · {label}"},
                "template": color,
            },
            "elements": [{
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{summary}\n\n{report} · [查看 Actions 运行]({run_url})",
                },
            }],
        },
    }


def main():
    webhook_url = os.getenv("LARK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("未配置 LARK_WEBHOOK_URL，跳过 Lark 通知。")
        return

    endpoint = urlsplit(webhook_url)
    if (
        endpoint.scheme != "https"
        or endpoint.netloc != "open.larksuite.com"
        or not endpoint.path.startswith("/open-apis/bot/v2/hook/")
    ):
        raise SystemExit("请检查 LARK_WEBHOOK_URL 的格式")

    run_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    outcome = os.getenv("REPORT_OUTCOME", "")
    payload = build_card(
        junit_summary(Path("reports")) if outcome in {"success", "failure"} else "本轮暂无用例统计。",
        outcome, run_url, os.getenv("REPORT_ARTIFACT_URL", ""),
    )

    signing_secret = os.getenv("LARK_SIGNING_SECRET")
    if signing_secret:
        timestamp = str(int(time.time()))
        digest = hmac.digest(f"{timestamp}\n{signing_secret}".encode(), b"", "sha256")
        payload.update(timestamp=timestamp, sign=base64.b64encode(digest).decode())

    try:
        response = httpx.post(webhook_url, json=payload, timeout=10)
    except httpx.RequestError as error:
        raise SystemExit(f"Lark 通知请求失败（{type(error).__name__}）") from None
    if not response.is_success:
        raise SystemExit(f"Lark 通知请求失败（HTTP {response.status_code}）")
    try:
        result = response.json()
    except ValueError:
        raise SystemExit("Lark 返回了无法解析的响应") from None
    code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
    if code != 0:
        raise SystemExit(f"Lark 拒绝通知（业务码 {code if type(code) is int else '未知'}）")
    print("Lark 已接受本轮报告卡片。")


if __name__ == "__main__":
    main()
