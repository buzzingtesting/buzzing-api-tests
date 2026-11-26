# B3 Preview 验收清单

依据 W05、交易链路及 Polymarket 限价单 API Contract、后端依赖 4–7 交接和实施计划第三、八节。下表优先级与必交范围为 D5 冻结草案，交由 Link 确认。C 类交易需要两轮独立真实执行、订单读回、资金状态及恢复或轮换证据。F 类契约用例按最终验收版本连续通过 5 批。

| 链路和场景 | 优先级 / 范围 / 套件 | 场所、资金及签名 | HTTP 方法与路径 | 验收证据与当前状态 |
|---|---|---|---|---|
| M04-01 市价买入与两段卖出 | P0 / 必交 / C | Polymarket、Cash、代签 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders`、`/api/v1/orderbook/positions` | 一次买入与两次卖出的独立订单、成交、历史、卖出后持仓。已有本地闭环证据，待当前版本 GitHub Actions 独立复验。 |
| M04-02 限价成交 | P0 / 必交 / C | Polymarket、Cash、代签 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders` | NBA 限价单全部成交和历史。已有本地实成交，待当前版本 GitHub Actions 独立复验。 |
| M04-03 限价挂单撤销 | P0 / 必交 / C | Polymarket、Cash、代签、2 U 含费预算 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`、`/api/v1/orderbook/order/cancel`，GET `/api/v1/orderbook/orders`、`/api/v1/orderbook/buying-power` | Preview 已真实挂单并零成交撤销。限价正确，市场可用 Cash 减少含费占用额并在撤单后恢复。Allure 记录请求 `post_only=true` 与订单回查 `false` 的差异，待后端确认。 |
| M04-04 限价部分成交后撤销余单 | P0 / 待确认必交 / C | Polymarket、Cash、代签 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`、`/api/v1/orderbook/order/cancel`，GET `/api/v1/orderbook/depth`、`/api/v1/orderbook/orders`、`/api/v1/orderbook/positions` | 限价内对手单只能吃掉部分份额，撤销余量后留存成交、对应持仓和解冻证据。待可控对手单。 |
| M05-01 同计划重复提交 | P0 / 必交 / C | Polymarket、Cash、代签 | POST `/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders`、`/api/v1/orderbook/positions`、`/api/v1/profile/funding`、`/api/v1/user/info` | 对本轮成交单重放，验证 `replayed=true`，订单与资金持仓快照一致。Preview 已真实重放，返回 `replayed=true`，无新订单，资金与持仓快照一致。原单历史为 `filled`，按逻辑订单查询为 `pending`，用例保留失败，待后端确认。 |
| M05-02 无凭证及无效凭证 | P0 / 必交 / F | Preview 交易接口，匿名或无效 Token | GET `/api/v1/orderbook/orders`，POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit` | 订单接口返回 `401/1005`，两个身份共 6 条。已在 Preview 实测。 |
| M05-03a 非法 plan_id | P0 / 必交 / F | Preview 交易接口，已登录 | POST `/api/v1/orderbook/orders/submit` | 返回 `400/INVALID_PARAMS/1004`。已在 Preview 实测。 |
| M05-03b 方案过期与非法签名 | P0 / 必交 / C | Buzzing、Cash、用户签名 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders`、`/api/v1/profile/funding` | 非法签名为 `400/INVALID_PARAMS/1004`，实际生成的计划超时后为 `409/CREDIT_PLAN_STALE/1035`。两种真实计划已在 Preview 各通过一轮，并确认原逻辑单无订单、资金一致。 |
| M05-04a Cash 余额不足 | P0 / 必交 / F | Polymarket、NBA、Cash 限价买入准备 | POST `/api/v1/orderbook/orders/prepare`，GET `/api/v1/orderbook/buying-power`、`/api/v1/profile/funding` | 从 24hr Volume 动态选 NBA 市场。使用当前 CI 账号，以实时 Cash 余额、购买力和最低下单金额三者的最大值加 1 U 作为 Amount，核对 `400/INSUFFICIENT_BALANCE/1004` 和资金不变。已在 Preview 实测通过。 |
| M05-04b 可卖份额不足 | P0 / 必交 / F | Polymarket、Cash、限价卖出准备 | POST `/api/v1/orderbook/orders/prepare`，GET `/api/v1/orderbook/positions`、`/api/v1/profile/funding` | 24hr Volume 动态选择零可卖份额的方向，核对 `400/INSUFFICIENT_SHARES/1004`、资金和可卖份额一致。已核对 Preview 返回。 |
| M05-05 纯 Credit | P0 / 必交 / C | Polymarket、纯 Credit、无 Cash 腿 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders`、`/api/v1/profile/funding` | 已实测 3 U 准备单只使用三个有效 Credit 批次，Cash 报价为 0。真实成交验收需核对 Credit 记入持仓、Cash 保持一致。 |
| M05-06 Cash + Credit | P0 / 必交 / C | Polymarket、混合资金、Cash 腿代签 | POST `/api/v1/orderbook/orders/prepare`、`/api/v1/orderbook/orders/submit`，GET `/api/v1/orderbook/orders`、`/api/v1/profile/funding`、`/api/v1/user/info` | 已实测 9.719320 U 准备单拆出 Credit 和 Cash 两类资金腿，Cash 腿返回 `cash_signing=server`。真实成交仍按已配置的 3 U 上限核对同一逻辑单与两类资金变动。 |

Cash 代签覆盖外部场所正常下单，自有 Cash 覆盖客户端签名校验。Preview 实测自有盘口零持仓的 `prepare` 仍能生成计划，后端交接对自有份额不足的断言在 `submit`，本轮采用可在 `prepare` 验证的外部场所。纯 Credit 覆盖无需 Cash 签名的路径，混合覆盖两种出资共存。M04-04 使用受控盘口锁定对手单大小，日常 NBA 挂单撤销用于检查零成交条件。

## 自营限价部分成交

自营部分成交从当轮 Newest 列表选活跃二元盘口，覆盖独立盘口和多市场事件下的非互斥二元子盘口。最新成交价进入报告，买单以当前卖一作为可立即成交的限价。Amount 为卖一价乘该档总份额再加 2 U。程序在提交前复查卖一与数量，并核对扣除预计费用后的计划份额大于该档卖量。提交后以同一逻辑订单确认部分成交，撤销该订单的余单，并对账持仓、扣款、解冻与历史。

该用例复用自营签名 Runner。配置 `BUZZING_OWN_PARTIAL_ACCOUNT`、`BUZZING_OWN_PARTIAL_MAX_BUDGET_RAW` 及现有 `BUZZING_TRADE_BACKEND`，使用钱包已有的交易授权。Preview 已实测动态盘口与客户端签名准备单，完整成交和撤单待配置签名 Runner 实跑。

## 数据配置

`BUZZING_B3_FIXTURES` 是 GitHub Actions 仓库变量或本地环境变量。只填当前轮次分配的数据，不包含密码、Token 或私钥。配置后，正常顺序自动执行对应场景。未配置或执行窗口未生效时，Allure 记录跳过。交易账号必须已在 `ci.yaml` 配置邮箱登录，凭证通过现有 CI Secrets 注入。

每个场景的通用字段为 `account`、`uid`、`event_id`、`market_id`、`outcome`、小数 `price`、6 位定点字符串 `budget_raw`、带时区的 `start` 与 `end`。事件与子盘口通过详情回查归属，`condition_id` 和交易方向 ID 取详情回包。

| 场景键 | 数据准备 |
|---|---|
| `buzzing_plan` | 活跃自有盘口，账号资金可生成一笔 3 U 以内、需用户签名的单腿 Cash 限价计划。选择限价内有足够对手盘的方向。 |
| `polymarket_partial` | 专用外部测试盘口和已授权的 Cash 钱包，预置足额 pUSD。额外填 `opposing_shares_raw`，限价内对手单合计必须正好等于这个值且低于本轮订单份额。 |
| `polymarket_credit` | 已验证的专用账号、有足额且适用于 Polymarket 的 Credit。填本轮允许使用的 `batch_ids` 字符串数组。 |
| `polymarket_mixed` | 可用 Credit 低于预算，余额足以覆盖剩余 Cash 腿，钱包已授权代签且现有 pUSD 足够。填 `batch_ids`。 |

两类资金准备单均已在 Preview 核对，订单回查为空，Cash、Credit 和 pUSD 前后快照一致。混合准备单的诊断金额超过日常成交用例上限，仅用于验证分腿。在已分配真实交易窗口、专用盘口限价以内能够全成时，为相应场景设置 `"submit": true`。同一 Actions 运行的重试跳过受控交易。如果提交结果不明，按报告中的逻辑订单 ID 回读订单、持仓和资金，查清前保留该轮数据。

主责暂按 W05 记录为 Neo。冻结评审、后端数据准备人与专用盘口和两轮运行窗口待确认。依赖齐备后预留约半天执行与对账，外部数据准备时长随盘口和 Credit 批次分配确定。
