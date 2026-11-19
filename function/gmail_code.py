import imaplib
import os
import re
import ssl
import sys
import time
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from getpass import getpass
from html.parser import HTMLParser
from pathlib import Path

from function.settings import get_environment


GMAIL_PASSWORD_FILE = Path(__file__).resolve().parents[1] / ".local" / "gmail_app_password"


class _EmailText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


@contextmanager
def open_gmail(email_address, app_password):
    """用测试邮箱的应用专用密码连接 Gmail，收件箱以只读方式打开。"""
    with imaplib.IMAP4_SSL(
        "imap.gmail.com", ssl_context=ssl.create_default_context(), timeout=15
    ) as mailbox:
        try:
            mailbox.login(email_address, app_password)
        except imaplib.IMAP4.error:
            raise RuntimeError("Gmail 登录失败，请检查该测试邮箱的应用专用密码") from None
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法以只读方式打开 Gmail 收件箱")
        yield mailbox


def next_mail_uid(mailbox):
    """发码前记录 UID，后续只读取本轮开始后收到的邮件。"""
    _, values = mailbox.response("UIDNEXT")
    if not values or not values[0]:
        raise RuntimeError("Gmail 未返回收件箱 UIDNEXT")
    return int(values[0])


def _code_from_message(raw_message, subject):
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if str(message["Subject"]) != subject:
        return None

    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return None
    content = body.get_content()
    if body.get_content_type() == "text/html":
        parser = _EmailText()
        parser.feed(content)
        content = " ".join(parser.parts)
    codes = set(re.findall(r"(?<![0-9])[0-9]{6}(?![0-9])", content))
    return codes.pop() if len(codes) == 1 else None


def wait_for_code(mailbox, first_uid, email_address, subject, *, timeout=90):
    # IMAP 字符串参数加引号，便于处理带空格的邮件标题。
    def quoted(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    deadline = time.monotonic() + timeout
    checked = set()
    while time.monotonic() < deadline:
        # 让 IMAP 会话先接收新邮件通知，再搜索本轮 UID。
        status, _ = mailbox.noop()
        if status != "OK":
            raise RuntimeError("无法刷新 Gmail 收件箱")
        status, data = mailbox.uid(
            "SEARCH", None, "UID", f"{first_uid}:*",
            "TO", quoted(email_address), "SUBJECT", quoted(subject),
        )
        if status != "OK":
            raise RuntimeError("Gmail 验证码邮件查询失败")
        uids = sorted(map(int, (data[0] or b"").split()), reverse=True)
        for uid in uids:
            if uid < first_uid or uid in checked:
                continue
            status, messages = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("Gmail 验证码邮件读取失败")
            for part in messages:
                if isinstance(part, tuple):
                    code = _code_from_message(part[1], subject)
                    if code:
                        return code
            checked.add(uid)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError("等待本轮验证码邮件超时，请检查收件箱和 YAML 中的邮件标题")


def get_app_password(email):
    environment = get_environment()
    variable = "BUZZING_PROD_GMAIL_APP_PASSWORD" if environment == "prod" else "BUZZING_GMAIL_APP_PASSWORD"
    app_password = os.getenv(variable, "")
    if not app_password and environment == "preview" and GMAIL_PASSWORD_FILE.is_file():
        saved_email, _, saved_password = GMAIL_PASSWORD_FILE.read_text(
            encoding="utf-8"
        ).partition("\n")
        if saved_email.casefold() == email.casefold():
            app_password = saved_password
    if not app_password and sys.stdin.isatty():
        app_password = getpass("请输入 Gmail 应用专用密码：")
    app_password = "".join(app_password.split())
    if not app_password:
        raise RuntimeError(f"请配置 {variable}")
    return app_password


def get_gmail_code(email, subject, send_code):
    with open_gmail(email, get_app_password(email)) as inbox:
        first_uid = next_mail_uid(inbox)
        send_code()
        return wait_for_code(inbox, first_uid, email, subject)
