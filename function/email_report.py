import os
import smtplib
import ssl
from email.message import EmailMessage


def main():
    sender = os.getenv("BUZZING_REPORT_FROM", "").strip()
    recipient = os.getenv("BUZZING_REPORT_TO", "").strip()
    password = "".join(os.getenv("BUZZING_GMAIL_APP_PASSWORD", "").split())
    if not sender or not recipient or not password:
        raise SystemExit("请配置报告发件人、收件人和 Gmail 应用专用密码")

    run_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    report_url = os.getenv("REPORT_ARTIFACT_URL")
    outcome = {"success": "通过", "failure": "失败"}.get(
        os.getenv("REPORT_OUTCOME"), "场景未执行"
    )
    report_line = (
        f"Allure HTML 下载链接\n{report_url}\n登录 GitHub，解压后打开 index.html。"
        if report_url else
        "本轮没有生成可下载的 Allure HTML，请查看 GitHub Actions 运行记录。"
    )
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"[Buzzing Preview] 接口自动化 {outcome}"
    message.set_content(f"测试结果 {outcome}\n\n{report_line}\n\n运行记录\n{run_url}\n")

    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=20, context=ssl.create_default_context()
        ) as server:
            server.login(sender, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        raise SystemExit(f"Gmail 报告发送失败（{type(error).__name__}）") from None
    print("Gmail 已接受本轮报告邮件")


if __name__ == "__main__":
    main()
