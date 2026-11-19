import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from function.settings import get_environment


TOKEN_DIR = Path(__file__).resolve().parents[1] / ".local"


class CachedLoginUnavailable(RuntimeError):
    """当前账号没有可用的缓存凭证。"""


def _token_path(case_name):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", case_name):
        raise ValueError("测试数据名称只能包含字母、数字、下划线和连字符")
    directory = TOKEN_DIR / "prod" if get_environment() == "prod" else TOKEN_DIR
    return directory / f"{case_name}.auth.yaml"


def save_tokens(case_name, email, login, privy_identity_token):
    path = _token_path(case_name)
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    data = {
        "email": email,
        "buzzing_jwt": login["token"],
        "privy_identity_token": privy_identity_token,
        "expires_at": login["expire"],
        "user_id": login["user_id"],
    }
    # 临时文件默认权限为 0600，写完后再替换本组数据的旧文件。
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as pending:
        temp_path = Path(pending.name)
        try:
            yaml.safe_dump(data, pending, sort_keys=False)
            pending.close()
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def load_buzzing_jwt(case_name, email, *, min_validity_seconds=0):
    path = _token_path(case_name)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        token = data["buzzing_jwt"]
        expires_at = datetime.fromisoformat(data["expires_at"])
        cached_email = data["email"]
    except (FileNotFoundError, yaml.YAMLError, KeyError, TypeError, ValueError):
        raise CachedLoginUnavailable("本组账号没有有效的凭证缓存") from None

    valid_until = datetime.now(timezone.utc) + timedelta(seconds=min_validity_seconds)
    if cached_email != email or not isinstance(token, str) or not token:
        raise CachedLoginUnavailable("缓存凭证与当前测试数据不匹配")
    if expires_at.tzinfo is None or expires_at <= valid_until:
        raise CachedLoginUnavailable("Buzzing JWT 已过期或即将过期")
    return token
