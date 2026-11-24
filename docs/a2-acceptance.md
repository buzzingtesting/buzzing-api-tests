# A2 Preview 验收记录

2026 年 11 月 24 日在 Preview 使用 `tester1` 从空凭证缓存运行完整套件。自动登录成功，Allure 和 JUnit 均记录 **24 条通过、0 条失败、0 条跳过**。

| 用例组 | 本轮结果 |
| --- | --- |
| 身份与登录 | 4 条通过，覆盖邮箱验证码登录、错误验证码、匿名会话和已登录会话 |
| 市场列表和详情 | 14 条通过，覆盖首页排序、分类、分页、五类市场详情及所选子市场的持仓和挂单 |
| 盘口与 Orderbook | 2 条通过，覆盖独立二元市场和盘口深度。买卖盘为空时仍核对选项归属 |
| 持仓、订单和资金查询 | 4 条通过，覆盖未平与已关闭持仓、订单历史、游标分页和购买力。测试账号余额须大于零 |

## 后端补验证据

- 无 Token 或无效 Token 查询 `GET /api/v1/auth/session` 返回 HTTP 200 和 S0，`platform` 为空且 `reauth_required=false`。后端在补验账号上实测 Web 和 App 均能取得 S2。Web 超出再授权窗口后可复现 S1，强制策略开启时 `reauth_required=true`。App 在该验证条件下仍为 S2。 [后端 PR #8215](https://github.com/Buzzing-Club/buzzing_backend/pull/8215)
- 无效 Token 查询订单、购买力、个人资金、旧入金配置和私有资料返回 `401 / 1005`。后端验证 Web 与 App 均能查询本人订单，并留存 `/api/v1/trade/funding-info` 与 `/api/v1/user/info` 的脱敏响应。后端反馈其 `buzzing-checks` Runner 已通过，产物含 JUnit、HTTP 和会话策略记录。 [后端 Runner](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45359879506) · [后端产物](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45359879506/artifacts/14903837946)

## 接口契约

| 接口 | Preview 验证结果 |
| --- | --- |
| `POST /api/v1/realtime/ticket` | 使用 Bearer 凭证和空请求体。已登录返回 HTTP 200、`ticket` 和 `expires_in`，实测有效期 30 秒。匿名返回 `401 / UNAUTHORIZED`。 |
| `GET /api/v1/orderbook/depth` | 支持匿名查询。传入 `source=orderbook` 和选项的 `position_id`。HTTP 200 回显 `position_id`，买盘和卖盘以 `bids`、`asks` 返回，每档含 `price` 和 `total_qty`。空侧可省略。 |

后端交接记录表明，这两条接口已补录至 Preview 用户 OpenAPI。 [OpenAPI PR #8163](https://github.com/Buzzing-Club/buzzing_backend/pull/8163)

测试仓库的全量 GitHub Actions 运行证据以推送后生成的 Allure 和 JUnit 产物为准。 [测试仓库工作流](https://github.com/buzzingtesting/buzzing-api-tests/actions/workflows/check-framework.yml)
