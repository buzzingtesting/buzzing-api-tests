# Buzzing 接口自动化

| 目录或文件 | 日常怎么用 |
|---|---|
| `function/` | 维护接口调用和可复用的方法。登录、取码和查会话从 `identity.py` 开始看。 |
| `testcase/` | 维护 CI 要跑的场景。一个 `test_*.py` 可以包含同一业务下的多个场景。共享 fixture 放在 `conftest.py`。 |
| `ci.yaml` | 维护测试账号，包括邮箱、收件方式和会话预期。 |
| `.github/workflows/check-framework.yml` | 配置 GitHub Actions 的触发时间、运行环境和报告邮件。 |
| `run.py` | 运行 `testcase/` 并生成报告。 |

## 本地运行

在 PyCharm 右键运行 `run.py`，或在项目 Terminal 执行：

```sh
.venv/bin/python run.py
.venv/bin/python run.py --env prod
```

项目首次安装依赖时运行 `.venv/bin/python -m pip install -r requirements.txt`。在 PyCharm 运行配置的参数中填写 `--env prod` 可切换到生产，也可以设置 `BUZZING_ENV=prod`。命令行参数优先。

## 按场景选择测试账号

在根目录 `ci.yaml` 维护账号。Preview 默认使用 `tester1`，也就是 `ddjking998@gmail.com`。新增账号时，复制 `tester1` 的 YAML 配置，改名为 `tester2`，再填写真实的测试邮箱和对应的收件配置。

某个场景固定使用 `tester2` 时，在现有测试函数上加这行。函数使用 `authenticated_api_client` 时，客户端会自动用该账号登录。

```python
@pytest.mark.parametrize("test_account", ["tester2"], indirect=True)
def test_my_scenario(authenticated_api_client):
    # 调用接口，断言业务结果。
    ...
```

同一场景要用两个账号分别跑，将列表写成 `["tester1", "tester2"]`。未指定账号的场景使用当前环境的默认账号。临时改变默认账号可设置 `BUZZING_TESTDATA`。Allure 会在每条使用账号的用例中显示实际账号名。

Gmail 的 `gmail_password_env` 填应用专用密码对应的变量名。现有 `tester1` 继续用 `BUZZING_GMAIL_APP_PASSWORD`，本机也可以读 `.local/gmail_app_password`。例如 Preview 新账号可用 `BUZZING_GMAIL_APP_PASSWORD_TESTER2`。在 PyCharm 的运行配置中设置它，或在 GitHub Actions 新增同名 Secret，并在工作流的 **Run scenario tests** 步骤把它传入同名环境变量。Mailosaur 使用 `MAILOSAUR_SERVER_ID` 和 `MAILOSAUR_API_KEY`。

匿名接口用 `api_client`，已登录接口用 `authenticated_api_client`。JWT 按账号保存在 `.local/<账号名>.auth.yaml`，例如 `.local/tester1.auth.yaml`。没有缓存、账号变更或 JWT 在 60 秒内到期时，fixture 自动收取验证码并重新登录，验证会话后更新缓存。生产凭证保存到 `.local/prod/`。

## 生产回归配置

- 在根目录 `ci.yaml` 的 `prod_testdata` 填写已有的生产回归专用账号和验证码邮件标题。
- 配置生产的 `BUZZING_PROD_PRIVY_APP_ID`。如果该 Privy 应用配置了客户端 ID，再设置 `BUZZING_PROD_PRIVY_CLIENT_ID`。
- 生产 Mailosaur 使用 `PROD_MAILOSAUR_SERVER_ID` 和 `PROD_MAILOSAUR_API_KEY`。若将生产数据组改用 Gmail，配置 `BUZZING_PROD_GMAIL_APP_PASSWORD`。

生产运行只选中标记为 `@pytest.mark.pro` 的用例。生产客户端允许 GET、HEAD 和明确的 `POST /api/v1/login`。登录通过 Privy 验码获得本环境的新身份凭证。

## 报告和个人 GitHub CI

本轮结果保存到 `reports/run-*`。本机安装 Allure CLI 时，打开固定入口 `reports/allure-report/index.html` 可看最新一轮。概览里的 **环境** 显示当前环境、API 地址和默认账号，历史报告在各轮目录里。

本项目可以独立放在个人 GitHub 的私有仓库中。首次在 **Actions → Buzzing API scenarios → Run workflow** 验证时，选择 `preview` 并保留默认勾选的 **Only verify API access**。这次只跑只读预检，可以直接下载 **preview-scenarios** 产物里的 `api-access.log`，用于核对 Runner 的 HTTPX 连通性。

CI 默认用 GitHub 托管的 `ubuntu-latest`。使用自有 Runner 时，可用仓库变量 `BUZZING_RUNNER_LABEL` 指定标签。每天北京时间 09:00 自动运行 Preview 场景。手动运行场景时，选择环境并取消勾选 **Only verify API access**。

Preview 场景结束后会发送邮件，包含本轮结果、GitHub Actions 运行链接和 Allure HTML 的下载链接。登录有仓库访问权限的 GitHub 账号下载，解压后打开 `index.html`。测试失败时同样发送，若未能生成 HTML，邮件会提供运行链接。

配置仓库 Secret `LARK_WEBHOOK_URL` 后，Preview 场景结束时也会向 Lark 群发送通过、失败、错误和跳过数，以及 Allure 和 Actions 链接。机器人开启签名校验时，再配置 `LARK_SIGNING_SECRET`。打开报告仍需要私有仓库访问权限。

在仓库的 **Settings → Secrets and variables → Actions** 中配置以下值。

| 环境 | YAML 数据组 | 仓库 Variables | 仓库 Secrets |
|---|---|---|---|
| Preview 默认 | `tester1` | `BUZZING_REPORT_FROM`、`BUZZING_REPORT_TO` | `BUZZING_GMAIL_APP_PASSWORD`，用于 Gmail 取码和发报告 |
| Preview 改用 Mailosaur 取码 | `testdata2` | `BUZZING_PREVIEW_TESTDATA=testdata2`、`MAILOSAUR_SERVER_ID` | `MAILOSAUR_API_KEY`，发送报告仍用 Gmail 的 Secret |
| Prod | `prod_testdata` | `PROD_MAILOSAUR_SERVER_ID`、`BUZZING_PROD_PRIVY_APP_ID`、按需设置 `BUZZING_PROD_PRIVY_CLIENT_ID` | `PROD_MAILOSAUR_API_KEY` |

本地用同一个入口预检，运行 `.venv/bin/python -m function.preview_access`。HTTPX 使用默认 User-Agent、8 秒超时、关闭自动重定向。预检覆盖匿名会话、包装响应成功码和未登录拒绝码。
