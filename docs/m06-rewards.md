# M06 Rewards 接口自动化

按开发交接第 6 版，Preview 日期工具支持模拟进度，也支持在达标后显式发放可入账的 `[TEST]` Credit。`python -m function.rewards_simulation` 用于验证进度投影，`python -m function.rewards_test_payout` 用于测试发奖和对账。真实业务事件的入口为 `python -m function.rewards_flow`。

[计划第三节](https://trufflelabs.jp.larksuite.com/wiki/STaYwLQuzieQMSkPLqNj52SMpJg#doxjpVVn8dDQ7PQtPmi7pHYProe) · [计划第八节](https://trufflelabs.jp.larksuite.com/wiki/STaYwLQuzieQMSkPLqNj52SMpJg#doxjperWnel3wJlWyd5GvwVWhvg) · [t105673 开发交接](https://trufflelabs.jp.larksuite.com/wiki/Pt4EwV2odid8WakLKXlj6524pae#ZULIdvsPAotIlexzX45j1TrkpEd)

## 指定好友任务的后台模拟

`ci.yaml` 中 `rewards_cases.m06_friend_simulation` 同时供好友进度模拟、CI 读取和发奖使用。配置对应 Preview 账号 `tester1`（UID `991039`）及交接中的 `QA1116-06-Friends-Once`，任务 ID `8d1c5468b058886d6aa902db2d5f5e99`，版本 2。

脚本使用 Preview 日期工具创建隔离场景，在场景内模拟邀请人完成 1,000 笔交易的解锁条件，先保存虚拟好友 App 登录，再补齐首充。它核对两阶段的达标好友数 `0 → 1`、后台与用户端的场景版本及条件状态，并查询任务回执、Credit 历史和 Inbox 确认模拟推进期间没有新增奖励。管理请求使用 `expected_version` 和每阶段固定的 `Idempotency-Key`。虚拟好友属于测试投影。 [日期推进管理接口契约](https://github.com/Buzzing-Club/buzzing_backend/blob/5c2ca13d4ea1f87eee3b61599c095bed0ba44182/proto/api/voucher-service/voucher/v1/rewards_date_test.proto)

历史场景版本 3 属于此前选用的任务。当前用例的场景和版本按 `QA1116-06-Friends-Once` 的 UID 与任务 ID 读取。 [Rewards 日期推进后台](https://test-admin.buzzing.app/next/tools/test/rewards-date)

### GitHub Actions 登录模式

在现有 **Buzzing API scenarios** 工作流手动选择 `preview`，取消勾选 **Only verify API access**，选择 `suite=m06_rewards`。M06 使用仓库的 `BUZZING_GMAIL_APP_PASSWORD` 和 `tester1` 登录，复用本轮用户会话。 [GitHub Actions 工作流](https://github.com/buzzingtesting/buzzing-api-tests/actions/workflows/check-framework.yml)

| `m06_operation` | 运行内容 |
| --- | --- |
| `inspect`（默认） | 读用户端 `QA1116-06-Friends-Once` 的已达标模拟好友和当前账务快照。可用 `m06_scene_version` 指定该任务的场景版本。报告为 `m06-rewards.json`。 |
| `prepare` | 使用后台日期工具构造可发奖单元，保存前置基线。选择 `m06_case=friends` 或 `daily`。 |
| `grant` | 使用后台日期工具显式发放测试 Credit，再核对到账、通知和重复提交。新跑时选择 `m06_case`，续跑时从原记录恢复用例。 |
| `reset` | 从已完成对账的运行恢复记录，退出本轮模拟场景并确认已入账的 Credit 仍在。填写发奖运行的 `m06_resume_run`。 |

写入操作从仓库 Secret `BUZZING_REWARDS_ADMIN_TOKEN` 读取后台凭证。新运行可填写 `m06_scene_version` 接管已核对的活动场景，留空则从无活动场景状态创建。操作日志在 **Artifacts → preview-scenarios**，阶段记录在 **Artifacts → m06-date-test**。

1. 首次选择 `prepare`。等工作流完成后，从该运行页面地址的 `/actions/runs/` 后复制数字 ID。
2. 另开一次 **Run workflow**，选择 `grant` 并将数字 ID 填到 `m06_resume_run`，`m06_scene_version` 留空。CI 下载同分支、同工作流的原始 `m06-date-test` 产物，恢复场景、内部运行 ID、前置基线和幂等键。
3. 如果写入响应或到账暂缺，用最近一次包含 `m06-date-test` 的工作流运行 ID 再次触发 `grant`。恢复时会验证 `ci.yaml` 与原用例一致。如果原产物缺失或已过期，先核对现有场景、发放记录和人工留存的日志。
4. 完成第一轮后，可以用 `reset` 恢复该轮发奖产物并退出场景，再为第二轮新跑 `prepare`。使用不同 CI 运行的 `result.json` 比较两轮时，选取两个不同内部运行 ID 的结果。

### 后台模拟与前后对账

通过本地密钥配置注入 `BUZZING_REWARDS_ADMIN_TOKEN`，使用具备日期推进权限的 Preview 后台凭证。用户会话复用 `tester1` 的项目收码和缓存配置，也可注入该用户的 `BUZZING_REWARDS_TOKEN`。当前活动场景可直接读回。

```sh
BUZZING_ENV=preview python -m function.rewards_simulation --inspect
```

同一 UID 和任务无活动场景时，以新的 UUID 运行两阶段模拟。用同一个 UUID 重试会复用已保存进度。已有活动场景时，脚本要求使用 `--inspect`。

```sh
BUZZING_ENV=preview python -m function.rewards_simulation --run-id 8307eff7-6396-42b9-bbd6-b12b8c82d49b
```

## 日期工具测试发奖

| 用例 | 测试数据和推进方式 |
| --- | --- |
| `friends` | 交接中的 `QA1116-06-Friends-Once`，`8d1c5468b058886d6aa902db2d5f5e99` v2。模拟邀请人前置解锁与一位好友的 App 登录及首充。每轮只发放一个 `qualified` 的 `friend:` 单元，额度为 500 cents。任务配置最多计入两位好友。 |
| `daily` | 开发交接的 `QA1116-03-Daily-Split`，`b41c1081acc145cf586c3d777943f263` v2。仅在隔离场景模拟解锁和首日交易达标，接着推进一个模拟 Manila 日期，核对此次首个奖励单元为 200 cents。 |

默认入口准备场景并保存发奖前 Credit 批次基线。指定 `--grant` 时，脚本核对场景版本、唯一可发奖单元和固定金额，再调用 `grant_rewards`。用同一个 `batch_id` 核对后台与用户任务投影的 `paid` 单元、后台 Credit 发放记录、Profile Credit 历史和唯一 Inbox 通知。Profile Credit 校验批次处于有效状态，发放额和可用额均与预期一致。后台发放记录要求 `source_type=task`、`source_ref=date-test:<task_id>`。这条路径使用 `credit_granted` 通用到账通知，批次带 `[TEST]` 名称。自然发奖的 Rewards 收据通过独立路径核对。 [日期场景发奖实现](https://github.com/Buzzing-Club/buzzing_backend/blob/5c2ca13d4ea1f87eee3b61599c095bed0ba44182/app/voucher-service/internal/service/rewards_date_test_grants.go)

以下示例从该好友任务的无活动场景状态开始。显式发奖调用复用准备阶段的运行 ID 和日志目录。接管该任务已有的活动场景时，两条命令均追加实际的 `--scene-version`。普通每日任务使用 `--case daily` 和新的 UUID。

```sh
run_id="$(python -c 'from uuid import uuid4; print(uuid4())')"
BUZZING_ENV=preview python -m function.rewards_test_payout --case friends --run-id "$run_id"
BUZZING_ENV=preview python -m function.rewards_test_payout --case friends --run-id "$run_id" --grant
```

每轮确认到账后，脚本在同一场景对同一个单元再次提交，确认批次、后台发放记录和通知均保持唯一。发奖请求超时时，原运行 ID 会复用原请求的幂等键。只有本轮已完成对账时，`--reset-completed` 才会退出该场景，并回查 Credit 批次仍然存在。接着为第二轮使用不同 UUID，好友脚本会生成不同虚拟好友键。两个 `result.json` 可用 `--compare-rounds` 验证场景和批次独立。

已通过 Preview 后台接口核对 `QA1116-06-Friends-Once` 的任务版本、好友条件、前置解锁和面额。发放、重放和新批次通知的行为已通过离线回归，现场测试 Credit 发放和两轮对账待运行。交接中好友批次 `130`、`131` 是历史到账证据。

## 真实业务事件场景

| `scenario` | 业务写入和期望 |
| --- | --- |
| `steps` | 代表性普通任务。选已上线、立即解锁、只需一次 App 登录的 `steps/once` 专用任务。一次原生登录完成任务，核对唯一 `once` 发奖单元。交接中的 `Step Reward-Once` 还要求入金和交易，因此这个入口需要单独的 App 登录型任务配置。 |
| `friends` | 选已上线、立即解锁、只要求受邀人登录 App 一次的 `friends/once` 专用任务。脚本读取邀请人的邀请码，由受邀人确认绑定。两边查到同一关系后，以受邀人身份 App 登录，核对邀请人完成的 `friend:<受邀 UID>` 单元。 |
| `daily` | 开发交接推荐的 `Daily Change - Once`，`0df5af2ae1e967f0e8fb60614b64ef71` v3。首日真实登录后核对只达标一天且未发奖。下一个 Manila 自然日续跑，第二次真实登录触发唯一 `once` 单元，发放 2 cents。该场景同时覆盖开发交接推荐的普通任务链路，可用于需要普通任务和自然日共用实际任务的安排。 |

登录使用 `POST /api/v1/login` 的 `platform=ios` 和 `expect_user_id`，携带同一 UID 已验证的会话。Identity 服务按正常流程产生 `identity.app_login.v1`。邀请人以 `GET /api/v1/referral/v2/overview` 获取邀请码，受邀人以 `POST /api/v1/referral/v2/inviter/confirm` 确认。登录路径和出站事件语义见 [Preview 原生登录映射测试](https://github.com/Buzzing-Club/buzzing_backend/blob/b3e6476157c1f4440cbcc3345706eaee06345c9a/app/bff-service/internal/service/identity_login_native_http_test.go) 和 [Identity 登录实现](https://github.com/Buzzing-Club/buzzing_backend/blob/b3e6476157c1f4440cbcc3345706eaee06345c9a/app/identity-service/internal/service/bff_grpc.go#L984)。

每轮先核对会话 UID、`payouts_enabled`、任务版本与规则、日期测试投影和期初奖励。脚本拒绝带展示 fixture 标记的任务。发奖后按同一 `task_id`、`unit_id` 和 `batch_id` 核对四处记录。

- Rewards 任务详情中的 `paid` 单元。
- 基线 revision 之后的唯一新 Rewards 回执及发放时间。
- 期初不存在的 Profile Credit 批次，金额按 1 cent = 10,000 raw 核对。
- 唯一关联的 `credit_granted` Inbox 通知，核对任务、单元、批次、金额和 `view_credit` 动作。

## 运行配置

每轮使用获准的 Preview 任务和专用用户。普通和跨日账号应无本任务历史进度，受邀人应已注册且尚未绑定。好友任务的受众和预算由 Rewards 服务交付。配置中的 `service_revision` 用于记录部署版本，按环境实际版本填写。

### 普通任务与真实跨日账号

普通任务与真实跨日共用 `Daily Change - Once` v3。2026-11-27 17:53（UTC+8）只读核对了从任务创建开始的完整 `participants` 和 `paid` 报表。A（`991090`）、B2（`991091`）、A2（`991093`）和 B3（`991094`）均无记录，任务当时仍上线，单次奖励 2 cents、可用预算 2,988 cents。建议首轮用 A、独立复跑用 B2，A2 和 B3 作为候选。账号分配沿用交接的独占规则，开始前核对分配窗口以及用户端任务进度和发奖开关。 [Preview 专用账号和登录交接](preview-api-automation-dependencies.md#专用账号钱包和登录)

这四个账号通过开发已验证的 Neo Gmail 收件箱和 Privy SDK 登录。用 `suite=trade-identity`、`account=neo`、`trade_tag=A` 或 `B2`、`trade_mode=proof` 可核对收码与账户归属。跨日执行时，在有该收件箱授权的运行环境取得对应 UID 的 Privy 身份凭证，并在进程内提供给 `BUZZING_REWARDS_PRIVY_IDENTITY_TOKEN`。若同一 UID 已有可用的 Preview 会话，也可提供给 `BUZZING_REWARDS_TOKEN`。日期推进的 `m06_rewards` 工作流使用 `tester1` 及其 Gmail Secret，交易账号的收码授权需要接入实际执行 `function.rewards_flow` 的环境。 [开发的登录工作流](https://github.com/Buzzing-Club/buzzing_backend/actions/workflows/preview-api-preflight.yml)

每轮真实跨日使用同一 UID 和运行目录。Day 1 由 M06 脚本发出真实 App 登录并核对 `1/2`，下一 Manila 自然日重新取得该账号的身份凭证，沿用原运行 ID 完成 `2/2`、新回执、Credit 和 Inbox 对账。第二轮使用独立 UID 和运行 ID。

示例保存为 `.local/rewards/daily-case.json`。替换示例 UID、账号名、run ID、服务版本和窗口。时间使用 UTC，`day_one` 使用 Manila 日期。窗口应覆盖两天实际运行时间。

```json
{
  "run_id": "8307eff7-6396-42b9-bbd6-b12b8c82d49b",
  "scenario": "daily",
  "uid": 12345,
  "task_id": "0df5af2ae1e967f0e8fb60614b64ef71",
  "task_version": 3,
  "amount_cents": 2,
  "service_revision": "APPROVED_PREVIEW_REVISION",
  "starts_at": "2026-11-27T00:00:00Z",
  "ends_at": "2026-11-29T00:00:00Z",
  "day_one": "2026-11-27",
  "beneficiary_account": "rewards_daily_r1"
}
```

- `steps` 省略 `day_one` 并填写实际步骤任务 ID、版本和奖励。
- `friends` 省略 `day_one`。将 `uid` 设为邀请人，另填 `friend_uid` 和 `friend_account`，以及实际好友任务配置。
- 通过 `beneficiary_account`、`friend_account` 引用 `ci.yaml` 专用数据组时，复用仓库的 Gmail 或 Mailosaur 自动收码。也可以从团队密钥库注入 `BUZZING_REWARDS_PRIVY_IDENTITY_TOKEN` 和 `BUZZING_REWARDS_FRIEND_PRIVY_IDENTITY_TOKEN`。注入 `BUZZING_REWARDS_TOKEN`、`BUZZING_REWARDS_FRIEND_TOKEN` 时直接验证这些会话。否则先以普通登录建立并核对会话，再携带对应会话执行 App 登录。
- 受邀人需要能完成邀请确认的用户会话。使用能在窗口内续期的邮箱账号或在第二天注入当日有效凭证。

```sh
BUZZING_ENV=preview python -m function.rewards_flow --case .local/rewards/daily-case.json
```

普通和好友各调用一次即可完成写入与对账。跨日 Day 1 结束返回 `waiting_day_2`，下一 Manila 自然日用同一用例和证据目录再次调用，完成后返回 `paid`。轮询异步状态默认 90 秒，可用 `--wait-seconds` 配置。已确认的写入保存到 `.local/rewards/<run_id>/`，后续调用只补未完成阶段。绑定回包丢失时从受邀人的关系查询恢复。App 登录结果不明时保留原请求记录，需先通过服务日志确认。

证据记录双方 UID、邀请关系、登录响应的服务端 `auth_at`、可用的请求追踪编号，以及回执、批次和通知 ID。凭证、验证码和邀请码由运行进程处理。需要核对实际 outbox 事件时，用登录 UID、`auth_at` 和请求追踪编号与服务日志关联。

## 两轮数据和原事件重投

为两轮运行分配独立的 `run_id` 及未使用的任务账号或邀请关系。自然发奖通过轮换账号和关系准备下一轮，实际账务保留。

完成发奖后，Rewards 服务可按交接方式重投同一条原始业务事件。将服务记录的 `event_id`、`type`、`uid`、`occurred_at`、`source_ref`、`delivery_id`、`delivered_at` 和 `processed_at` 存为脱敏 JSON，再调用同一入口核对原回执、批次和通知仍唯一。原事件 ID 和处理结果由服务日志提供。

```sh
BUZZING_ENV=preview python -m function.rewards_flow --case .local/rewards/daily-case.json \
  --replay-event .local/rewards/replay-record.json
```

`python -m function.rewards_run --help` 提供独立的分阶段查询入口，可用于定位手工触发样本。离线回归使用 `python -m pytest offline_tests -q`。
