# Buzzing 接口自动化

[GitHub 仓库 buzzingtesting/buzzing-api-tests](https://github.com/buzzingtesting/buzzing-api-tests)

| 目录或文件 | 日常怎么用 |
|---|---|
| `function/` | 维护接口调用和可复用的方法。登录、取码和查会话从 `identity.py` 开始看。 |
| `testcase/` | 按业务目录维护查询和交易场景。共享 fixture 放在 `conftest.py`。 |
| [`ci.yaml`](ci.yaml) | 维护测试账号和市场场景参数。 |
| [`check-framework.yml`](.github/workflows/check-framework.yml) | 配置 GitHub Actions 的定时计划、运行环境和报告通知。 |
| `run.py` | 运行日常场景并生成报告。传入 `--trade-run` 时加入指定的 Preview 受控交易。 |

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

场景所需的筛选条件和查询参数按模块写在 `market_cases` 段。BTC 1h、XRP 日档的系列名用于指定覆盖的市场类型，每次从 Preview 回包获取市场 ID 和游标。电竞 Props 的联赛由本轮导航返回。 [场景配置](ci.yaml)

| 用例目录 | Allure 模块 | 场景 |
|---|---|---|
| [auth](testcase/auth) | 身份与登录 | 邮箱验证码、错误验证码、匿名及已登录会话。 |
| [market_list](testcase/market_list) | 市场 | 列表排序、游标分页、分类和市场详情。按市场类型从本轮列表选样，核对事件或比赛详情。 |
| [orderbook](testcase/orderbook) | Orderbook 盘口 | 盘口事件、选项、最优报价和深度，以及与本轮选项对应的可用余额和购买力。买卖盘可为空，测试账号余额须大于零。 |
| [market_order](testcase/market_order) | 市价单 | 市价买入与卖出，核对成交、持仓变化、资金和订单历史。按运行配置执行。 |
| [limit_order](testcase/limit_order) | 限价单 | 日常查询本人订单历史。按运行配置验证限价成交、零成交撤单、部分成交后撤余单和解冻。 |
| [position](testcase/position) | Position 持仓 | 查询未平和已关闭持仓，校验账号归属、字段、分页和空列表。 |
| [settlement](testcase/settlement) | 市场争议与结算 | 日常核对结算历史、已结算持仓的领取状态，以及争议中市场的再次发起资格。公示窗内的争议资格用例在发版回归时手动运行。 |

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

两人共用一条场景时，可在各自的 PyCharm 运行配置里设置 `BUZZING_TESTDATA`，值填写本人在 `ci.yaml` 中的账号名。场景需要固定用 `tester2` 时，导入 `pytest` 并在测试函数上加 `@pytest.mark.parametrize("test_account", ["tester2"], indirect=True)`。同一场景要用两个账号分别验证时，把列表写成 `["tester1", "tester2"]`。Allure 会显示实际使用的账号名。

Gmail 的 `gmail_password_env` 填应用专用密码对应的变量名。本地 `ci_tester` 优先使用 `BUZZING_GMAIL_APP_PASSWORD_CI`，也可读取权限为 `0600` 的 `.local/gmail_app_password_ci`，首行是账号邮箱，次行是应用专用密码。`tester1` 使用 `BUZZING_GMAIL_APP_PASSWORD` 或 `.local/gmail_app_password`。GitHub Actions 通过同名 Secrets 注入凭证。Mailosaur 使用 `MAILOSAUR_SERVER_ID` 和 `MAILOSAUR_API_KEY`。

匿名接口用 `api_client`，已登录接口用 `authenticated_api_client`。JWT 按账号保存在 `.local/<账号名>.auth.yaml`，例如 `.local/tester1.auth.yaml`。没有缓存、账号变更或 JWT 在 60 秒内到期时，fixture 自动收取验证码并重新登录，验证会话后更新缓存。生产凭证保存到 `.local/prod/`。

## 生产回归配置

- 在根目录 `ci.yaml` 的 `prod_testdata` 填写已有的生产回归专用账号和验证码邮件标题。
- 配置生产的 `BUZZING_PROD_PRIVY_APP_ID`。如果该 Privy 应用配置了客户端 ID，再设置 `BUZZING_PROD_PRIVY_CLIENT_ID`。
- 生产 Mailosaur 使用 `PROD_MAILOSAUR_SERVER_ID` 和 `PROD_MAILOSAUR_API_KEY`。若将生产数据组改用 Gmail，配置 `BUZZING_PROD_GMAIL_APP_PASSWORD`。

生产运行只选中标记为 `@pytest.mark.pro` 的用例。生产客户端允许 GET、HEAD 和明确的 `POST /api/v1/login`。登录通过 Privy 验码获得本环境的新身份凭证。

## GitHub Actions 运行计划

**定时执行**在 [`check-framework.yml`](.github/workflows/check-framework.yml) 的 `on.schedule` 维护。当前为 `0 9 * * *`，时区为 `Asia/Shanghai`，每天北京时间 **09:00** 计划触发 Preview 场景。修改后提交到默认分支 `main`。

**手动执行**在 [Actions → Buzzing API scenarios](https://github.com/buzzingtesting/buzzing-api-tests/actions/workflows/check-framework.yml) 点击 **Run workflow**。默认 Preview，留空 `dispute_market_id` 和 `disputed_market_id` 时执行与定时任务相同的套件。`access_only` 开关只用于 Production。

**发版争议回归**需先准备仍在公示窗内的 Preview 自营市场、一个持仓账号和一个无持仓账号。选 Preview，在 `dispute_market_id` 填入当轮市场 ID，在 `dispute_no_position_account` 填写无持仓账号在 `ci.yaml` 中的别名。持仓账号沿用 Preview CI 的测试账号。Allure 分别显示匿名与持仓账号的争议资格，以及已登录无持仓账号被拒绝的结果。留空 `dispute_no_position_account` 时，无持仓用例会记录跳过原因。

日常 Preview CI 会从当前账号的自营持仓里选择一个处于第一轮或第二轮争议的市场，核对争议入口禁用、拒绝原因和对应轮次的历史记录。没有匹配市场，或接口结果不符合预期时，Allure 会记录跳过及原因。匿名、有持仓和无持仓的公示窗资格用例仍在发版回归时手动执行。

手动指定 `disputed_market_id` 时，用例对该市场做严格校验。两个市场输入可以分别填写，也可以同轮填写。运行结果见该轮的 Actions Artifacts。

领取状态用例检查当前账号最近关闭的 5 条持仓，不足 5 条时检查实际返回的记录。有兑付金额时接受 `received`（已到账）和 `pending`（待兑付），零兑付应为 `none`，两类记录的 `can_claim` 都应为 `false`。其他结束原因直接通过。Allure 附件列出抽样数量、实际状态、允许的状态和失败原因。

本地执行时，将 `MARKET_ID` 换成当轮市场 ID，并填写两个账号在 `ci.yaml` 中的别名。

```sh
BUZZING_TESTDATA=ci_tester BUZZING_DISPUTE_NO_POSITION_ACCOUNT=NO_POSITION_ACCOUNT \
  .venv/bin/python run.py --env preview --dispute-market-id MARKET_ID
BUZZING_TESTDATA=ci_tester .venv/bin/python run.py --env preview --disputed-market-id DISPUTED_MARKET_ID
```

CI 默认用 GitHub 托管的 `ubuntu-latest`。仓库变量 `BUZZING_RUNNER_LABEL` 可指定 Runner 标签。Preview CI 默认从公司邮箱取验证码，使用 `ci.yaml` 中的 `ci_tester`。仓库变量 `BUZZING_PREVIEW_TESTDATA` 可切换 Preview CI 使用的账号。

每轮 CI 从空的登录缓存开始，本轮 JWT 存在独立临时目录，场景执行结束时清理。CI 禁用 pytest 缓存，Allure、JUnit 和脱敏日志通过 Artifacts 查看。

## 查看报告和通知

- **本地**。每轮结果保存在 `reports/run-*`。本机安装 Allure CLI 后，可用浏览器打开 `reports/allure-report/index.html` 查看最新一轮。概览显示当前环境、API 地址和默认账号。
- **GitHub Actions**。打开运行记录的 **Artifacts**，下载 `preview-allure-report`，解压后在浏览器打开 `index.html`。预检日志、JUnit 和场景日志在 `preview-scenarios` 中。下载需要登录有私有仓库访问权限的 GitHub 账号。
- **邮件**。完整 Preview 场景结束后，公司邮箱 `dejian.ding@trufflelabs.ai` 向 `BUZZING_REPORT_TO` 指定的地址发送结果及报告链接。仓库变量 `BUZZING_REPORT_FROM` 保存发件地址。若收件箱没有邮件，可先检查运行记录的 **Email the Preview report** 步骤和收件邮箱的垃圾邮件。
- **Lark**。完整 Preview 场景结束后，机器人向测试群发送用例总数、通过、失败、错误和跳过数，并附上 Allure 和 Actions 链接。排查推送时查看 **Post the Preview report to Lark** 步骤。

用例失败时，CI 跑完并生成报告后仍显示成功。Allure、邮件和 Lark 保留用例失败数。预检、执行中断或报告生成出错时，CI 会显示失败，可通过 Actions 链接查看日志。[组织仓库 CI 验证记录](https://github.com/buzzingtesting/buzzing-api-tests/actions/runs/44660760957)

Preview 的 `orders/submit` 若返回 `500 / INTERNAL / 1002` 且提示提交结果未知或 `Could not add funds. Please try again.`，当前用例结束后中断本轮 CI。Allure 在触发用例下显示「测试环境测试金额不足（疑似）」并附上原始错误信息和 `plan_id`。结果未知时先按原单回查，避免重提。`Could not add funds` 会标注 Polymarket pUSD 补资未就绪。

## CI 变量和凭证

在仓库的 **Settings → Secrets and variables → Actions** 管理。密码、Webhook 和签名密钥填入 **Secrets**。

| 用途 | 仓库 Variables | 仓库 Secrets |
|---|---|---|
| Preview CI 登录 `ci_tester` | 默认读取 `ci.yaml` 的 `ci_tester` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| 报告邮件 | `BUZZING_REPORT_FROM`、`BUZZING_REPORT_TO` | `BUZZING_GMAIL_APP_PASSWORD_CI` |
| Lark 群推送 | 无 | `LARK_WEBHOOK_URL`，开启签名校验时另加 `LARK_SIGNING_SECRET` |
| Preview 改用 Mailosaur 取码 | 先在 `ci.yaml` 的 `testdata2` 填 Mailosaur 测试邮箱，再配置 `BUZZING_PREVIEW_TESTDATA=testdata2` 和 `MAILOSAUR_SERVER_ID` | `MAILOSAUR_API_KEY` |
| Prod | `PROD_MAILOSAUR_SERVER_ID`、`BUZZING_PROD_PRIVY_APP_ID`、按需设置 `BUZZING_PROD_PRIVY_CLIENT_ID` | `PROD_MAILOSAUR_API_KEY`，改用 Gmail 取码时配置 `BUZZING_PROD_GMAIL_APP_PASSWORD` |

本地用同一个入口预检，运行 `.venv/bin/python -m function.preview_access`。HTTPX 使用默认 User-Agent、8 秒超时、关闭自动重定向。预检覆盖匿名会话、包装响应成功码和未登录拒绝码。

## M03 Preview 自有订单簿

[市价单](testcase/market_order/test_market_order.py) 和[限价下单与撤单](testcase/limit_order/test_limit_order.py) 使用专用账号在 Preview 自有订单簿逐笔执行。Cash 场景可在运行配置里填 `market_ids`。执行时从本轮市场列表里随机选择一笔符合条件的获准二元市场，核对 NO 选项、盘口深度和账户可用数据。每份 `--trade-run` 只选中它指定的交易场景。

| `scenario` | pytest 用例 | 数据和主要断言 |
|---|---|---|
| `limit_buy`（旧配置默认值） | `test_limit_buy_no` | `BUY LIMIT`，`limit=0.88`，单笔含费预算 `budget_raw=2000000`。用 Cash 或纯 Credit，核对逻辑订单、累计成交与剩余量、手续费、持仓、余额及限价单历史。 |
| `market_buy` | `test_market_buy_no` | `BUY MARKET`、Cash、`budget_raw=2000000`。填 `max_price`，用于深度检查和事后成交价断言。校验购买力、成交、持仓增加和订单历史。 |
| `market_sell` | `test_market_sell_no` | `SELL MARKET`，使用专用钱包已有的自有 Cash 仓位，填 `quantity_raw`（10000 到 2000000）和小数 `min_price`，`budget_raw=0`，省略 `limit`。核对全部卖出、份额减少和扣费后到账。 |
| `limit_cancel` | `test_post_only_limit_buy_cancel_no` | `BUY LIMIT`、Cash、`budget_raw=2000000`。选低于当前卖一的 `limit`，执行器设置 `post_only`。验证零成交挂单、冻结额、针对本单撤单、冻结释放及 `logical_order_ids` 的 `cancelled` 终态。零成交撤单应从 Profile 的开放和历史视图中消失。 |
| `limit_partial_cancel` | `test_limit_partial_cancel_no` | `BUY LIMIT`、Cash。配置 `max_budget_raw` 后从实时列表选自营盘口，记录最新成交价，按卖一价和该档总份额计算 Amount，再加 2 U。观察到本单部分成交后撤销余单，并核对成交、费用、持仓、解冻和历史记录。 |

USD/USDB 价格使用每份的小数，例如 `0.88` 等于 88 cents。资金、成交量、份额和手续费按 6 位 raw 整数比较。市价单配置的 `min_price` / `max_price` 用于下单前深度检查和事后成交断言。

限价面板初始化按交易交付原型计算预期值。先将所选方向的报价四舍五入到整百分点，买入取卖一减 1 个百分点，卖出取买一加 1 个百分点。默认值范围为 1% 至 99%，对应侧无报价时预期为空。日志与 Allure 记录同一份盘口的报价和两个预期值，供页面校验使用。实际限价单价格取本轮获批配置。

B3 专用数据、M04/M05 场景与验收证据见 [B3 Preview 验收清单](docs/b3-acceptance.md)。

**运行环境。** 在可使用 Neo Gmail 工作负载身份的执行环境，把获准访问的后端仓库置于提交 `04bdad8214319d1d7a8a8d4651c5e1219f94992a`，`BUZZING_TRADE_BACKEND` 指向该 checkout 根目录。使用 Python 3.12 安装本项目依赖及该 checkout 的 `tests/preview_api/requirements-browser.txt`，运行 `python -m playwright install --only-shell chromium`。在该 checkout 的 `tests/preview_api/privy_sdk/` 下用 Node 22.22.0、npm 10 执行 `npm ci --ignore-scripts --include=peer --include=optional` 和 `npm run build`。市价卖出在临时目录从同一固定版本源码构建签名适配。由执行环境注入 `PREVIEW_QA_GMAIL_CLOUD_ACCESS_TOKEN` 和 `PREVIEW_QA_PRIVY_APP_ID`。

**自营限价部分成交。** Preview CI 配置 `BUZZING_OWN_PARTIAL_ACCOUNT`（签名支持库里的专用账号）和 `BUZZING_OWN_PARTIAL_MAX_BUDGET_RAW`（单笔含费上限，六位定点）后，按既有顺序自动加入用例。本地 `--trade-run` 对应 `scenario=limit_partial_cancel`、`venue=buzzing`、`funding=cash`、`side=BUY`、`order_type=LIMIT`，填 `max_budget_raw`，由程序选市场与方向。Runner 使用已构建的 `BUZZING_TRADE_BACKEND` 签名 SDK，钱包需有覆盖动态 Amount 的自营 Cash 余额和交易授权。提交前卖一价格或份额有变化时跳过并记录盘口；收到部分成交后只撤本轮余单。Allure 保留盘口、原始金额和份额、订单状态、成交和对账详情。

**运行参数。** 将模板复制到忽略目录 `.local/`，为每轮使用单独文件。填入新窗口、独立的 `run_id` 和已分配的专用账号。登录身份、UID、maker 和 signer 取自固定版本的账号映射。Cash 的 `batch_id` 留空。纯 Credit 填获准、未动用且未过期的真实批次 ID。共用盘口的执行者须共享同一锁目录，或在调度侧串行。

```json
{
  "environment": "preview", "run_id": "<本轮唯一 UUID>", "account": "A",
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

GitHub Actions 的 **Buzzing API scenarios** 中，Preview 定时运行和新发起的手动运行都先执行日常用例，再串行执行 BTC 市价买入、BTC 两段卖出、NBA 限价买入、同计划幂等重放和 NBA 挂单撤单。交易使用 `ci_tester` 的邮箱登录和钱包代签，执行结束时清理临时参数与登录缓存。重跑同一次 Actions 运行时跳过真实下单，用报告里的逻辑订单 ID 查证原单。

**B3 Polymarket NBA 限价买入。** 用例按 24hr Volume 顺序找标题含 NBA 的多元事件，取列表中排第一的子市场，通过详情核对并随机尝试 Yes 或 No。限价取该方向当前卖一，按 3 U 含费预算检查最少可买份额，再用准备单的实际份额重新校验限价内深度。若该方向不满足条件则尝试另一方向，两个方向均不满足时继续筛下一个事件。提交后最多等 60 秒确认全部成交，成交详情见 Allure「NBA 限价买入成交详情」。未确认时标记跳过，订单按准备时设置的五分钟有效期处理。

**M05 Cash 同计划幂等重放。** 仅在本轮 NBA 限价买单已全成且订单和资金快照稳定后，提交一次相同的 `plan_id` 和代签参数。核对 `replayed=true`、逻辑订单和 CLOB 订单 ID 相同，并比较开放及历史限价订单、Cash、pUSD、可提现额和本轮方向的持仓。Allure 保留重放回执和前后快照。买入未确认时跳过此用例。

**B3 Polymarket NBA 被动挂单与撤单。** 使用相同的 NBA 动态选样和 2 U 含费预算，按卖一减一个价格档位提交 post-only 买单，Polygon 钱包需预备至少 2 pUSD。用 `buying-power` 的 `balance` 核对未成交金额及计划手续费的占用，并核对撤单后恢复。用例只撤销本轮 CLOB 订单并核对零成交终态，Allure 分别记录挂单、资金占用、撤单终态及资金恢复。

在项目根目录按需执行：

```sh
python -m pytest offline_tests -q
BUZZING_ENV=preview python -m pytest testcase/limit_order/test_limit_order.py::test_limit_buy_no \
  --trade-run=.local/trade-round-1.json --trade-run=.local/trade-round-2.json --tb=short --no-showlocals -q
python run.py --env preview --trade-run .local/trade-round-1.json
```

**临时身份契约验证。** 将临时 Preview 用户 Token 注入 `BUZZING_M03_PREVIEW_TOKEN` 后，可运行 `python -m pytest testcase/orderbook/preview_contract.py --tb=short --no-showlocals -q`。该入口只做 GET，仅用于会话、资金和订单契约检查。

**执行与恢复。** 准备前核对会话与钱包、期初资金/持仓、盘口深度、挂单和授权。Cash 核对钱包持仓的前后差额，纯 Credit 使用已验证且未动用的 3 USDB 批次。每轮只有一次 `prepare` 和一次 `submit`，请求结果不明时使用日志按原单查证。只按本轮订单 ID 撤销待成交余单，已成交记录保留。Cash 在确认订单终态和冻结释放后核对临时授权清理。

证据在 `reports/trade-runs/<run_id>.jsonl`，包括计划、逻辑订单、原始量、成交、剩余量、手续费、持仓和资金变化。遇到 `incomplete`，先按日志中的订单标识回读状态并核对链上授权再复用账号。D5 的两轮使用不同专用账号和钱包，同一场景的多轮可通过重复传入 `--trade-run` 一次串行执行。离线和临时身份验证仅用于开发，C 套件按计划第八节收集真实执行证据。
