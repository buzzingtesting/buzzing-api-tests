# Buzzing 接口自动化

[GitHub 仓库 buzzingtesting/buzzing-api-tests](https://github.com/buzzingtesting/buzzing-api-tests)

| 目录或文件 | 日常怎么用 |
|---|---|
| `function/` | 维护接口调用和可复用的方法。登录、取码和查会话从 `identity.py` 开始看。 |
| `testcase/` | 按业务目录维护查询和交易场景。共享 fixture 放在 `conftest.py`。 |
| [`ci.yaml`](ci.yaml) | 维护测试账号和市场场景参数。 |
| [`check-framework.yml`](.github/workflows/check-framework.yml) | 配置 GitHub Actions 的定时计划、运行环境和报告通知。 |
| `run.py` | 运行日常场景并生成报告。传入 `--trade-run` 时加入指定的 Preview 受控交易。 |
| [Preview 接口自动化一期依赖交接](docs/preview-api-automation-dependencies.md) | 查阅开发提供的依赖 4–7，涵盖市场与资金、API 契约、Rewards 发奖、争议与结算。 |
| [M06 Rewards 接口自动化](docs/m06-rewards.md) | 运行 Preview 好友进度模拟、普通任务、真实跨日和 Credit 对账。 |

## 本地运行

在 PyCharm 右键运行 `run.py`，也可以在项目根目录的 Terminal 执行以下命令。

```sh
.venv/bin/python run.py
.venv/bin/python run.py --env prod
```

项目首次安装依赖时运行 `.venv/bin/python -m pip install -r requirements.txt`。在 PyCharm 运行配置的参数中填写 `--env prod` 可切换到生产，也可以设置 `BUZZING_ENV=prod`。命令行参数优先。

## 新增和维护用例

[A2 Preview 验收记录](docs/a2-acceptance.md)汇总本地测试、后端补验和接口契约。

1. **找对应模块**。在 `testcase/` 的业务目录中修改或新增 `test_*.py`。用例函数以 `test_` 开头。登录流程可参考 [邮箱验证码登录用例](testcase/auth/test_email_login.py)。
2. **复用接口调用和 fixture**。公共请求使用 [`function/api_client.py`](function/api_client.py) 中的 `request()`。可复用的业务接口方法放在 `function/`，账号和客户端 fixture 放在 [`testcase/conftest.py`](testcase/conftest.py)。匿名场景用 `api_client`，已登录场景用 `authenticated_api_client`。
3. **按接口契约写断言**。原生 JSON 用 `expect_native()` 检查 HTTP 状态和 schema，包装响应 `EnvelopeReply` 用 `expect_envelope()` 检查 HTTP 状态和业务码。用 `allure.title` 和 `allure.step` 写清场景名称和业务步骤。 [响应校验方法](function/response_checks.py)
4. **在 Preview 验证**。先单跑修改的用例，再用 `run.py --env preview` 跑日常套件并查看 Allure。日常用例合入 `main` 后由定时工作流收集。受控交易使用 `--trade-run`，生产回归使用下方的标记规则。

所有接口场景都维护在 `testcase/`，使用真实 Preview API 响应做断言。交易用例需要对应的市场、账号和资金等测试数据。M06 Rewards 通过 `--m06-operation` 单独选择操作。

场景所需的筛选条件和查询参数按模块写在 `market_cases` 段。BTC 1h、XRP 日档的系列名用于指定覆盖的市场类型，每次从 Preview 回包获取市场 ID 和游标。电竞 Props 的联赛由本轮导航返回。 [场景配置](ci.yaml)

| 用例目录 | Allure 模块 | 场景 |
|---|---|---|
| [auth](testcase/auth) | 身份与登录 | 邮箱验证码、错误验证码、匿名及已登录会话。 |
| [market_list](testcase/market_list) | 市场 | 列表排序、游标分页、分类和市场详情。按市场类型从本轮列表选样，核对事件或比赛详情。 |
| [orderbook](testcase/orderbook) | Orderbook 盘口 | 盘口事件、报价和深度、账户资金、购买力、交易接口鉴权、无效计划、签名与过期方案。 |
| [market_order](testcase/market_order) | 市价单 | 自营与 Polymarket 市价买卖、分两笔卖出、成交确认、持仓和资金对账。 |
| [limit_order](testcase/limit_order) | 限价单 | 限价买卖、同计划重放、零成交撤单、部分成交撤余单、Cash/Credit 出资、余额与份额不足、本人历史和逻辑订单筛选。 |
| [position](testcase/position) | Position 持仓 | 查询未平和已关闭持仓，校验账号归属、字段、分页和空列表。 |
| [settlement](testcase/settlement) | 市场争议与结算 | 日常核对结算历史、已结算持仓的领取状态，以及争议中市场的再次发起资格。公示窗内的争议资格用例在发版回归时手动运行。 |
| [rewards](testcase/rewards) | Rewards | M06 好友进度检查与模拟、日期工具准备与测试发奖、已完成场景重置、普通任务、好友绑定、真实跨日和重投对账。 [运行说明](docs/m06-rewards.md) |
| [M06 Rewards 离线用例](offline_tests) | — | 覆盖好友进度、普通任务、跨日、测试发奖、Credit 与 Inbox 对账、重放幂等和续跑。执行 `python -m pytest offline_tests -q`。 |

市场详情和盘口用例在 Allure 的选样步骤附上「本轮选中市场」，记录事件标题和 ID，以及该场景使用的比赛或市场 ID。

登录后进入市场详情会按所选子市场的 `condition_id` 和 `position_id` 查询本人持仓及在挂限价单。多子市场场景抽样一个子市场，持仓和挂单都允许为空，Allure 会记录数量。

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
BUZZING_ENV=preview .venv/bin/python -m pytest testcase/auth/test_email_login.py::test_email_login -v
```

## 按场景选择测试账号

在根目录 `ci.yaml` 维护账号。本地和 GitHub Actions 的 Preview 默认都使用公司邮箱测试账号 `ci_tester`。原 `tester1` 可通过 `BUZZING_TESTDATA=tester1` 显式选择。新增账号时可参考 `tester1` 的收件配置。

每轮需要登录的用例使用同一个账号，包括自营交易、Polymarket 和争议资格。账号由 `BUZZING_TESTDATA` 选择。在 GitHub Actions 中通过仓库变量 `BUZZING_PREVIEW_TESTDATA` 配置，默认 `ci_tester`。Allure 会显示实际使用的账号名。

Gmail 的 `gmail_password_env` 填应用专用密码对应的变量名。本地 `ci_tester` 优先使用 `BUZZING_GMAIL_APP_PASSWORD_CI`，也可读取权限为 `0600` 的 `.local/gmail_app_password_ci`，首行是账号邮箱，次行是应用专用密码。`tester1` 使用 `BUZZING_GMAIL_APP_PASSWORD` 或 `.local/gmail_app_password`。GitHub Actions 通过同名 Secrets 注入凭证。Mailosaur 使用 `MAILOSAUR_SERVER_ID` 和 `MAILOSAUR_API_KEY`。

配置了 `expected_uid` 时，邮箱登录和交易钱包读取都核对返回的 UID。

匿名接口用 `api_client`，已登录接口用 `authenticated_api_client`。JWT 按账号保存在 `.local/<账号名>.auth.yaml`，例如 `.local/tester1.auth.yaml`。没有缓存、账号变更或 JWT 在 60 秒内到期时，fixture 自动收取验证码并重新登录，验证会话后更新缓存。生产凭证保存到 `.local/prod/`。

## 生产回归配置

- 在根目录 `ci.yaml` 的 `prod_testdata` 填写已有的生产回归专用账号和验证码邮件标题。
- 配置生产的 `BUZZING_PROD_PRIVY_APP_ID`。如果该 Privy 应用配置了客户端 ID，再设置 `BUZZING_PROD_PRIVY_CLIENT_ID`。
- 生产 Mailosaur 使用 `PROD_MAILOSAUR_SERVER_ID` 和 `PROD_MAILOSAUR_API_KEY`。若将生产数据组改用 Gmail，配置 `BUZZING_PROD_GMAIL_APP_PASSWORD`。

生产运行只选中标记为 `@pytest.mark.pro` 的用例。生产客户端允许 GET、HEAD 和明确的 `POST /api/v1/login`。登录通过 Privy 验码获得本环境的新身份凭证。

## GitHub Actions 运行计划

**定时执行**在 [`check-framework.yml`](.github/workflows/check-framework.yml) 的 `on.schedule` 维护。每天北京时间 **18:00** 触发 Preview 回归，同一轮在常规回归和补跑结束后执行 M06。M06 运行离线测试，通过日期工具为 `tester1` 实际发放一笔 200 cents 的 Preview 测试 Credit，对账到账与 Inbox 通知，并保存用户端快照。修改后提交到默认分支 `main`。

**手动执行**在 [Actions → Buzzing API scenarios](https://github.com/buzzingtesting/buzzing-api-tests/actions/workflows/check-framework.yml) 点击 **Run workflow**。默认使用 Preview 和 `suite=regression`，三个争议市场 ID 都留空时执行常规回归。Production 回归可用 `access_only` 只做预检。

**发版争议回归**使用仍在公示窗内的 Preview 自营市场。`dispute_market_id` 填 CI 账号有持仓的市场，`dispute_no_position_market_id` 填同一账号无持仓的市场。Allure 分别显示匿名、已登录有持仓和已登录无持仓的争议资格。各场景按对应市场 ID 执行，留空时记录跳过原因。

日常 Preview CI 会从当前账号的自营持仓里选择一个处于第一轮或第二轮争议的市场，核对争议入口禁用、拒绝原因和对应轮次的历史记录。没有匹配市场，或接口结果不符合预期时，Allure 会记录跳过及原因。匿名、有持仓和无持仓的公示窗资格用例仍在发版回归时手动执行。

手动指定 `disputed_market_id` 时，用例对该市场做严格校验。三个市场输入可以分别填写，也可以同轮填写。运行结果见该轮的 Actions Artifacts。

领取状态用例检查当前账号最近关闭的 5 条持仓，不足 5 条时检查实际返回的记录。有兑付金额时接受 `received`（已到账）和 `pending`（待兑付），零兑付应为 `none`，两类记录的 `can_claim` 都应为 `false`。其他结束原因直接通过。Allure 附件列出抽样数量、实际状态、允许的状态和失败原因。

本地执行时，将占位符换成对应的市场 ID。

```sh
.venv/bin/python run.py --env preview --dispute-market-id POSITION_MARKET_ID \
  --dispute-no-position-market-id NO_POSITION_MARKET_ID
.venv/bin/python run.py --env preview --disputed-market-id DISPUTED_MARKET_ID
```

手动运行 M06 时，选择 Preview 和 `suite=m06_rewards`，取消勾选 `access_only`，三个争议市场 ID 留空。M06 使用 `tester1` 的现有 Gmail Secret 登录。手动场景准备、发奖和重置通过 `m06_operation` 选择，阶段记录保存在 `m06-date-test` 产物中，通过 `m06_resume_run` 恢复原轮次。 [M06 运行说明](docs/m06-rewards.md)

常规 CI 默认使用 GitHub 托管的 `ubuntu-24.04`，与钱包签名所需的 Playwright 版本兼容。签名依赖无法安装或启动时，自营零成交撤单用例会在报告中记录为跳过，其余用例照常执行。M06 Rewards 默认使用 `ubuntu-latest`。仓库变量 `BUZZING_RUNNER_LABEL` 可指定 Runner 标签。Preview CI 默认从公司邮箱取验证码，使用 `ci.yaml` 中的 `ci_tester`。仓库变量 `BUZZING_PREVIEW_TESTDATA` 可切换 Preview CI 使用的账号。

每轮 CI 从空的登录缓存开始，本轮 JWT 存在独立临时目录，场景执行结束时清理。CI 禁用 pytest 缓存，Allure、JUnit 和脱敏日志通过 Artifacts 查看。

## 查看报告和通知

- **本地**。每轮结果保存在 `reports/run-*`。本机安装 Allure CLI 后，可用浏览器打开 `reports/allure-report/index.html` 查看最新一轮。概览显示当前环境、API 地址和默认账号。
- **GitHub Actions**。打开运行记录的 **Artifacts**，点击 `preview-allure-report.html` 在浏览器中查看单文件报告。`preview-allure-report` 提供 ZIP 下载。预检日志、JUnit 和场景日志在 `preview-scenarios` 中。自动补跑的产物名称带 `-retry`。查看和下载都需要登录有私有仓库访问权限的 GitHub 账号。
- **M06 定时结果**。在同一轮 Actions 的 `preview-m06-rewards` 产物查看离线、发奖、重置与快照的 JUnit 和日志。对账批次、通知和续跑记录保存在 `m06-date-test-scheduled-*` 产物中。
- **邮件**。完整 Preview 场景结束后，公司邮箱 `dejian.ding@trufflelabs.ai` 向 `BUZZING_REPORT_TO` 指定的地址发送结果及报告链接。仓库变量 `BUZZING_REPORT_FROM` 保存发件地址。若收件箱没有邮件，可先检查运行记录的 **Email the Preview report** 步骤和收件邮箱的垃圾邮件。
- **Lark**。每轮常规 Preview 运行结束后，两个群机器人都会发送结果卡片，覆盖定时、手动和自动补跑。卡片包含用例统计、「查看 Allure 测试报告」和 Actions 链接。在线报告上传失败时提供 ZIP 下载入口。自动补跑的卡片标注「自动补跑」。排查推送时查看对应的 Lark 通知步骤。

完整 Preview 首轮有失败用例时，GitHub Actions 用该轮记录的 pytest 用例 ID 补跑一次。补跑使用新登录缓存，并独立上传报告、发送邮件和通知。已尝试提交或撤单的用例，以及依赖首轮买单的用例，会在补跑报告中记录跳过原因，按首轮订单标识回查。本地用 `run.py --retry-cases <首轮报告目录>/retry-cases.json` 指定失败清单，受控交易用例同时传入本轮对应的 `--trade-run` 参数。

Preview 的 `orders/submit` 若返回 `500 / INTERNAL / 1002` 且提示提交结果未知或 `Could not add funds. Please try again.`，当前用例结束后中断本轮 CI。Allure 在触发用例下显示「测试环境测试金额不足（疑似）」并附上原始错误信息和 `plan_id`。结果未知时先按原单回查，避免重提。`Could not add funds` 会标注 Polymarket pUSD 补资未就绪。

## CI 变量和凭证

在仓库的 **Settings → Secrets and variables → Actions** 管理。密码、Webhook 和签名密钥填入 **Secrets**。

| 用途 | 仓库 Variables | 仓库 Secrets |
|---|---|---|
| Preview CI 登录 `ci_tester` | 默认读取 `ci.yaml` 的 `ci_tester` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| 报告邮件 | `BUZZING_REPORT_FROM`、`BUZZING_REPORT_TO` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| Lark 群推送 | 无 | `LARK_WEBHOOK_URL`，开启签名校验时另加 `LARK_SIGNING_SECRET` |
| Lark 第二群推送 | 无 | `LARK_SCHEDULED_WEBHOOK_URL`，开启签名校验时另加 `LARK_SCHEDULED_SIGNING_SECRET` |
| Preview 改用 Mailosaur 取码 | 先在 `ci.yaml` 的 `testdata2` 填 Mailosaur 测试邮箱，再配置 `BUZZING_PREVIEW_TESTDATA=testdata2` 和 `MAILOSAUR_SERVER_ID` | `MAILOSAUR_API_KEY` |
| Prod | `PROD_MAILOSAUR_SERVER_ID`、`BUZZING_PROD_PRIVY_APP_ID`、按需设置 `BUZZING_PROD_PRIVY_CLIENT_ID` | `PROD_MAILOSAUR_API_KEY`，改用 Gmail 取码时配置 `BUZZING_PROD_GMAIL_APP_PASSWORD` |

本地用同一个入口预检，运行 `.venv/bin/python -m function.preview_access`。HTTPX 使用默认 User-Agent、8 秒超时、关闭自动重定向。预检覆盖匿名会话、包装响应成功码和未登录拒绝码。

## M03 Preview 自有订单簿

[自营限价下单与撤单](testcase/limit_order/test_limit_order.py)使用本轮配置的 CI 账号在 Preview 自有订单簿逐笔执行。Cash 场景可在运行配置里填 `market_ids`。执行时从本轮市场列表里随机选择一笔符合条件的获准二元市场，核对 NO 选项、盘口深度和账户可用数据。每份 `--trade-run` 只选中它指定的交易场景。

下表为显式传入 `--trade-run` 时的受控配置。日常 CI 的动态选样规则见下文。

| `scenario` | pytest 用例 | 数据和主要断言 |
|---|---|---|
| `limit_buy`（旧配置默认值） | `test_limit_buy_no` | `BUY LIMIT`，`limit=0.88`，单笔含费预算 `budget_raw=2000000`。用 Cash 或纯 Credit，核对逻辑订单、累计成交与剩余量、手续费、持仓、余额及限价单历史。 |
| `limit_cancel` | `test_own_limit_cancel_restores_available_cash` | 2 U Cash 限价买单，提交时启用 `post_only`。按限价和未成交份额核对冻结额与 `buying-power.balance` 的减少量。撤销后核对原单零成交终态、冻结释放和可用余额恢复。 |
| `limit_partial_cancel` | `test_limit_partial_cancel_no` | `BUY LIMIT`、Cash。配置 `max_budget_raw` 后从实时列表选自营盘口，记录最新成交价，按卖一价和该档总份额计算 Amount，再加 2 U。观察到本单部分成交后撤销余单，并核对成交、费用、持仓、解冻和历史记录。 |

USD/USDB 价格使用每份的小数，例如 `0.88` 等于 88 cents。资金、成交量、份额和手续费按 6 位 raw 整数比较。

限价面板初始化按交易交付原型计算预期值。先将所选方向的报价四舍五入到整百分点，买入取卖一减 1 个百分点，卖出取买一加 1 个百分点。默认值范围为 1% 至 99%，对应侧无报价时预期为空。日志与 Allure 记录同一份盘口的报价和两个预期值，供页面校验使用。实际限价单价格取本轮获批配置。

B3 专用数据、M04/M05 场景与验收证据见 [B3 Preview 验收清单](docs/b3-acceptance.md)。

**自营 Cash 的纯 API 路径。** Privy 邮箱验码返回的身份 Token 通过 `POST /api/v1/login` 换成 Buzzing JWT。后续使用 Buzzing JWT 和登录账号绑定的交易钱包。自营 `prepare` 返回 `cash_signing=server` 时，提交本次 `plan_id` 和空的 `cash_signature`，再按原单核对成交及资金持仓。若 Cash 计划仍要求客户端签名，纯 API 用例会在准备单阶段报告具体原因。

**自营零成交撤单的钱包签名。** 该用例在独立的 Chromium 会话中通过 Privy SDK 登录当前 CI 账号，对比实际绑定的 maker 和 signer，并签署当次 `prepare` 返回的 Cash 订单。签名前校验链、合约、价格、数量、预算和订单哈希，签名后核对恢复出的地址。Actions 会为该用例构建签名页并安装 Chromium。本地执行前安装 `privy_signer/requirements.txt`、执行 `python -m playwright install chromium --only-shell`，再用 pnpm 11.25.0 运行 `pnpm --dir privy_signer install --frozen-lockfile` 和 `pnpm --dir privy_signer run build`。

如果 macOS 本地运行器的沙盒限制导致 Chromium 无法启动，可为本地运行设置 `BUZZING_PREVIEW_CHROMIUM_SINGLE_PROCESS=1`。GitHub 托管的 Ubuntu Runner 使用 Chromium 默认启动方式。

显式指定的自营纯 Credit 场景通过 `BUZZING_TRADE_BACKEND` 加载对账支持库，checkout 固定到 `04bdad8214319d1d7a8a8d4651c5e1219f94992a`。场景使用本轮 CI 账号登录，批次也必须分配给该账号。

**自营限价日常 CI。** 首轮从 Volume 首页返回的活跃自营事件随机选样，并选择该事件累计成交量 `volume_clob` 最高的子市场。全成使用 3 U 含费预算，以所选方向最新成交价按价格档位取整为限价。部分成交使用最新成交价对应的限价，将限价以内全部可吃卖盘的金额加 1 U，默认单笔上限为 5 U。缺少符合条件的盘口时，报告记录跳过原因。

全成和部分成交场景提交前复核盘口和准备单的份额，成交后按档位核对本单吃掉的委托量，并校验订单历史、持仓及资金。部分成交后撤销本单的剩余委托。零成交撤单场景在卖一以下挂单，Allure 记录挂单前、挂单后与撤单后的冻结额和可用余额。

Actions 可通过 `BUZZING_OWN_TRADE_RUNS` 为 `select_from_volume=true` 的场景指定执行窗口，也可通过 `BUZZING_OWN_PARTIAL_MAX_BUDGET_RAW` 调整日常部分成交的预算上限。

**运行参数。** 将模板复制到忽略目录 `.local/`，为每轮使用单独文件。填入新窗口和独立的 `run_id`。UID、maker 和 signer 从本轮 CI 账号的真实会话获取。Cash 的 `batch_id` 留空。纯 Credit 填获准、未动用且未过期的真实批次 ID。共用盘口的执行者须共享同一锁目录，或在调度侧串行。

```json
{
  "environment": "preview", "run_id": "<本轮唯一 UUID>",
  "scenario": "limit_buy", "funding": "cash", "batch_id": "",
  "start": "<含时区的窗口开始时间>", "end": "<含时区的窗口结束时间>",
  "venue": "buzzing", "market_id": "4828026", "outcome": "NO",
  "side": "BUY", "order_type": "LIMIT", "limit": "0.88", "budget_raw": "2000000"
}
```

Cash 动态选样时，用 `"market_ids": ["<候选市场 ID>", "<候选市场 ID>"]` 替换固定的 `market_id`，按实际场景填价格参数。限价成交场景使用本轮批准的价格。卖出候选须已有足额可卖持仓，部分成交撤单候选须有能形成部分成交的卖盘。纯 Credit 沿用固定盘口路径。

**B3 Polymarket BTC 市价买入。** CI 从当轮 Volume 列表中随机选择标题含 BTC 的活跃二元循环市场，并随机尝试 UP 或 DOWN。首选方向卖盘不足时预检另一方向。用例核对盘口、购买力和代签授权后提交一笔 1.5 含费市价买单，按 `logical_order_ids` 最多轮询 60 秒，确认全成后立即继续。通过后在 Allure 的 **BTC 成交详情** 附件查看市场、周期、方向、预算、成交金额与份额、订单 ID、链上状态和确认耗时。

**B3 Polymarket BTC 市价卖出。** 使用同一轮已确认的买单，在原市场和原方向先市价卖出本轮购买份额的 50%，再根据刷新后的 Cash 持仓卖出本轮剩余可卖份额。每笔分别最多等待 60 秒确认全成，持仓核对保留买入前原有的份额。Polymarket 市价卖出份额向下取到 0.01 份，零头会记入 Allure 的「BTC 卖出后持仓核对」。两笔成交详情分别附在卖出用例下。

当 Preview 的开放持仓省略少于 0.01 份的卖出零头时，最终对账接受持仓回到买入前的数量。Allure 分别显示按订单计算的零头和开放持仓返回值。

交易中确认 BTC 已关闭或进入结算时，从最新 Volume 列表随机选择仍可交易的 ETH 二元循环市场，按相同预算和规则重新执行一次买入及两次卖出。报告保留原市场、回退触发阶段、已成交订单和 ETH 新订单。订单提交后 60 秒内仍未确认时，将当前用例记为跳过，停止依赖该单的后续卖出，并在 Allure 记录逻辑订单 ID、最后订单及链上状态和等待时间。已确认拒绝或失败的订单仍按用例失败记录。

GitHub Actions 的 **Buzzing API scenarios** 中，Preview 定时运行和新发起的手动运行都先执行日常用例，再串行执行 BTC 市价买卖、NBA 限价买卖和撤单，以及体育 Moneyline Draw 市价买卖。交易使用 `ci_tester` 的邮箱登录和钱包代签，执行结束时清理临时参数与登录缓存。重跑同一次 Actions 运行时跳过真实下单，用报告里的逻辑订单 ID 查证原单。

**体育 Moneyline Draw 市价买入。** CI 从本轮体育列表随机尝试待开赛或进行中的比赛，从详情的 Moneyline 读取 Draw 选项。盘口和购买力校验通过后，使用 1.3 U 含费 Cash 预算下单，按逻辑订单号确认全成。持仓须增加，开放持仓的增量与订单记录相差小于 0.01 份时接受，差值记入 Allure。再按订单哈希核对持仓历史的成交金额、手续费和份额。找不到可交易的 Draw 时，Allure 记录跳过原因。

**体育 Moneyline Draw 市价卖出。** 回查紧接的本轮买单，通过原条件和选项查询可卖持仓。卖出数量取成交份额和实际持仓增量中的较小值，按 0.01 份向下取整，保留既有持仓及精度零头。按逻辑单号确认卖出全成，按订单哈希核对历史净额，再核对可提现 Cash 增加及持仓减少。买单未完成或卖出前市场已关闭时，报告记录跳过原因。

**B3 Polymarket NBA 限价买入。** 用例按 24hr Volume 顺序找标题含 NBA 的多元事件，取列表中排第一的子市场，通过详情核对并随机尝试 Yes 或 No。限价取该方向当前卖一，按 3 U 含费预算筛选可买入足够拆成两笔限价卖单的份额，再用准备单的实际份额重新校验限价内深度。若该方向不满足条件则尝试另一方向，两个方向均不满足时继续筛下一个事件。提交后最多等 60 秒确认全部成交，成交详情见 Allure「NBA 限价买入成交详情」。未确认时标记跳过，订单按准备时设置的五分钟有效期处理。

**M05 Cash 同计划幂等重放。** 仅在本轮 NBA 限价买单已全成且订单和资金快照稳定后，提交一次相同的 `plan_id` 和代签参数。核对 `replayed=true`、逻辑订单和 CLOB 订单 ID 相同，并比较开放及历史限价订单、Cash、pUSD、可提现额和本轮方向的持仓。Allure 保留重放回执和前后快照。买入未确认时跳过此用例。

**B3 Polymarket NBA 两段限价卖出。** 复用本轮已全成的买单。原市场和方向先卖出本轮买入份额的 50%（向下取到 0.01 份），全成后再卖出本轮剩余可卖份额。每笔以刚读取的买一价为限价，并在提交前复核限价内深度。按逻辑订单 ID 最多等待 60 秒，校验全成份额与委托匹配、成交金额达到限价对应的最低金额，最后核对买入前的原有持仓。Allure 展示两笔订单、限价、成交均价和金额、合计金额及余量。

**B3 Polymarket NBA 被动挂单与撤单。** 使用相同的 NBA 动态选样和 2 U 含费预算，按卖一减一个价格档位提交 post-only 买单。下单资格按 `buying-power` 返回的可用 Cash 和购买力校验。可用 Cash 包含 Base USDB 和 Polymarket pUSD，服务端按需补差。用同一个接口的 `balance` 核对未成交金额及计划手续费的占用，以及撤单后的余额恢复。用例只撤销本轮 CLOB 订单并核对零成交终态，Allure 分别记录挂单、资金占用、撤单终态及资金恢复。

在项目根目录按需执行：

```sh
BUZZING_ENV=preview python -m pytest testcase/limit_order/test_limit_order.py::test_limit_buy_no \
  --trade-run=.local/trade-round-1.json --trade-run=.local/trade-round-2.json --tb=short --no-showlocals -q
python run.py --env preview --trade-run .local/trade-round-1.json
```

**执行与恢复。** 准备前核对会话与钱包、期初资金/持仓、盘口深度、挂单和授权。Cash 核对钱包持仓的前后差额，纯 Credit 使用已验证且未动用的 3 USDB 批次。每轮只有一次 `prepare` 和一次 `submit`，请求结果不明时使用日志按原单查证。只按本轮订单 ID 撤销待成交余单，已成交记录保留。Cash 在确认订单终态和冻结释放后核对临时授权清理。

证据在 `reports/trade-runs/<run_id>.jsonl`，包括计划、逻辑订单、原始量、成交、剩余量、手续费、持仓和资金变化。遇到 `incomplete`，先按日志中的订单标识回读状态并核对链上授权再复用账号。同一场景如需在独立数据上多轮执行，可传入多个 `--trade-run` 串行运行。C 套件按计划第八节收集 Preview 实跑证据。
