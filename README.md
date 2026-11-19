# Buzzing 接口自动化

## 从这里读代码

1. `tests/test_session.py` 是匿名会话用例。它发请求，再校验 HTTP 200 和 `auth_level = 0`。
2. `conftest.py` 提供 `api_client`。pytest 每条用例建立一个客户端，结束后关闭，Cookie 和认证状态随客户端隔离。
3. `api_client.py` 集中管理地址、10 秒超时、Bearer 凭证注入和请求日志。日志只记录固定操作名、HTTP 状态和耗时，网络失败时记录异常类型。
4. `response_checks.py` 有两种断言。原生响应显式传入该接口的 schema，`EnvelopeReply` 显式传入期望的 HTTP 状态和业务 `code`。

新增用例放到 `tests/test_<业务域>.py`。用例负责选择接口和写业务断言，共享配置放在公共客户端。用 `request(api_client, ...)` 发请求，`operation` 使用固定操作名。预检用例加 `preflight` 标记。后续需要身份的用例应从独立的身份 fixture 得到测试凭证，再创建带 Bearer 的客户端。

## 在 PyCharm 运行预检

从项目根目录运行。已有 `.venv` 可以直接使用，首次安装用以下两条命令。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

```sh
sh scripts/run_preflight.sh
```

每批结果存放在命令打印的 `reports/preflight-*` 目录，包含 `http.log`、`junit.xml` 和 `allure-results/`。把下面命令里的目录换成本轮目录，然后在浏览器查看报告。

```sh
allure serve reports/preflight-XXXXXX/allure-results
```

## 切换环境

项目只需设置 `BUZZING_ENV`。在 PyCharm 的 **Run → Edit Configurations → Environment variables** 填写 `BUZZING_ENV=preview` 或 `BUZZING_ENV=prod`。建议为两个环境分别保存运行配置。未填写时默认使用 Preview。

| 变量值 | API 地址 | 用途 |
|---|---|---|
| `preview` | `https://preview-api.buzzing.app` | 日常预检、登录和后续受控回归 |
| `prod` | `https://prod-api.buzzing.app` | 已有只读用例，公共客户端仅允许 GET 和 HEAD |

在 Terminal 中也可以对一条匿名会话用例临时指定环境。

```sh
BUZZING_ENV=preview .venv/bin/python -m pytest -v tests/test_session.py
BUZZING_ENV=prod .venv/bin/python -m pytest -v tests/test_session.py
```

`scripts/run_preflight.sh` 和 GitHub Actions 的 A1 预检固定使用 Preview。两个环境的测试身份和凭证分别管理。

## 预检基线

以下原生 schema 校验预检所需字段，后续按接口文档补齐完整契约。

| 请求 | HTTP | 校验 |
|---|---:|---|
| GET `/api/v1/home/banners` | 200 | 原生 JSON 的 `version` 和 `items` |
| GET `/api/v1/rewards/public-config` | 200 | 原生 JSON 的 `tasks` |
| GET `/api/v1/auth/session` | 200 | 原生 JSON 的 `auth_level = 0` |
| GET `/api/v1/user/info` | 401 | 未登录的业务码 `1005` |
| GET `/api/v1/errorcode` | 200 | 旧版包装响应的业务码 `0` |

历史基线来自 2026-11-18 实施计划。额外的原生字段及错误码目录成功响应来自本轮 Preview 实测。报告会单独提示网关 `403 / 1010`，便于与业务授权拒绝区分。

## 接入 Runner 和测试身份

Runner 在项目根目录安装同一份依赖并运行 `sh scripts/run_preflight.sh`。流水线应在通过和失败时都归档本轮目录，并保留实际 Runner、提交和运行链接。若 Runner 预装了依赖，可将 `PYTHON_BIN` 指向对应的 Python 解释器。

GitHub Actions 配置见 `.github/workflows/preview-preflight.yml`。上传到 GitHub 后，从 **Actions → Preview API preflight → Run workflow** 手动运行。当前默认用 GitHub 托管的 Ubuntu Runner，结果在该次运行的 **Artifacts** 下载。

和 Lance 评审时确认 `tests/` 按业务域组织、复用 `request` 和响应断言、匿名与已登录 fixture 分开。记录双方确认结果后再并行扩展业务模块。

待 Link 或后端、运维确认的事项：

- Preview Runner 的正式访问配置，以及需要放进 CI Secret 的参数名称。
- 专用测试账号的验证码发放和取码方式、登录请求和响应契约、续期或凭证重获方式，以及如何验证凭证确实更新。
- Preview 实际启用的 S0/S1/S2 与平台规则，以及可在隔离数据上验证授权和写入的操作、预期结果及数据恢复方式。

`identity.py` 根据脱敏后的 Preview 请求及响应字段实现了登录交换步骤。验证码先由 Preview 的 Privy 流程校验，再将取得的 `privy_identity_token` 提交给 `POST /api/v1/login`。接口返回 Buzzing 的 `token`、`expire` 和 `user_id`。后续可把 Buzzing token 传给 `create_client(bearer_token=...)` 并验证会话。自动获取 Privy 凭证、续期与真实登录验收仍待专用测试身份接入。

以上五条预检只覆盖本地或 Runner 的连通性与基础断言。第三节的必交业务链路仍需各自提交真实执行证据。第八节要求 F 在最终基准连续 5 批全通过，C 对每个冻结必交场景执行 2 轮独立数据并验证恢复或轮换，L 保留完整真实周期。
