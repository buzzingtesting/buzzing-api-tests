import os
from pathlib import Path

import yaml


BASE_URLS = {
    "preview": "https://preview-api.buzzing.app",
    "prod": "https://prod-api.buzzing.app",
}
WEB_ORIGINS = {
    "preview": "https://preview.buzzing.app",
    "prod": "https://www.buzzing.app",
}
DEFAULT_TESTDATA = {"preview": "ci_tester", "prod": "prod_testdata"}
DATA_FILE = Path(__file__).resolve().parents[1] / "ci.yaml"


def load_test_data():
    return yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))


def get_environment():
    environment = os.getenv("BUZZING_ENV", "preview")
    if environment not in BASE_URLS:
        raise ValueError("BUZZING_ENV 只接受 preview 或 prod")
    return environment


def full_preview_required():
    return get_environment() == "preview" and os.getenv("BUZZING_REQUIRE_FULL_PREVIEW") == "1"


def get_testdata():
    environment = get_environment()
    account_name = os.getenv("BUZZING_TESTDATA", DEFAULT_TESTDATA[environment])
    if environment == "preview" and account_name == "testdata1":
        return "tester1"
    return account_name


def get_test_account(test_data):
    name = get_testdata()
    account = test_data.get(name) if isinstance(test_data, dict) else None
    if not isinstance(account, dict) or name == "market_cases":
        raise ValueError(f"ci.yaml 中没有账号 {name}")
    check_account_environment(account)
    return name, account


def check_account_environment(account):
    if account.get("environment", "preview") != get_environment():
        raise ValueError("数据组和运行环境不匹配，请检查 ci.yaml")
