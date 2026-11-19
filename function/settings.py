import os


BASE_URLS = {
    "preview": "https://preview-api.buzzing.app",
    "prod": "https://prod-api.buzzing.app",
}
WEB_ORIGINS = {
    "preview": "https://preview.buzzing.app",
    "prod": "https://www.buzzing.app",
}
DEFAULT_TESTDATA = {"preview": "testdata1", "prod": "prod_testdata"}


def get_environment():
    environment = os.getenv("BUZZING_ENV", "preview")
    if environment not in BASE_URLS:
        raise ValueError("BUZZING_ENV 只接受 preview 或 prod")
    return environment


def get_testdata():
    return os.getenv("BUZZING_TESTDATA", DEFAULT_TESTDATA[get_environment()])


def check_account_environment(account):
    if account.get("environment", "preview") != get_environment():
        raise ValueError("数据组和运行环境不匹配，请检查 email_login.yaml")
