# Preview 接口自动化一期：依赖 4–7 交接

> [飞书原文](https://trufflelabs.jp.larksuite.com/wiki/Pt4EwV2odid8WakLKXlj6524pae) · 第 6 版 · 2026-11-24 存档。
> 执行用例前，请按飞书最新版本及负责人确认的条件核对市场窗口、测试账号和操作授权。
> [项目补充 · Preview 交易专用账号、签名与对账](#t105671-preview-交易补充) · 2026-11-27 整理。

记录日期：2026-11-23。代码契约参照 `origin/preview`；现场证据单独标注。交付数据必须由对应负责人确认可用窗口，敏感凭证只走团队密钥库。

## **t105671 · 市场、流动性、资金和恢复（Lance）**

**状态：Buzzing Cash 已完成成交与轮换，纯 Credit 已核对历史真实成交，Polymarket 专用 A2 已完成自动补资、服务端代签、真实成交和资金／持仓对账。** 以下时间均为 UTC+8。

### **Buzzing 自有订单簿**

- **可复用盘口：** [活动 `1601693` / 市场 `4828026`](https://preview.buzzing.app/market-detail-v2/1601693?entry=ordinary&detailType=event&detailId=1601693&source=orderbook)，`condition_id=0x12c6f995e4c06f239ce5794ef3f5f95cf2b8333171d7f4e2ffc1093f0d7fe2fe`，NO token `75261210676100639389204572555822557915086305149470255070100474248204804001538`。11-23 复查仍在接单，NO 卖一 `0.88`、聚合数量 raw `197210818`，计划 12-01 22:20 结束。盘口截止时间是可用上界；每轮仍须核对当前深度和对手单安排。若需新盘口，由市场操作员走 `POST /api/v1/admin/orderbook/events`、`POST /api/v1/admin/orderbook/events/{event_id}/prepare` 和正常审核上线，提供专用对手单。

- **Cash / 客户端签名：** [A/B2 首轮](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45122061815) `991090` / `991091`，[A2/B3 轮换](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45133511720) `991093` / `991094`，均使用独立钱包，在 `4828026` 串行各成交一笔 NO。每笔限价 `0.88`，含费上限 `2` 测试 USDB；实际买入 `2.066100` 份、扣款 `1.839986`（手续费 `0.021818`），钱包余额 `3 → 1.160014`，无挂单及冻结，临时授权已撤。已成交 NO 持仓保留，下一轮用干净钱包槽位。签绑定钱包的 `cash_leg.typed_data_json`，随后提交 `plan_id` 和 `cash_signature`。上述四个槽位是已用证据，不作为新的空仓数据。

- **纯 Credit：** Preview `market` 交易库只读核对了 Lance UID `31543` 的市场 `5467832`（活动 `1714120`，已到期），逻辑订单 `e2b3a056-4d32-4b5f-b655-497b4591bb60`。现行订单和归档合计只有一条 `custody/credit` 腿、订单 `851431cc-ec2d-41b4-b032-dc1458640a17` 为 `filled`，两条成功成交记录合计 `11.363636` 份 YES。[Base Sepolia 回执成功](https://base-sepolia.blockscout.com/tx/0x8cbf33ecd8812a9e3543eb183d08cc5b5aa548666547f3d8c2ba19ae1eb636a1)。批次 `147` 预占 `4.943103`、释放 `0.124922`、`4.818181` 计入持仓，三项已对平。证据证明这笔下单是纯 Credit 成交；已到期市场与该批次不是新一轮数据。复跑要选已验证账号、足量且未过期的 Credit，并确认 `prepare` 所有腿均为 `credit`、没有 `cash_leg`，提交 `plan_id` 时不填 `cash_signature`。这是 Credit 托管持仓，核对时使用账务归属和持仓来源。

- **资金与持仓准备：** 有权限的管理员通过 `POST /api/v1/admin/tools/test-usdb/issue` 为 Base Sepolia（84532）**实际交易 maker 钱包**发放测试 USDB；核对链上回执及 `GET /api/v1/orderbook/buying-power`。Credit 用 `POST /api/v1/admin/credit/grants` 给已验证的专用 UID 发批准额度，记录批次 ID、有效期和场所，用 `/api/v1/profile/funding` 和 `/api/v1/profile/credit/history` 回读。两种发放均使用 `Idempotency-Key`；Credit 的 `amount` 为 6 位 raw。持仓通过成交准备，用逻辑订单、成交和持仓快照对账，无需 SQL 写库。新增 worker 使用独立账号、钱包和预算；并发取码已验两路，成交案例按组串行。

### **Polymarket 外部订单簿**

- **历史代签真实成交：** Preview 历史市场 `5432866`（活动 `1709386`，已停止接单），UID `990205`，`plan_id=fbccc1f702e651a579a6ef6837dc05fb`、`prepare_id=6035f3d3-5515-4baa-9149-453a85e50b31`、`logical_order_id=0e77d99b-0513-4c65-9520-9b7f896ed0c6`。同一 UID 的提交窗口里有 `poly server signing: submit timing` 与 `SubmitCreditOrder` 200；下游 `PostOrder ok` 的 CLOB 订单哈希与成交确认一致。交易库 Cash 持仓批次记录 `1.475000` 份、`1.194160` 成本，[Polygon 成交回执](https://polygonscan.com/tx/0xa96ddee248feacb0af10f83588ba2f22f5224eb2e1e94b34a9ec094a51054e33) 查询状态 `0x1`，事件日志包含同一订单哈希。该历史记录供核验路径；未确认历史 UID 或钱包获准用于新测试。

- **新一轮候选：** [活动 `1716817` / 市场 `5473497`](https://preview.buzzing.app/market-detail-v2/1716817?entry=ordinary&detailType=event&detailId=1716817&source=polymarket)，`condition_id=0x5c85c4c7366213aafaadd040410470f7d98111480d8f72d6c4c4b4c3118478a6`，Up token `109624155061211246268794832080606875986299044979708170834759773319887820213061`。11-23 公开实查，Preview 在接单，Preview 和 Polymarket 公共订单簿的 Up 卖一均为 `0.51 × 45`；计划 11-24 23:00 结束。备选 `5473502`、`5473499` 同时结束。三盘均为公开外部市场，窗口及成交条件运行前重查。候选盘和历史成交均已提供；新执行仍需名单、专用 deposit wallet、钱包授权和资金确认。

- **新一轮执行建议：** 用获批已开通代签（`granted`）、已预先备足可用 pUSD 的专用钱包，便于把实际成交与入金拆开验证。先核对实际钱包、Buying Power、盘口最小量、价格与含费限额，再调用 `prepare`；`cash_signing=server` 时用 `plan_id` 提交，`cash_signature` 留空。当前代码市价 BUY 含费总预算下限为 `1.1`；限价最低 5 份，公开候选卖一 `0.51` 时本金至少 `2.55`、含费更高，不能套用自有盘口的 `2` 测试 USDB 边界。运行结果应核对外部订单哈希和链上实际成交（成交不了按外部结果记录，避免盲目重提）。`prepare` 可能自动开户或补差入金；本轮审批须明确 `prepare`、资金来源、单笔和累计上限、真实提交以及后续处置。

- **新一轮拟议边界（待批准）：** 在 `5473497` 的 Up 方向，按 `0.51` 限价买入最少 `5` 份；本金 `2.55`，按当前 9% 预留计算预算至少 `2.7795`，建议单笔和整轮合计上限 `3 pUSD`，仅允许一笔。开跑前确认专用 UID、已绑定的 Polygon deposit wallet、代签 `granted`、至少 `3 pUSD` **可用**（剔除挂单冻结）、新鲜卖盘和窗口。若盘口卖一高于限价、深度不足、准备单需要入金或钱包权限未确认，停止。订单受理后确认实际成交；如出现本轮剩余挂单，只按获批清理规则撤该单，然后对账持仓、实际扣费和冻结。已用的历史钱包 `0x76a2…4950` 在区块 `97825761` 的链上 pUSD 为 `13.319595`；尚未确认它是给 Lance 的专用钱包，链上余额也不等于可用余额，不计入本次预算。没有调用 `prepare` 或 `submit`。

- **四个专用账号的钱包部署已验（11-24）：** [Preview CI `45224726701`](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45224726701) 串行通过，分别回读 Polygon 工厂成功交易和对应签名地址，得到 A `991090` → `0xb8e484d26a5fbd0c7c3979d157cce9ce6e415cca`、B2 `991091` → `0x277345bc275476210c392edc96b597ae0e8b5641`、A2 `991093` → `0xf0242997577f9789eb87d90b33aa4bdcca90f63c`、B3 `991094` → `0x5d1439925e64cb4316d9b5e4002bbe08ef9f661c`。链上独立复查四者 pUSD 均为 `0`。仅部署钱包，未添加 Privy 代签、发起默认无限额度合约授权、创建外部交易 API Key、拨款或下单。下一步仍需适用的受限授权方案及该钱包专用 pUSD 审批；不能将钱包部署视为外部交易就绪。旧 `990205` 及邮箱继续排除。

- **A2 交易前置复查（11-24 09:32，UTC+8）：** `991093` 的 Polygon 交易钱包 `0xf0242997577f9789eb87d90b33aa4bdcca90f63c` 链上 pUSD 为 `0`，普通及 NegRisk 交易合约的 pUSD 授权均为 `0`，普通交易合约的条件份额授权为 `false`。A2 的 Base Sepolia 智能钱包有 `1.160014` 测试 USDB，低于显式补资入口当前 `2 USDB` 的最低配置。Base 测试 USDB 补资会从平台 Polygon 主网资金池兑付实际 pUSD，测试发币接口只补 Base 测试币；两项审批分开。当前 Preview 强制代签，未配置明确的代签限制策略。默认 Polymarket `approve` 会为多份合约签无限额代币授权和全量份额授权；只开一项限额许可也无法满足现有完整开户判定。要按单笔预算继续，先确认适用于专用账号的限权配置和有上限的交易许可，或由交易侧提供已经按此范围准备好的专用钱包。随后用批准的 QA 资金来源为 A2 备资，核对链上到账及可用余额，再检查盘口、费用、成交和清理。`5473497` 本轮复查仍有 `0.51 × 45`；报价需在实际提交前再次核对。这次未授权、转账、补资或下单。

- **A2 默认授权验证（11-24）：** QA 已确认本轮仅 A2 走默认交易授权，不另配代签额度策略。Preview [PR #8211](https://github.com/Buzzing-Club/buzzing_backend/pull/8211) 新增仅该 UID、邮箱、签名钱包和一次窗口的官方 Privy 授权与链路回读，成功标准是 `granted`、预期 Polygon 钱包、`is_approve_origin=true`、`is_created_origin_api_key=true`；整条任务拦截转账、补资和下单。离线测试和 SDK 构建通过，PR 已合入 Preview；[实跑 `45311867803`](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45311867803) 核对 A2 的 Privy `granted`、预期钱包、全部默认交易合约授权和交易 API Key 均就绪，pUSD 仍为 `0`。工作流只完成指定钱包的授权和状态同步，没有补资或发单。其余三个 `+` 账号仍保持钱包部署完成、未开交易权限。pUSD 转入仍需要已批准的资金账户和本轮金额；旧 `990205` 继续排除。

- **A2 实际复跑（11-24 14:28）：** [只读 `45340080016`](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45340080016) 确认首轮中断未留下订单、挂单、成交历史或在途入金，可用 Base USDB 为 `1.160014`。随后[单笔实跑 `45340485406`](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45340485406) 在 `5473497` 用 UID `991093` 以 `1.10 USDB` 含费总预算做一笔市价 BUY Up，仅一次 `prepare` 和一次 `submit`，均为 HTTP 200。代签走 `cash_signing=server`，自动入金从 A2 自有 Base 钱包扣 `1.09 USDB`。逻辑单 `2841c5cf-d0fd-4f7c-8153-5dea03fd6bce`，CLOB 哈希 `0x97b5699a62908edf1b157b34142ca49d2e81b122df6c1f4d69b0b2b64a4d4d9b`，`filled`、Polygon 已确认。成交 `1.960785` 份 Up、成交额 `1.000000`，跨链合计净支出 `1.014900`。余额和仓位复查：Base `1.160014 → 0.070014 USDB`，Polygon `0 → 0.075100 pUSD`，Up `0 → 1.960785`，无开放订单。保留成交、仓位和余额作为证据，下一轮使用独立账号或重新申请专用资金。未使用旧 `990205`、未额外转账或重复提交。

### **恢复与验收边界**

- **自有订单簿恢复：** 只撤本轮未成交挂单，先查原订单及成交状态，核对冻结释放，再撤本轮临时授权。保留已成交的订单、份额和资金记录；需要平仓或划转资金时由负责人另行批准。默认留存旧仓位、轮换到新账号和钱包。自有 Cash 已实际验证这套轮换；本轮未拨款、开仓、修改数据库余额或调用 Polymarket `prepare` / `submit`。

- 公共 Polymarket 盘口 `5473497` 的计划截止是 11-24 23:00（UTC+8），可用窗口以场所接单状态和实时深度为准。本轮专用 A2 钱包已完成资金和成交验证，当前余额及持仓为已用基线；新的用例须使用单独测试资金和可用窗口。

## **t105672 · API 契约（Neo / Lance）**

**状态：两个缺失接口已核对，公开／匿名行为已实测。用户 OpenAPI 通过生成器测试和 lint，已由 PR #8163 合入 Preview；门户发布运行 `45121270676` 尚排队，当前门户仍为旧版。11-05 的旧发布运行 `42067389500` 仍显示等待；已获准的普通取消、强制取消及网页取消均被 GitHub 拒绝。需要 GitHub Actions 管理员或支持清理旧运行，再由最新 Preview 分支重新发布。Neo 的完整 GenericObject 清单待核对。**

- **`POST /api/v1/realtime/ticket`**：无请求体或查询参数要求。只认 `Authorization: Bearer <Buzzing JWT>`，并校验用户、钱包及黑名单。源码成功回包为 HTTP 200，裸 JSON `{"ticket":"<redacted>","expires_in":30}`；`ticket` 是一次性 WebSocket 票据，`expires_in` 是兑换窗口（秒）。响应带 `Cache-Control: no-store`。Preview 实测匿名 POST 为 HTTP 401，`error.code=UNAUTHORIZED`；`OPTIONS` 为 204。GET 落入通用兜底，实测 HTTP 200、业务 `1003`。源码还有 429 `RATE_LIMITED` 和 503 `UNAVAILABLE`；首次成功兑换与 WebSocket 订阅仍待带合法测试身份的验收。

- **`GET /api/v1/orderbook/depth`**：无需登录及请求体。查询参数 `position_id` 是方向 token ID，`source` 可省略，默认 Buzzing，也支持 `orderbook`；Polymarket 用 `polymarket` 或 `poly`。成功回包为裸 JSON：`position_id` 回显方向 ID，`bids` 是买盘，`asks` 是卖盘，每档 `price` 为价格字符串，`total_qty` 为该档聚合数量字符串。空侧可能省略。Preview 实测自有空盘为 200，只有 `position_id`；Polymarket 样本为 200，卖盘首档 `{"price":"0.999","total_qty":"10"}`。非法 `source` 实测 `400 / INVALID_PARAMS / 1004`；漏传 `position_id` 实测 HTTP 500，记录为当前行为并交接口负责人确认期望。

- **`POST /api/v1/orderbook/order/cancel`**：需用户 Bearer。Buzzing 优先传 `order_id`（本地订单 UUID）；支持用 `order_hash` 查询本人未结订单后撤单。Polymarket 传 `source=polymarket` 和 CLOB 的 `order_hash`。默认来源 Buzzing。Buzzing 成功时可返回 `{"order":...}`，查询不到快照时为 `{}`；Polymarket 成功返回 `{}`。`cancelling` 表示撤单请求已受理，验收仍要读回订单终态。Buzzing 的一个腿订单可能关联同一逻辑单的多条未结腿。

- **`POST /api/v1/orderbook/orders/cancel`**：需用户 Bearer。Buzzing 范围由 `all=true` 或相应市场／方向／订单哈希筛选确定，`order_ids` 不是批量筛选字段；只对专用测试订单使用具体筛选。Polymarket 传自己的 `order_hashes`。200 仍要检查 `results[].success`、`error_detail`、成功数和失败数。既有 [Preview 探针](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/44922188957) 使用 `{}`，实测 S2 为 `400 / INVALID_PARAMS / 1004`、启用再授权的 S1 为 `401 / REAUTH_REQUIRED / 1710`。探针未实际撤单。未登录的订单路径按 `401 / 1005` 检查。

- **旧 `POST /api/v1/trade/cancel`**：需登录，传 `order_hash` 和可选 `source`，默认 Polymarket，成功载荷为 `null`。Buzzing 场所使用新的订单簿撤单接口处理本地 UUID。其他旧路径待 Neo 的清单逐条核对真实样例。

`POST /api/v1/orderbook/orders/submit` 源码契约：native 回包中 `code` 是 HTTP 状态，`metadata.biz_code` 是业务码。合法格式但没有可恢复结果的过期计划为 `409 / CREDIT_PLAN_STALE / 1035`；格式错误的计划 ID 为 `400 / INVALID_PARAMS / 1004`。Buzzing Cash 缺签名或签名不匹配为 `400 / INVALID_PARAMS / 1004`；Polymarket 传入错误签名的详细结果可能由下游决定。顶层余额或可卖份额不足为 `400 / INSUFFICIENT_BALANCE` 或 `400 / INSUFFICIENT_SHARES`，业务 `1004`；逐腿拒绝也可能在 200 回包中的 `results[].success=false` 和 `error_code`，全腿 Credit 失效可能归 `CREDIT_PLAN_STALE`。同一用户、同一 `plan_id` 的既有结果可用 200、`replayed=true` 返回；提交仍在进行时可能返回 `409 / ORDER_IN_PROGRESS / 1035`。超时后先按逻辑订单 ID 查证原计划是否落单，再按同计划的幂等规则处理。交易异常要用获批订单材料补现场样例。

主要代码：`app/bff-service/internal/service/realtime_ticket_http_gateway.go`、`proto/api/bff-service/bff/v1/orderbook.proto`、`app/trading-service/internal/service/orderbook_gateway.go`、`app/trading-service/internal/service/orderbook_credit_order.go`。用户 OpenAPI 已由 Preview PR #8163 从真实注册补录两条路径，兼容聚合、admin 和 partner 生成物无变化；门户新版本及调用方回归仍待检查。

## **t105673 · Rewards 真实发奖（Neo）**

**状态：按 11-23 更新的验收口径，普通任务和好友任务的日期测试工具发放均已查到实际 Credit 到账。** `Preview display fixture only` 等纯展示不计。另有真实业务事件验收需求时，仍需单独验证正常事件链；下方的生产式实跑方案作为备选，不影响本轮测试工具验收。

### **日期测试工具到账证据（11-23 查证，11-16 发放）**

在 [测试后台 → Credit 发放 → 发放记录](https://test-admin.buzzing.app/next/user-ops/users/credit) 按名称 `[TEST]` 检索，以下记录均有独立 `batch_id`、`source_type=task`、“未使用”状态及对应的实际可用金额：

- **普通任务 `QA1116-03-Daily-Split`**（`b41c1081acc145cf586c3d777943f263`）：模拟 Manila 11-16 的奖励批次 `134`，可用 `$2.00 Credit`；模拟 11-18 的批次 `135`，可用 `$3.00 Credit`。这是同一任务的两个日期奖励单元。另有普通步骤任务 `QA1116-02-Step-Once` 的批次 `136`，发放 `$5.00 Credit`。

- **好友任务 `QA1116-06-Friends-Once`**（`8d1c5468b058886d6aa902db2d5f5e99`）：批次 `130` 和 `131` 各到账 `$5.00 Credit`，接收者相同，来源均为 `date-test:8d1c5468b058886d6aa902db2d5f5e99`。这是两笔独立账务批次，发放记录不单独显示模拟好友键或场景代数。此前查的是任务报表的自然发奖到账人群，和测试工具发放记录是两条查询口径。

- **Neo 复跑：** Preview 后台「测试工具 → Rewards 日期推进」，按专用 UID 和 `task_id` 创建场景。普通任务按 `set_day` 标记达标、用 `advance` 推进一个 Manila 自然日；好友任务用 `set_friends` 设置两个不同场景内好友键和达标条件，按需要设置模拟解锁。达标后由获批操作者执行 `grant_rewards`，回查 `projection.rewards[].state=paid` 和 `batch_id`。同一场景的同一奖励单元重复提交用于验幂等；需要独立复跑可换好友单元，或在没有未决发放时 `reset` 退出并新建场景。`reset` 保留已到账的 Credit。

- **到账与通知：** 用 `batch_id` 查后台发放记录及用户 Profile Credit 历史。日期工具走 `credit_granted` 通用到账通知，可在 Inbox 按同一批次核对；本次只查了实际账务批次，未核对这四个批次的通知投递。自然发奖的 Rewards 收据断言与日期工具账务分开。

### **已核对的 Preview 任务（11-23，只读）**

- **优先用 `Daily Change - Once` 做普通任务和跨日。** `task_id=0df5af2ae1e967f0e8fb60614b64ef71`，v3，立即解锁、已上线，查询时剩余 `$29.88`。运营后台最近 30 日 `paid` 统计显示 7 位到账用户；这是该任务的历史聚合，尚未核对本票专用账号的新事件、批次及 Inbox。同一专用用户在两个连续 Manila 自然日分别重新登录 App，`2/2` 后一次发 `2` cents Credit，奖励单元为 `once`。首轮用账号 R1，复跑用独立账号 R2。两个账号的 Day 1 都可安排 11-23，Day 2 最早 11-24（均 UTC+8）；需先确认账号无先前进度、每轮预算、登录方式，并查两次真实 `identity.app_login.v1` 对应的任务进度。当天重复登录可用来核验仍只计一天；读取网页或模拟日期不计。若未赶上 Day 1，按连续两天重新排。

- **普通任务备选 `Step Reward-Once`。** `task_id=02bd65706a072070c1440b3e3f76371a`，v4，立即解锁、已上线，单次奖励 `$1`、剩余 `$47`。条件为一次不少于 `$2` 的交易、一次有效入金和一次 App 登录；本次优先使用无交易依赖的每日任务。

- **已有好友任务的限制：** `QA1116-06-Friends-Once`（`8d1c5468b058886d6aa902db2d5f5e99`，v2）要求邀请人先做 `1,000` 笔交易，达标好友需首充和 App 登录，每位 `$5`，最多 2 位；`Friend Reward-Once`（`c4f94f711349611c12e92f91e4b73bdc`，v3）立即解锁，但好友必须完成 `Getting started` 和 `Keep trading`。后者引用的 `Keep trading` 是展示样本，不用于本次验收。未核实有专用邀请人已满足前一任务门槛。11-23 在后台「统计 / 导出 → 到账用户」分别核对这两个任务，Manila 10-25 至 11-23，两者均为 0 位到账用户；该结果仅覆盖所查任务和窗口。

### **真实业务事件端到端备选（单独要求时执行）**

1. **普通任务：** Neo 用 R1、R2 两个获批、无历史进度的专用 App 测试账号，在 Day 1 / Day 2 各登录一次。Day 1 确认只计 `1/2`、无当轮发奖；Day 2 确认 `2/2` 后各产生一个 `once` 单元、`2` cents 入账。确认首轮和复跑 UID、收据起始 revision 及凭证来源。实际窗口内先核对 `GET /api/v1/rewards/overview` 的 `payouts_enabled` 和各自任务版本；任务已有发奖、处于测试投影或规则漂移就停。

2. **好友任务：** 建议在获批的隔离 Preview 范围使用小额、立即解锁的 `friends` 任务，好友做一次真实 App 登录即可。先确认是否能限制受众；现有 `TaskConfig` 未找到按 UID 限定的字段，共享 Preview 新建任务可能影响其他用户。需要 Rewards 负责人批准环境、受众、单次与累计预算，再创建及上线。两轮用独立邀请关系：邀请人 I1 → 新好友 F1；I2 → 新好友 F2。通过正常邀请入口建立绑定，先查 `/api/v1/rewards/tasks/{id}/friends` 确认归属，再让好友登录 App，验证每位邀请人产生且仅产生 `friend:<好友 UID>` 的奖励。若采用同一获准邀请人，也必须两位新好友、两个不同单元和可用名额。不可用展示任务、后台直接拨 Credit、事件注入替代。

3. **账务与通知：** 每轮开跑前记录受益人 UID、`task_id` / 版本、`unit_id`、奖励 cents、收据 `after_revision`、UTC 起止时间；真实事件留好 UID 和发生时间，需要时和身份 outbox／Rewards 事实记录关联。跑后依次查任务 `paid`、Rewards receipt、Profile Credit 批次和 Inbox，四处共用同一个 `batch_id` 且金额一致。真实任务发放的内部 Credit 批次来源为 `source_type=task`、`source_ref=task_id`。见下方对账入口及 `tests/preview_api/rewards-evidence.md`。有发奖但通知晚到时先核对现有批次及通知投递；不直接重复发奖。

4. **恢复或轮换：** 同一笔事实重放只能用于幂等核对。普通任务换 R2，好友换独立邀请关系；已支付记录和关联关系保留。日期工具按 UID / 任务推进隔离场景，`reset` 只退出该场景；场景投影及 `[TEST]` 奖励不可替代真实业务证据。真实跨日使用 `Asia/Manila`（UTC+8）。原 11-20 启动点已过；11-24 是按 11-23 启动推算的最早日期，由负责人确认实际安排。

- 发奖后按 `task_id`、`unit_id`、`batch_id`、金额和时间对账：`GET /api/v1/rewards/tasks/{id}` 查任务单元 `paid`，`GET /api/v1/rewards/receipts` 查回单，`GET /api/v1/profile/credit/history` 查 Credit 批次，`GET /api/v1/notifications/list?type=credit_granted` 查 Inbox。`PREVIEW_QA_REWARDS_CASES` 配好专用账号和期望后运行现有 `rewards` 套件；当前 GitHub Preview 环境未见该 Secret。详见 `tests/preview_api/rewards-evidence.md`。

## **t105674 · 争议、结算、自动 Claim（Lance）**

**状态：历史争议和兑付已核实；当前可争议专用市场，以及由自动任务触发的兑付与资金对账仍待确认。** 11-23 只读检查：运营后台「人工市场结算 → 公布结果 → 结算环节：已公布」返回 0 条。已知的 `4828026` 和 `5308538` 均未公示结果（`dispute_window_ends_at=0`）；`3526900` 已进入 `claimable`，可用作历史对照。上述查询不代表所有市场类别均无可争议样本。

- **争议准备：** 普通自有测试市场到期后，由有写权限的操作员通过 `POST /api/v1/admin/markets/{market_id}/settlement-results` 公布初始结果。用 `GET /api/v1/orderbook/market/{id}/settlement/dispute-info` 记录 `proposed_at`、`dispute_window_ends_at`、`expected_version`；只有持仓且满足条件的测试用户才提交。`POST /api/v1/orderbook/market/{id}/settlement/dispute` 带最新版本、`Idempotency-Key`、不同于当前拟议结果的请求结果、150–2000 字符理由、来源名和链接，以及本人上传的 PNG/JPEG（不超过 10 MB）。通过后查看 `dispute1` 或 `dispute2` 及历史记录。窗口以已冻结的截止时间为准，时钟达到截止即拒绝。普通市场种子规则的初次公示／争议倒计时／一裁公示均为 24 小时；现场规则和版本以该市场最终记录为准。二裁终裁无公示等待。争议倒计时结束后仍需独立管理员裁决；走正常裁决或轮换独立市场来恢复测试。样本 `5308538` 已结束但未公示，不能直接用作开放争议窗口。

- **结算与自动 Claim：** 无争议则等公示窗关闭；有争议则走裁决。结算补偿约每 30 秒扫描，确认链上结算回执及 CTF 赔付结果后市场才变为 `claimable`。普通用户订单簿自动兑付由 Preview 的 `auto-redeem-delegated` 每 5 分钟调度，用户须有已授权的代签钱包及正数可领取仓位。该任务配置的单次执行上限为 900 秒。无已记录的 E2E 到账 SLA；建议把等待拆成公示截止、链上确认、自动任务执行和索引四段。可把 `claimable` 后 15 分钟设为人工排查点（测试建议），检查调度、尝试状态和链上回执，再按实际状态决定是否继续等待。超时或状态未知时保留原尝试，先对账，避免补发。

- **证据：** 后台 `GET /api/v1/admin/markets/{market_id}/settlement-case` 和 `settlement-history` 对照用户侧 `settlement/status-history`。普通 Auto Claim 要把 `auto_redeem_attempts` 的用户、条件、`user_op_hash` 和时间关联链上回执，再和 trade-service 的 `t_user_claim_history`、持仓事实的 `claim_status=claimed`、`GET /api/v1/orderbook/positions/closed` 的 `can_claim=false` 及兑付金额、用户资金变化核对。市场层的 `claimable` 仅说明进入可领取阶段。

- **历史证据：** event `1436538`／market `3526900` 的[结算历史](https://preview-api.buzzing.app/api/v1/orderbook/market/3526900/settlement/status-history)明确记录两次用户提交（历史项 `2743`、`2745`），从初次公示进入 `dispute1`，一裁公示后进入 `dispute2`，二裁后结算确认。该记录的初次公示窗为 24 小时；历史环境曾提前裁决，当前按冻结窗口和权限校验执行。[Base Sepolia 结算回执](https://base-sepolia.blockscout.com/tx/0x41acdbe31875f43edb88acc5cd15c048f1a5ad9920cf6d4c2348066a40b8d9ab)与[兑付 2 USDB 的回执](https://base-sepolia.blockscout.com/tx/0x674260247c57a9c288df2853a8060c8f2705e35728c7fbf347d6cc11dcf717f4)均成功，相隔 `15:53:44`，不能用作自动到账时限。兑付回执的 `user_op_hash` 为 `0xbf62207a99964475321c511c1f48e7159cbebf82b5ff3e23f268334ba09ae617`，`condition_id` 为 `0x0a1ce41ca347fbf0802311104eac02a0a0fe8234619a88a23f47eca9041144e5`。按这两个值在只读 `auto_redeem_attempts` 查 `venue=base`、`status=confirmed`、`tx_hash` 和时间，再核对交易账务及持仓，才能归因为自动 Claim。11-23 当前只读数据库页面显示无连接，尚未取得这条记录。若记录保留期外或任务未匹配，另找近期自动 Claim 样本。

- **建议分别安排两盘：** 争议用已到期、可公示且专用用户有持仓的盘，运营发布后把 `market_id`、持仓 UID、窗口截止和裁决负责人交给 Lance。自动 Claim 用另一盘且保持无争议、无人点手动 Claim，先确认胜方持仓和代签授权。若用普通 24 小时公示窗，为 11-27 前留出自动兑付和对账时间，宜在 11-25 前完成公示排期；这是建议最迟节点，仍待负责人确认。

- **两盘候选复查（11-23）：** 后台 `QA` 队列中，自有市场 `4404721`（event `1549669`）和 `3920744`（event `1488151`）已过结束时间、待公布，公示前仍要核实当前归属、在途订单和持仓影响。`4404721` 公共成交量为 `0`，没有可用于验收的持仓证据；`3920744` 已有历史成交，须先确认其他持仓归属。Base Sepolia 区块 `62436260` 上，A/B2 已知资料钱包在上述两盘及历史候选 `3637039`、`3997317`、`4057556`、`3525714` 的 YES/NO 份额均为 `0`；这不涵盖另有的实际持仓钱包。`3637039` 已有两轮争议，不能用作新的一轮争议提交；其余三个历史候选已经 `claimable`。本轮没有找到两盘可直接交给 Lance 实跑的样本，也未重启 t105670 或触发任何结算写入。

- **Lance 交付的两盘（11-23）：** `1714120`、`1714103` 是活动 ID；由 `/api/v1/events/{event_id}/markets?lean=1` 解出自有订单簿市场 `5467832`（`test-112302`，16:00 UTC+8 到期，争议）及 `5467797`（`test-112301`，15:25 UTC+8 到期，自动 Claim）。同数字作为市场 ID 会命中另两盘 Polymarket。下午在 Preview 后台按实际市场 ID 查到两盘均为「待公布」，公开历史为空，`expected_version=1`；后台结算期设置显示初次公示和一裁后缓冲均为 24 小时。UID `31543` 的自有订单簿公开持仓列表未返回这两盘。按当前 Preview Base Sepolia Conditional Tokens 合约 `0x2446b15a81AA76e9dBDa0a06a1B898643855eDc1`、区块 `62502860`，`0x504A9013bfC1a7cbebe380e7aE324c55b3803B68` 在 `5467797` 有 `3.475900` YES、`0` NO，在 `5467832` 的两边均为 `0`；UID 公共资料地址在两盘的余额均为 `0`。11-23 再核对：运营后台确认 UID `31543` 的资料钱包为 `0x09821de901b2fa5aee9c43491356e02a7e364d25`；链上 `owner()` 确认该钱包是 `0x504A9013bfC1a7cbebe380e7aE324c55b3803B68` 的所有者。Base Sepolia 区块 `62510235` 再次确认 Claim 盘该持仓钱包为 `3.475900` YES；资料钱包、充值地址在这两盘的 YES 均为 `0`，已知 Claim 持仓钱包在争议盘的 YES 也为 `0`。仍需用 `31543` 的登录态核对争议盘实际持仓，并确认 Claim 盘的服务端代签状态为 `granted`。争议盘还需确认初始公布结果。等前置确认后，可给 `5467797` 公示 YES，等待公示窗关闭和链上结算，再查自动兑付及账务；本轮未公布结果。

## **交接与排期**

- `t105671`：市场／交易负责人交市场 ID、对手单、交易窗口；钱包／资金负责人交预算和补资边界。Polymarket 需要独立审批。
- `t105672`：Neo 交一期 GenericObject 清单，Lance 核对交易断言；接口负责人处理 OpenAPI 补录和缺参 500 的期望。
- `t105673`：Neo 可先按上述四个测试批次验收；自然业务事件验收另行确认专用账号、邀请样本和预算。
- `t105674`：市场运营交专用市场、持仓用户和独立裁决人；结算执行负责人提供自动任务和账务查证入口，Lance 验收。争议测试和无争议结算测试可分别准备独立市场，避免争议人工等待拖住自动 Claim 样本。

飞书任务与计划读取均返回 `token_missing`。正式负责人和新预计交付日需由对应负责人确认；本轮未转派、评论或更改飞书排期。

## t105671 Preview 交易补充

本节收录首轮 A/B2 交易、A2/B3 轮换及 A/B2 纯 Credit 的交接记录。账号资金、盘口深度和权限取自各次执行时的快照。开跑前按本轮批准的账号、预算和窗口重新核对。

### 专用账号、钱包和登录

首批 A、B2 可各分配给一名测试人员。轮换按 A / A2、B2 / B3 两组分配。四组账号都沿用现有 `+preview-trade-*` 登录身份，各自有独立 UID、签名地址和交易钱包。同一账号和钱包同一时间只分给一名执行者，资金与预算按账号独立核算。Lance `31543` 和 Neo `991017` 不参与这四组交易。

| 账号 | 登录邮箱 | UID | 签名地址 `signer` | 交易钱包 `maker` |
| --- | --- | --- | --- | --- |
| A | `dejian.ding+preview-trade-a@trufflelabs.ai` | `991090` | `0xc2c84d49239a82ab855f44a45c3ffa7cd2325e83` | `0xd6485411c729b4d8ff47c03f839b4bb07fe41a50` |
| B2 | `dejian.ding+preview-trade-b2@trufflelabs.ai` | `991091` | `0xc44efa8051299873a042437bb7b41338c0fad74c` | `0x301ee60c628e0cc427c361c084ed784a02a0981c` |
| A2 | `dejian.ding+preview-trade-a2@trufflelabs.ai` | `991093` | `0x2afe8893eaf7be22904100d03d649e8efa12351c` | `0x168905160395035a601d2a53316f4710e4dbef76` |
| B3 | `dejian.ding+preview-trade-b3@trufflelabs.ai` | `991094` | `0xfa64c2ffe8e489599bf40ed5402452684859e9fc` | `0x69e21997bc165efc5cb9b4a98def5ce6e0055d72` |

- 四个登录别名共用 Neo 的 Gmail 收件箱。`preview-api-preflight.yml` 的试签输入示例为 `suite=trade-identity`、`account=neo`、`trade_tag=A`、`trade_mode=proof`。`account=neo` 选择收件箱授权，`trade_tag` 决定实际登录身份。CI 使用 `PREVIEW_QA_GMAIL_CI_WIF_PROVIDER` 与 `PREVIEW_QA_GMAIL_CI_SERVICE_ACCOUNT` 获取云访问权限，按完整 `To` / `Delivered-To`、发码时间和已验证发件人取本轮新码。登录后核对 UID 和钱包。交接提供登录入口和可调用的 Privy 签名入口，密码、私钥和验证码留在现有授权流程中。 [邮箱取码 `gmail_otp.py`](https://github.com/Buzzing-Club/buzzing_backend/blob/c26f2477c10a64b28f06b17e84fccee538a2706e/tests/preview_api/gmail_otp.py) · [登录与订单校验 `test_trade_identity.py`](https://github.com/Buzzing-Club/buzzing_backend/blob/c26f2477c10a64b28f06b17e84fccee538a2706e/tests/preview_api/test_trade_identity.py) · [Privy 签名 `login.jsx`](https://github.com/Buzzing-Club/buzzing_backend/blob/c26f2477c10a64b28f06b17e84fccee538a2706e/tests/preview_api/privy_sdk/login.jsx)
- 首轮 A、B2 按组串行发码和下单，发码间隔至少 120 秒。两组都已验证重登与试签。B2 的 CI 运行中 B2 两次登录和试签成功，整轮因 A 当时收码超时失败。后续 A2、B3 相隔 14 毫秒发码，从同一收件箱按完整别名分别取到验证码。同一盘口的实际下单仍按组串行。 [A 重登与试签](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45044967690) · [B2 两次登录与试签](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45043249878) · [A2/B3 并发取码](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45138391123)
- 自有盘口的测试资产为 Base Sepolia（`84532`）上的测试 USDB，资金发到 `maker`。首批 A、B2 各到账 `3` 测试 USDB，各做一笔 Cash 买入，单笔含费上限 `2` 测试 USDB。轮换沿用每账号每轮一笔、含费上限 `2` 的边界。盘口 `4870059` 和 `4870060` 排除在本轮之外。开跑前用各自会话核对实际可用购买力。额外补资、转账和 Polymarket 入金按各自批准范围执行。

### Prepare 与 Submit 的三种签名路径

使用同一测试账号的登录会话先调用 `POST /api/v1/orderbook/orders/prepare`，按回包的 `legs`、`cash_leg` 和 `cash_signing` 选择签名路径，再调用 `POST /api/v1/orderbook/orders/submit`。

1. **客户端签名。** Buzzing 自有盘口的 Cash 买入设 `use_credit=false`，只接受一条 Cash 腿，`cash_signing` 可以为空；出现 `offset` 或其他出资腿即停。Polymarket 返回 `cash_signing=client` 时也走客户端签名。测试侧需要登录会话、UID、绑定的 `maker` 和 `signer`、可用的 Privy 签名入口、测试资金、盘口与选项 ID。将 `cash_leg.typed_data_json` 解析为 EIP-712 数据。核对链、交易合约、`maker`、`signer`、买卖方向、价格和含费金额。本轮自有普通盘口交易合约为 `0x3d30e9cac3620793c29724cc350bbb486b101456`，本轮订单的 `recipient` 应与该组 `maker` 一致。用该组 Privy 钱包签名并核对订单摘要与恢复出的签名地址，再提交 `{"plan_id":"<本轮计划 ID>","cash_signature":"0x..."}`。Polymarket 客户端准备单已有 `cash_leg` 但签名数据为空时，按此交接记录视为待入金。仅在获批范围内补差，带 `tx_hash` 重新 `prepare`，使用新计划签名。
2. **服务端代签。** 准备单有 `cash_leg` 且 `cash_signing=server`，提交 `{"plan_id":"<本轮计划 ID>"}`，`cash_signature` 留空，由服务端代签并处理所需入金。测试侧需确认 `GET /api/v1/user/delegated-signer` 为 `granted`、Polymarket deposit wallet 已绑定、盘口可用，以及单独获批的 pUSD 入金和交易上限。`prepare` 可能触发预入金，调用前先确认预算。以回包实际出现 `cash_signing=server` 为代签路径验收条件。自有 Cash / Credit 交接记录当时四组尚无 Polymarket deposit wallet 或代签记录，未调用外部 `prepare` / `submit`。此后四组已部署 deposit wallet，A2 的授权、代签和实际成交见 [Polymarket 外部订单簿](#polymarket-外部订单簿)。
3. **纯 Credit。** 账号需人脸验证通过、`credit_locked=false`，可用且未过期的 Credit 覆盖预算，并有对应批次、登录会话和可成交盘口。以 `use_credit=true` 准备，确认 `legs` 全为 `credit`、`cash_leg` 为空且没有 `offset` 后，只提交 `{"plan_id":"<本轮计划 ID>"}`，由服务端使用托管钱包签 Credit 腿。提交后核对 `credit_reserved`、各腿的 `results` 和实际订单状态。如同时出现 Cash 和 Credit 腿，按混合单记录，Cash 腿根据回包选择客户端签名或服务端代签。

### 首轮 A/B2 下单与对账

- **盘口。** 活动 `1601693` 的自有子盘口 `4828026`（`task_one dispute`），`condition_id=0x12c6f995e4c06f239ce5794ef3f5f95cf2b8333171d7f4e2ffc1093f0d7fe2fe`，NO 的 `token_id=75261210676100639389204572555822557915086305149470255070100474248204804001538`。 [事件子盘口接口](https://preview-api.buzzing.app/api/v1/events/1601693/markets?lean=1)
- **请求。** 两组各买一次 NO，按各自 `maker_address` 和 `signer_address` 走客户端签名。`prepare` 使用该 `condition_id` 和 NO 的 `token_id`，传 `venue=buzzing`、`side=BUY`、`order_type=LIMIT`、`price="0.88"`、`total_budget="2000000"`、`use_credit=false`、`post_only=false`。`total_budget` 以 6 位 raw 表示含费的 `2` 测试 USDB，使用该字段时省略 `quantity`。首轮只接受一条 Cash 腿。
- **盘口复查。** 历史复查的 NO 卖一为 `0.88`、约 `201.343018` 份。每组在下一轮 `prepare` 前调用 `GET /api/v1/orderbook/depth?source=orderbook&position_id=<NO token_id>`，以当时 `0.88` 及以下的可成交深度为准，深度不足即停。
- **成交与撤单。** 脚本约每 4 秒查单一次。提交约 10 秒后如仍为 `pending` 或 `partial`，可用 `POST /api/v1/orderbook/order/cancel` 提交 `{"order_id":"<本轮自己的订单 ID>"}`，仅撤本轮余单。部分成交验收要求成交数量为正、链上已结算、余单撤销、冻结归零，并且成交数量和含费扣款对平。零成交判失败。A 和 B2 两笔分别于 2026-11-23 北京时间 14:09 和 14:11 提交并全部成交。 [首轮真实成交和对账](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45122061815)
- **失败与超时。** 本轮 Buzzing Cash 的 `prepare` 只生成短期计划。校验失败时停在 `submit` 前，等待计划过期，回查挂单和 `usdb_frozen_amount` 与期初一致。`submit` 超时后先按原逻辑订单 ID 或订单哈希查结果，原流程最多对账约 145 秒。仍无法确认时保留原证据并停止，禁止新建计划补单或提前撤销仍可能用于结算的授权。`plan_id` 用于提交和同计划去重，下单结果按 `logical_order_id` 或 `order_hash` 查询。 [执行与对账脚本 `no_live.py`](https://github.com/Buzzing-Club/buzzing_backend/blob/c26f2477c10a64b28f06b17e84fccee538a2706e/tests/preview_api/no_live.py)

| 查证内容 | 入口和关键字段 |
| --- | --- |
| 订单与各腿 | `GET /api/v1/orderbook/orders?source=orderbook&logical_order_ids=<逻辑订单 ID>`，也可按 `order_hash` 精查。保留 `logical_order_id` 与各腿的订单 ID。 |
| 本人成交 | `GET /api/v1/orderbook/trades?condition_id=<condition_id>&position_ids=<NO token_id>`。按订单 ID 或哈希关联，记录实际成交量、`fill_key`、`quote_amount`、`fee_amount`、`tx_hash`。 |
| 钱包和持仓 | 通过 `GET /api/v1/user/info` 核对 `polymarket.usdb_balance` 和 `usdb_frozen_amount`，通过 `GET /api/v1/orderbook/buying-power` 和 `GET /api/v1/orderbook/positions` 核对购买力、持仓的前后变化。`GET /api/v1/user/activities?source=orderbook` 可查活动，精确对账以成交明细、前后余额和链上回执为准。 |

### Cash 轮换、纯 Credit 与下一轮分配

- **Cash。** A/B2 首轮和 A2/B3 轮换各成交一次。每个账号买到 `2.066100` 份 NO，扣款 `1.839986` 测试 USDB（含手续费），完成时各剩 `1.160014` 测试 USDB。四组均无剩余挂单和冻结，临时授权已撤销，成交与仓位留存。 [A/B2 首轮](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45122061815) · [A2/B3 轮换](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45133511720)
- **纯 Credit。** 四组曾各发 `3` Credit。A 和 B2 随后各成交 `2.066100` 份 NO，消耗 `1.839986` Credit，历史可用额各剩 `1.160014`，Cash 各维持 `1.160014` 测试 USDB。A 使用批次 `151`、订单 `23efd464-d5c6-415c-8898-5f7f3ba6bf77`，B2 使用批次 `152`、订单 `89aa549c-a528-45b2-8cf0-59e991393b51`。此记录取样时 A2、B3 各自的 `3` Credit 未使用。这两笔纯 Credit 与第 6 版正文中 Lance `31543` 的历史成交是不同样本。 [A/B2 纯 Credit 成交](https://github.com/Buzzing-Club/buzzing_backend/actions/runs/45202461921)
- **下一轮分配。** 首轮交接时，新一批数据尚未发放，也没有自助领取入口。由交接方给 Lance 提供每个并发 worker 的独立登录身份、UID、签名钱包、已绑定的交易钱包和预算，并协调测试资金。原 CI 入口限制固定窗口和一次性数据，复跑先配新账号、钱包及窗口，再以新会话核对可用购买力覆盖预算、冻结额为 `0`、该盘口无仓位或挂单。随后按批准范围开启临时授权。已成交仓位和账务继续留存，恢复时只撤本轮未成交余单。
