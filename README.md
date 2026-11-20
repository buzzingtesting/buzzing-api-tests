# Buzzing 接口自动化

[GitHub 仓库 buzzingtesting/buzzing-api-tests](https://github.com/buzzingtesting/buzzing-api-tests)

| 目录或文件 | 日常怎么用 |
|---|---|
| `function/` | 维护接口调用和可复用的方法。登录、取码和查会话从 `identity.py` 开始看。 |
| `testcase/` | 维护 CI 要跑的场景。一个 `test_*.py` 可以包含同一业务下的多个场景。共享 fixture 放在 `conftest.py`。 |
| [`ci.yaml`](ci.yaml) | 维护测试账号，包括邮箱、收件方式和会话预期。 |
| [`check-framework.yml`](.github/workflows/check-framework.yml) | 配置 GitHub Actions 的定时计划、运行环境和报告通知。 |
| `run.py` | 运行 `testcase/` 并生成报告。 |

## 本地运行

在 PyCharm 右键运行 `run.py`，也可以在项目根目录的 Terminal 执行以下命令。

```sh
.venv/bin/python run.py
.venv/bin/python run.py --env prod
```

项目首次安装依赖时运行 `.venv/bin/python -m pip install -r requirements.txt`。在 PyCharm 运行配置的参数中填写 `--env prod` 可切换到生产，也可以设置 `BUZZING_ENV=prod`。命令行参数优先。

## 新增和维护用例

1. **找对应模块**。在 `testcase/` 中修改已有的 `test_*.py`，新模块可以新建 `test_<模块>.py`。用例函数以 `test_` 开头。登录流程可参考 [邮箱验证码登录用例](testcase/test_email_login.py)。
2. **复用接口调用和 fixture**。公共请求使用 [`function/api_client.py`](function/api_client.py) 中的 `request()`。可复用的业务接口方法放在 `function/`，账号和客户端 fixture 放在 [`testcase/conftest.py`](testcase/conftest.py)。匿名场景用 `api_client`，已登录场景用 `authenticated_api_client`。
3. **按接口契约写断言**。原生 JSON 用 `expect_native()` 检查 HTTP 状态和 schema，包装响应 `EnvelopeReply` 用 `expect_envelope()` 检查 HTTP 状态和业务码。用 `allure.title` 和 `allure.step` 写清场景名称和业务步骤。 [响应校验方法](function/response_checks.py)
4. **在 Preview 验证**。先单跑修改的用例，再用 `run.py --env preview` 跑完整场景并查看 Allure。用例合入 `main` 后，由现有工作流在下次定时运行时收集。需参加生产回归的用例按下方生产回归规则添加标记。

下面是一个已登录用户查询会话的用例示例。它复用现有会话接口，并用 `ci.yaml` 中选定账号的预期校验响应。

```python
import allure

from function.identity import get_session
from function.response_checks import expect_native


@allure.title("已登录用户可查询会话")
def test_authenticated_session(authenticated_api_client, test_account):
    _, account = test_account
    with allure.step("查询并校验会话权限"):
        response = get_session(authenticated_api_client)
        expect_native(
            response,
            status=account["expected_session_status"],
            schema={
                "type": "object",
                "required": ["auth_level"],
                "properties": {
                    "auth_level": {"type": "integer", "enum": account["expected_auth_levels"]},
                },
            },
        )
```

**单跑现有用例**时，可以在 PyCharm 点击函数旁的运行按钮，或用以下命令查看结果。生成整套 Allure 报告时使用 `run.py --env preview`。

```sh
BUZZING_ENV=preview .venv/bin/python -m pytest testcase/test_email_login.py::test_email_login -v
```

## 按场景选择测试账号

在根目录 `ci.yaml` 维护账号。本地 Preview 默认使用 `tester1`，也就是 `ddjking998@gmail.com`。GitHub Actions 的 Preview 默认使用 `ci_tester`，也就是公司邮箱。新增账号时，复制 `tester1` 的 YAML 配置，改名为 `tester2`，再填写真实的测试邮箱和对应的收件配置。

两人共用一条场景时，可在各自的 PyCharm 运行配置里设置 `BUZZING_TESTDATA`，值填写本人在 `ci.yaml` 中的账号名。场景需要固定用 `tester2` 时，导入 `pytest` 并在测试函数上加 `@pytest.mark.parametrize("test_account", ["tester2"], indirect=True)`。同一场景要用两个账号分别验证时，把列表写成 `["tester1", "tester2"]`。Allure 会显示实际使用的账号名。

Gmail 的 `gmail_password_env` 填应用专用密码对应的变量名。现有 `tester1` 继续用 `BUZZING_GMAIL_APP_PASSWORD`，本机也可以读 `.local/gmail_app_password`。例如 Preview 新账号可用 `BUZZING_GMAIL_APP_PASSWORD_TESTER2`。在 PyCharm 的运行配置中设置它，或在 GitHub Actions 新增同名 Secret，并在工作流的 **Run scenario tests** 步骤把它传入同名环境变量。Mailosaur 使用 `MAILOSAUR_SERVER_ID` 和 `MAILOSAUR_API_KEY`。

匿名接口用 `api_client`，已登录接口用 `authenticated_api_client`。JWT 按账号保存在 `.local/<账号名>.auth.yaml`，例如 `.local/tester1.auth.yaml`。没有缓存、账号变更或 JWT 在 60 秒内到期时，fixture 自动收取验证码并重新登录，验证会话后更新缓存。生产凭证保存到 `.local/prod/`。

## 生产回归配置

- 在根目录 `ci.yaml` 的 `prod_testdata` 填写已有的生产回归专用账号和验证码邮件标题。
- 配置生产的 `BUZZING_PROD_PRIVY_APP_ID`。如果该 Privy 应用配置了客户端 ID，再设置 `BUZZING_PROD_PRIVY_CLIENT_ID`。
- 生产 Mailosaur 使用 `PROD_MAILOSAUR_SERVER_ID` 和 `PROD_MAILOSAUR_API_KEY`。若将生产数据组改用 Gmail，配置 `BUZZING_PROD_GMAIL_APP_PASSWORD`。

生产运行只选中标记为 `@pytest.mark.pro` 的用例。生产客户端允许 GET、HEAD 和明确的 `POST /api/v1/login`。登录通过 Privy 验码获得本环境的新身份凭证。

## GitHub Actions 运行计划

**定时执行**在 [`check-framework.yml`](.github/workflows/check-framework.yml) 的 `on.schedule` 维护。当前为 `0 9 * * *`，时区为 `Asia/Shanghai`，每天北京时间 **09:00** 计划触发 Preview 场景。修改后提交到默认分支 `main`。

**手动执行**在 [Actions → Buzzing API scenarios](https://github.com/buzzingtesting/buzzing-api-tests/actions/workflows/check-framework.yml) 点击 **Run workflow**，选择环境。保留 **Only verify API access** 会执行只读预检，取消勾选会继续执行场景并生成报告。

CI 默认用 GitHub 托管的 `ubuntu-latest`。仓库变量 `BUZZING_RUNNER_LABEL` 可指定 Runner 标签。Preview CI 默认从公司邮箱取验证码，使用 `ci.yaml` 中的 `ci_tester`。仓库变量 `BUZZING_PREVIEW_TESTDATA` 可切换 Preview CI 使用的账号。

## 查看报告和通知

- **本地**。每轮结果保存在 `reports/run-*`。本机安装 Allure CLI 后，可用浏览器打开 `reports/allure-report/index.html` 查看最新一轮。概览显示当前环境、API 地址和默认账号。
- **GitHub Actions**。打开运行记录的 **Artifacts**，下载 `preview-allure-report`，解压后在浏览器打开 `index.html`。预检日志、JUnit 和场景日志在 `preview-scenarios` 中。下载需要登录有私有仓库访问权限的 GitHub 账号。
- **邮件**。完整 Preview 场景结束后，公司邮箱 `dejian.ding@trufflelabs.ai` 向 `BUZZING_REPORT_TO` 指定的地址发送结果及报告链接。仓库变量 `BUZZING_REPORT_FROM` 保存发件地址。若收件箱没有邮件，可先检查运行记录的 **Email the Preview report** 步骤和收件邮箱的垃圾邮件。
- **Lark**。完整 Preview 场景结束后，机器人向测试群发送用例总数、通过、失败、错误和跳过数，并附上 Allure 和 Actions 链接。排查推送时查看 **Post the Preview report to Lark** 步骤。

Preview 测试失败也会发送通知。未生成 Allure HTML 时，可通过通知中的 Actions 链接查看运行日志。[组织仓库 CI 验证记录](https://github.com/buzzingtesting/buzzing-api-tests/actions/runs/44660760957)

## CI 变量和凭证

在仓库的 **Settings → Secrets and variables → Actions** 管理。密码、Webhook 和签名密钥填入 **Secrets**。

| 用途 | 仓库 Variables | 仓库 Secrets |
|---|---|---|
| Preview CI 登录 `ci_tester` | 默认读取 `ci.yaml` 的 `ci_tester` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| 报告邮件 | `BUZZING_REPORT_FROM`、`BUZZING_REPORT_TO` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| Lark 群推送 | 无 | `LARK_WEBHOOK_URL`，开启签名校验时另加 `LARK_SIGNING_SECRET` |
| Preview 改用 Mailosaur 取码 | `BUZZING_PREVIEW_TESTDATA=testdata2`、`MAILOSAUR_SERVER_ID` | `MAILOSAUR_API_KEY` |
| Prod | `PROD_MAILOSAUR_SERVER_ID`、`BUZZING_PROD_PRIVY_APP_ID`、按需设置 `BUZZING_PROD_PRIVY_CLIENT_ID` | `PROD_MAILOSAUR_API_KEY`，改用 Gmail 取码时配置 `BUZZING_PROD_GMAIL_APP_PASSWORD` |

本地用同一个入口预检，运行 `.venv/bin/python -m function.preview_access`。HTTPX 使用默认 User-Agent、8 秒超时、关闭自动重定向。预检覆盖匿名会话、包装响应成功码和未登录拒绝码。
