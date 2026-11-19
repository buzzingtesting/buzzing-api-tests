import os
import re
from datetime import datetime, timezone

from mailosaur import MailosaurClient
from mailosaur.models import MailosaurException, SearchCriteria

from function.settings import get_environment


def get_mailosaur_code(email, subject, send_code):
    """发码后，通过 Mailosaur 读取本轮邮件里的验证码。"""
    prefix = "PROD_" if get_environment() == "prod" else ""
    key_name = f"{prefix}MAILOSAUR_API_KEY"
    server_name = f"{prefix}MAILOSAUR_SERVER_ID"
    api_key = os.getenv(key_name, "").strip()
    server_id = os.getenv(server_name, "").strip()
    if not api_key or not server_id:
        raise RuntimeError(f"请配置 {key_name} 和 {server_name}")

    client = MailosaurClient(api_key)
    try:
        client.messages.list(server_id, page=0, items_per_page=1)
    except MailosaurException as error:
        raise RuntimeError(
            f"无法访问 Mailosaur Inbox，请检查配置（HTTP {error.http_status_code}）"
        ) from None

    criteria = SearchCriteria()
    criteria.sent_to = email
    criteria.subject = subject

    started_at = datetime.now(timezone.utc)
    send_code()
    try:
        message = client.messages.get(
            server_id, criteria, timeout=90_000, received_after=started_at
        )
    except MailosaurException as error:
        if error.error_type in {"no_messages_found", "search_timeout"}:
            raise RuntimeError("等待本轮 Mailosaur 验证码邮件超时") from None
        raise RuntimeError(
            f"Mailosaur 取码失败（HTTP {error.http_status_code}）"
        ) from None

    if message.subject != subject or message.received < started_at:
        raise RuntimeError("Mailosaur 返回的邮件不匹配本轮发码")

    # 同一验证码可能同时出现在纯文本和 HTML 邮件体中。
    codes = {
        code.value
        for part in (message.text, message.html)
        for code in part.codes
        if re.fullmatch(r"[0-9]{6}", code.value or "")
    }
    if len(codes) != 1:
        raise RuntimeError("Mailosaur 邮件中没有唯一的六位验证码")
    return codes.pop()
