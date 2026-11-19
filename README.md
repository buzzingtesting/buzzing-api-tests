# Buzzing 接口自动化

| 目录或文件 | 日常怎么用 |
|---|---|
| `function/` | 维护接口调用和可复用的方法。登录、取码和查会话从 `identity.py` 开始看。 |
| `testcase/` | 维护 CI 要跑的场景。一个 `test_*.py` 可以包含同一业务下的多个场景。共享 fixture 放在 `conftest.py`。 |
| `email_login.yaml` | 维护场景数据，包括邮箱、收件方式和会话预期。 |
| `run.py` | 运行 `testcase/` 并生成报告。 |

## 本地运行

在 PyCharm 右键运行 `run.py`，或在项目 Terminal 执行：

```sh
.venv/bin/python run.py
.venv/bin/python run.py --env prod
```

项目首次安装依赖时运行 `.venv/bin/python -m pip install -r requirements.txt`。默认跑 Preview 的 Gmail 数据组 `testdata1`。在 PyCharm 运行配置的参数中填写 `--env prod` 可切换到生产，也可以设置 `BUZZING_ENV=prod`。命令行参数优先。用 `BUZZING_TESTDATA` 可以指定同环境的其他数据组。

Gmail 应用专用密码从 `BUZZING_GMAIL_APP_PASSWORD` 或本机的 `.local/gmail_app_password` 读取。Mailosaur 使用 `MAILOSAUR_SERVER_ID` 和 `MAILOSAUR_API_KEY`。登录通过后，凭证保存到 `.local/<数据组>.auth.yaml`。

写场景时，匿名接口用 `api_client`，已登录接口用 `authenticated_api_client`。后者会复用当前环境、同组账号的 JWT。没有缓存、账号变更或 JWT 在 60 秒内到期时，fixture 自动收取验证码并重新登录，验证会话后更新缓存。生产凭证保存到 `.local/prod/`。

## 生产回归配置

- 在根目录 `email_login.yaml` 的 `prod_testdata` 填写已有的生产回归专用账号和验证码邮件标题。
- 配置生产的 `BUZZING_PROD_PRIVY_APP_ID`。如果该 Privy 应用配置了客户端 ID，再设置 `BUZZING_PROD_PRIVY_CLIENT_ID`。
- 生产 Mailosaur 使用 `PROD_MAILOSAUR_SERVER_ID` 和 `PROD_MAILOSAUR_API_KEY`。若将生产数据组改用 Gmail，配置 `BUZZING_PROD_GMAIL_APP_PASSWORD`。

生产运行只选中标记为 `@pytest.mark.pro` 的用例。生产客户端允许 GET、HEAD 和明确的 `POST /api/v1/login`。登录通过 Privy 验码获得本环境的新身份凭证。

## 报告和个人 GitHub CI

本轮结果保存到 `reports/run-*`。本机安装 Allure CLI 时，打开固定入口 `reports/allure-report/index.html` 可看最新一轮。概览里的 **环境** 显示当前环境、API 地址和数据组，历史报告在各轮目录里。

本项目可以独立放在个人 GitHub 的私有仓库中。首次在 **Actions → Buzzing API scenarios → Run workflow** 验证时，选择 `preview` 并保留默认勾选的 **Only verify API access**。这次只跑只读预检，可以直接下载 **preview-scenarios** 产物里的 `api-access.log`，用于核对 Runner 的 HTTPX 连通性。

CI 默认用 GitHub 托管的 `ubuntu-latest`。使用自有 Runner 时，可用仓库变量 `BUZZING_RUNNER_LABEL` 指定标签。每天北京时间 09:00 自动运行 Preview 场景。手动运行场景时，选择环境并取消勾选 **Only verify API access**。

Preview 场景结束后会发送邮件，包含本轮结果、GitHub Actions 运行链接和 Allure HTML 的下载链接。登录有仓库访问权限的 GitHub 账号下载，解压后打开 `index.html`。测试失败时同样发送，若未能生成 HTML，邮件会提供运行链接。

在仓库的 **Settings → Secrets and variables → Actions** 中配置以下值。

| 环境 | YAML 数据组 | 仓库 Variables | 仓库 Secrets |
|---|---|---|---|
| Preview 默认 | `testdata1` | `BUZZING_REPORT_FROM`、`BUZZING_REPORT_TO` | `BUZZING_GMAIL_APP_PASSWORD`，用于 Gmail 取码和发报告 |
| Preview 改用 Mailosaur 取码 | `testdata2` | `BUZZING_PREVIEW_TESTDATA=testdata2`、`MAILOSAUR_SERVER_ID` | `MAILOSAUR_API_KEY`，发送报告仍用 Gmail 的 Secret |
| Prod | `prod_testdata` | `PROD_MAILOSAUR_SERVER_ID`、`BUZZING_PROD_PRIVY_APP_ID`、按需设置 `BUZZING_PROD_PRIVY_CLIENT_ID` | `PROD_MAILOSAUR_API_KEY` |

本地用同一个入口预检，运行 `.venv/bin/python -m function.preview_access`。HTTPX 使用默认 User-Agent、8 秒超时、关闭自动重定向。预检覆盖匿名会话、包装响应成功码和未登录拒绝码。
