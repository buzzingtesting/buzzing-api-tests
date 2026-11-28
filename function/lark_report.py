"""把 Preview 的 JUnit 用例统计和报告链接发到 Lark。"""

import base64
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from function.reporting import junit_summary


def build_card(summary, outcome, run_url, artifact_url, *, report_view_url="", report_round=""):
    label = "测试通过" if outcome == "success" else "完成"
    title = f"Buzzing Preview 接口自动化 · {label}"
    if report_round:
        title += f"（{report_round}）"
    if report_view_url:
        report = f"[查看 Allure 测试报告]({report_view_url})"
    elif artifact_url:
        report = f"[下载 Allure 报告]({artifact_url})"
    else:
        report = "本轮暂无 Allure 报告"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if outcome == "success" else "blue",
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
    summary = junit_summary(Path("reports"), bold_total=True).replace("失败 ", "未通过 ")
    payload = build_card(
        summary, os.getenv("REPORT_OUTCOME", ""), run_url, os.getenv("REPORT_ARTIFACT_URL", ""),
        report_view_url=os.getenv("REPORT_VIEW_URL", ""),
        report_round=os.getenv("REPORT_ROUND", ""),
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
