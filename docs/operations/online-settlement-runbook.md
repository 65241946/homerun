# HOMERUN 在线结算与收益账本运行手册

## 1. 适用范围

本手册用于 HOMERUN 的 Polymarket 在线终局事实采集、Shadow 结算、历史补偿预览和 Live 结算核验。它不授权实盘下单、领取或历史余额写回。

四条必须同时成立的口径：

1. Gamma 显示市场终局，只代表官方结果事实可用。
2. `TraderOrderSettlement.status=applied` 才代表 Shadow 经济效果已原子写入。
3. `ledger-integrity.difference_usdc=0.000000` 且 sequence 连续，才代表 v2 模拟余额可由现金账本重建。
4. Live 只有进入 `cash_verified`，才可作为已到账实际盈亏；`market_final`、`claimable` 或链上交易已提交均不等于到账。

## 2. 安全开关

| 配置 | 允许行为 | 禁止行为 | 默认值 |
|---|---|---|---|
| `HOMERUN_SETTLEMENT_RUNTIME_MODE=off` | 不运行在线结算周期 | 联网、事实写入、结算写账 | - |
| `...=observe` | 冷平面抓取并保存官方 observation/current fact；计算 `would_settle` | 修改订单、仓位、余额和 settlement 经济状态 | 是 |
| `...=shadow` | observe 的全部能力；仅对安全的 Shadow v2 订单原子结算 | Live 资金动作、含糊身份自动结算 | 否 |
| `...=live` | 当前仅做 readiness 检查 | adapter 尚未开放；即使有凭据也返回 `adapter_not_ready` | 否 |
| `HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED=false` | 历史 preview 和 ledger integrity 只读查询 | 历史补偿 apply | 是 |

生产/实盘准备阶段不得同时打开 `shadow/live` 与历史 apply。先逐个权限面验证。

## 3. 当前已冻结基线

- 实施前 Git HEAD：`193dc3478f0f62f112e737e395e734809eb7cd3f`
- 实施前运行镜像：`local-20260810-trader-token-bridge-v6`
- 实施前数据库 migration：`202608100001`
- 目标 migration：`202608100002`
- 当前验证镜像：`local-settlement-20260811-r2`
- 当前验证镜像 ID：`sha256:9d7fe7f02aa2b40ffb954a74e9d6aee557867a5c24654f0a1cd69b36b2b00aaa`
- 当前运行 migration：`202608100002`
- 备份：`data/runtime/settlement-baseline-20260810-225157/homerun-pre-settlement.dump`
- 备份大小：`1,040,620,886` bytes
- 备份 SHA-256：`51081E5F6A04A997198D7C2448825C8582DEF1EACD9B6F46F74D48FDEF6EF947`

任何恢复前先重新执行：

```powershell
Get-FileHash .\data\runtime\settlement-baseline-20260810-225157\homerun-pre-settlement.dump -Algorithm SHA256
docker compose exec -T postgres pg_restore --list /dev/stdin < .\data\runtime\settlement-baseline-20260810-225157\homerun-pre-settlement.dump
```

Windows PowerShell 对第二条输入重定向兼容性有限；实际恢复前也可在主机使用已安装的 `pg_restore --list <path>`。不得在未校验归档时执行恢复。

## 4. 上线前门禁

在项目根目录执行：

```powershell
git status --short --branch
git diff --check
docker compose ps
Set-Location backend
py -3.12 -m alembic current
py -3.12 -m alembic heads
Set-Location ..
```

允许进入构建的条件：

- `git diff --check` 退出码为 0；LF/CRLF 提示不属于 whitespace error。
- 当前库为预期旧 head，代码只有一个新 head。
- PostgreSQL/Redis healthy。
- 上述备份文件、大小、SHA-256 和 `pg_restore --list` 均通过。
- Live 私钥不因本次操作新增；历史 apply 保持 false。

## 5. 固定源码构建

在同一个 PowerShell 会话中设置固定标签，禁止使用 `latest`：

```powershell
$env:HOMERUN_IMAGE_TAG = 'local-settlement-20260811-r2'
$env:HOMERUN_SETTLEMENT_RUNTIME_MODE = 'observe'
$env:HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED = 'false'
docker compose build backend
docker image inspect "ghcr.io/braedonsaunders/homerun-backend:$env:HOMERUN_IMAGE_TAG" --format '{{.Id}}'
```

`backend`、`migrate` 和所有 Python worker 共用同一 backend 镜像定义。构建成功不代表已经迁移或重启。
构建并验证后，应把项目 `.env` 中的 `HOMERUN_IMAGE_TAG` 固定为同一标签，避免后续新 PowerShell 会话恢复到旧镜像。不得使用 `latest`。

## 6. 迁移与 observe 分阶段启动

### 6.1 迁移

```powershell
$env:HOMERUN_IMAGE_TAG = 'local-settlement-20260811-r2'
$env:HOMERUN_SETTLEMENT_RUNTIME_MODE = 'observe'
$env:HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED = 'false'
docker compose run --rm migrate
Set-Location backend
py -3.12 -m alembic current
Set-Location ..
```

必须看到 `202608100002`。迁移失败时停止，不重启任何业务服务。

### 6.2 先启动 API 与冷结算平面

```powershell
docker compose up -d --no-deps --force-recreate backend worker-reconciliation
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Image}}'
docker compose logs --since 5m backend worker-reconciliation
```

API healthy、冷 worker 无 schema/import 错误后，再重启交易平面：

```powershell
docker compose up -d --no-deps --force-recreate worker-trading
```

恢复已停止的 worker 也使用 `docker compose up -d --no-deps <service>`。不要使用 `docker compose start <service>`：`start` 可能连同已存在但陈旧的一次性 `migrate` 容器启动，造成“当前数据库 revision 新于旧镜像”的假迁移故障。若发生此情况，先核对数据库 `alembic current`，再用当前固定镜像 `--force-recreate` migrate；不得降级数据库。

其余 Python worker 应在核心观察通过后按服务逐批重建，确保最终不混跑两个源码版本；前端未改结算 UI 时无需随 backend 标签重建。

## 7. observe 验证

在线结算间隔为 60 秒。至少观察两个完整周期，并查询 worker snapshot：

```powershell
docker compose exec -T postgres psql -U homerun -d homerun -x -c "SELECT updated_at, last_run_at, running, enabled, current_activity, last_error, stats_json->'online_settlement' AS online_settlement FROM worker_snapshot WHERE worker_name='trader_reconciliation_cold';"
docker compose logs --since 10m worker-reconciliation | Select-String 'Online settlement'
```

统计字段含义：

| 字段 | 含义 | 处理 |
|---|---|---|
| `candidates` | 本周期进入官方事实观察批次的市场数；可含只读推断的历史 identity | 可为 0；不能据此认定可自动结算 |
| `observed` | 已规范化并持久化的官方观测数 | 连续网络正常时应随候选出现 |
| `observation_inserted` | 新证据行数 | 相同证据重放为 0 是正常幂等 |
| `final` | 已确认严格终局的市场数 | 不等于已写收益 |
| `would_settle` | observe 下本可处理的订单数 | 只读预告 |
| `applied` | 本周期实际 Shadow 结算数 | observe 必须始终为 0 |
| `idempotent_replays` | 已结算事实的安全重放 | 不得产生第二次经济效果 |
| `manual_review/conflicted/blocked` | 需人工处理或安全门禁拒绝；可推断但尚未显式升级的历史订单仍计入 blocked | 不能自动放宽 |
| `network_errors` | provider 网络/超时错误数 | 连续非零才判定外部链异常 |
| `persistence_errors/apply_errors` | 本地事实/结算事务错误 | 任一非零均阻断升级到 shadow |
| `health` | `healthy/attention/degraded/disabled/...` | `degraded` 阻断；`attention` 必须逐项解释 |

observe 的数据库不变量：

```powershell
docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT state, COUNT(*) FROM online_market_resolutions GROUP BY state ORDER BY state;"
docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT COUNT(*) AS observations FROM online_market_resolution_observations;"
docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT COUNT(*) AS economic_rows FROM trader_order_settlements WHERE status IN ('applied','verified');"
docker compose exec -T postgres psql -U homerun -d homerun -c "SELECT COUNT(*) AS cash_rows FROM simulation_cash_ledger_entries;"
```

启用 observe 前后，既有订单状态、账户余额、`economic_rows` 和 `cash_rows` 必须没有因 observe 增长。事实表/观测表允许增长。

## 8. 历史修复预览（只读）

接口前缀为 `/api/simulation`：

```powershell
$accountId = '<simulation-account-id>'
$preview = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:3000/api/simulation/accounts/$accountId/settlement-repair/preview" `
  -ContentType 'application/json' `
  -Body '{"order_ids":null}'
$preview | ConvertTo-Json -Depth 20
```

`safe` 表示在当前订单版本和已持久化 final fact 下可计算，不等于已批准写回。`ambiguous` 和 `blocked` 的常见 reason：

- `identity_not_settlement_safe`
- `resolution_fact_missing`
- `resolution_not_final`
- `resolution_conflicted`
- `resolution_winner_incomplete`
- `identity_*_mismatch`
- `simulation_ledger_reference_missing/changed`
- `simulation_ledger_row_missing`
- `order_account_mismatch`
- `unsupported_order_mode`

保持 apply 开关为 false 时，调用 apply 应返回 HTTP 403。不得为了“让收益显示出来”临时打开开关。

## 9. Shadow 验证门槛

只有 observe 连续两个周期满足以下条件，才可把冷 worker 切换为 `shadow`：

1. `persistence_errors=0`、`apply_errors=0`、无无法解释的 conflict。
2. observe 前后订单/余额/settlement/cash journal 经济行零变化。
3. 事实 identity 与订单 condition/token/outcome 完整对齐。
4. 新建隔离 v2 模拟账户，不复用用户现有账户。
5. 使用一笔已知胜、一笔已知负的受控 Shadow 订单；不得注入 Live 订单。

验证后检查每个账户：

```powershell
$accountId = '<isolated-v2-account-id>'
Invoke-RestMethod "http://127.0.0.1:3000/api/simulation/accounts/$accountId/ledger-integrity" | ConvertTo-Json -Depth 10
```

必须满足：

- `ledger_version=2`
- `status=complete`
- `coverage=complete`
- `difference_usdc=0.000000`
- `sequence_contiguous=true`
- 同一订单重复运行 100 次后 settlement 数、journal 数、余额和胜负计数不再变化
- worker 重启、一次 provider 超时恢复后仍满足以上不变量

任一不满足，立即切回 `observe`，保留证据，不手工改余额。

## 10. Live 状态与禁止事项

Live 状态顺序：

`market_final -> claimable -> redeem_submitted -> redeem_confirmed -> cash_verified`

禁止事项：

- 不凭 Gamma final 写 `actual_profit`。
- 不把共享钱包未归属的 SELL/FIFO 结果分配给机器人订单。
- 不在 dry-run 中写 redemption 状态。
- 不在 receipt 成功前标记 `redeem_confirmed`。
- 不在可归属净回款与成本无法对账时标记 `cash_verified`。
- 本版本 `live` runtime 仍为 readiness fail-closed，不得绕过 `adapter_not_ready`。

## 11. 告警与阻断条件

以下任一情况阻断 Shadow/Live 升级：

- journal `difference_usdc != 0.000000` 或 sequence 不连续
- 同一订单出现多个经济 settlement/journal effect
- final winner 冲突被自动覆盖
- observe 产生 `applied > 0`
- cold worker 出现 persistence/apply error
- 新代码在交易热路径进行 Gamma/REST 终局查询
- migration head 非唯一或不为预期值
- 运行平面混用不可追溯的 `latest` 镜像

## 12. 紧急停用与回滚

### 12.1 首选：关闭结算写入，不回滚 schema

```powershell
$env:HOMERUN_IMAGE_TAG = 'local-settlement-20260811-r2'
$env:HOMERUN_SETTLEMENT_RUNTIME_MODE = 'off'
$env:HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED = 'false'
docker compose up -d --no-deps --force-recreate worker-reconciliation
```

若仍需保留官方事实采集，将 `off` 改为 `observe`。这是首选恢复方式，因为不破坏已保存证据和账本。

### 12.2 代码镜像回滚

新 migration 为 additive。确认新 worker 已停止后，可把 API/worker 切回冻结旧标签，暂不 downgrade 数据库：

```powershell
$env:HOMERUN_IMAGE_TAG = 'local-20260810-trader-token-bridge-v6'
docker compose up -d --no-deps --force-recreate backend worker-reconciliation worker-trading
```

### 12.3 数据库恢复

只有出现无法通过 append-only reversal/修复处理的结构性损坏，才考虑停全栈后恢复完整备份。恢复会覆盖备份时间点后的全部数据，属于破坏性操作，必须再次获得明确批准。不得在运行服务仍连接数据库时执行，不得直接自动运行 `alembic downgrade` 丢弃新事实/账本表。

## 13. 每次运行记录

每次迁移、observe、shadow 或回滚都应记录：

- 时间、操作者、Git commit/worktree diff 摘要
- 固定镜像 tag 与 image SHA
- migration before/after
- runtime mode 与 repair apply 开关
- worker 两个周期的完整 `online_settlement` stats
- facts/observations/settlements/journal before/after 计数
- 账户 ledger integrity 结果
- 失败日志、决策与回滚动作

没有这些证据，不得把面板上的收益数字用作策略有效性结论。

### 2026-08-11 R2 验证记录

- 固定镜像：`local-settlement-20260811-r2`，ID `sha256:9d7fe7f02aa2b40ffb954a74e9d6aee557867a5c24654f0a1cd69b36b2b00aaa`；镜像内关键源码 SHA-256 与主机一致，且不含 `.env`。
- migration：`202608100002 (head)`；R2 `migrate` 容器重建后退出码 0。
- observe 周期 1：`candidates=14, observed=14, observation_inserted=14, final=5, blocked=22, applied=0`，三类错误均为 0。
- observe 周期 2：`candidates=14, observed=9, observation_inserted=2, final=5, blocked=22, applied=0`，三类错误均为 0。
- 既有账户 `4473f507-5e6e-4269-b04a-de4a967284a0` 在 observe 前后保持 `current_capital=919.403948315238`、`total_pnl=0`；历史订单 typed 列保持 0，settlement/journal 保持 0。
- 历史 preview：`safe=5, ambiguous=1, blocked=11`，digest `fc87957021f43e2edafb600c1fb37057c503de01f3f55f6e65e111dacd332042`；候选净派彩 `26.147042`、候选已实现盈亏 `3.054009`。未调用 apply。
- 隔离 v2 账户 `settlement-validation-r2-20260811`：一胜 `+147.000000`、一负 `-100.000000`，最终余额 `1047.000000`、净盈亏 `47.000000`；2 条 settlement、5 条 journal，`difference_usdc=0.000000`、sequence 连续。
- 对两笔终局订单执行 100 轮、共 200 次 coordinator 重放后，余额、胜负计数、settlement 数和 journal 数均不变。
- provider 超时注入产生 `network_errors=9, health=degraded, applied=0`；重启后真实周期恢复为 `network_errors=0`，经济数据未变。
- 最终运行态：backend、migrate 和全部 Python worker 使用 R2；冷结算为 `observe`，historical apply 为 `false`，Live 凭据未配置。

### 2026-08-11 授权 repair 与 Shadow 启用记录

- 授权边界：只处理预先批准的 5 个历史 order ID；全量 preview 必须仍为 `safe=5, ambiguous=1, blocked=11`，定向 preview 必须为 5 safe、0 ambiguous、0 blocked。
- 写入前定向备份：`data/runtime/settlement-preapply-20260811-044938/homerun-settlement-preapply.dump`，custom-format TOC 93 行，97,423 bytes，SHA-256 `D94973F075EA544982024C4E2958C85E0D0305483F227C23B21B67B08B057EB8`。
- 实际定向摘要：`6dccfec0bceaf216204bfd083021bea2cc50bf4d8db5057527e63a2a1f622e74`；净派彩 `26.147042`，已实现盈亏 `3.054009`。
- apply 结果：`checkpointed=true`、`applied_count=5`、5 条 settlement 均为 `projected`；完成后后端容器已重建并确认 `HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED=false`。
- 账户结果：余额 `945.550990`、总 PnL `3.054009`、5 胜、11 未平；ledger v2/checkpointed，6 条流水连续，`difference_usdc=0.000000`。
- 持久运行配置：`HOMERUN_SETTLEMENT_RUNTIME_MODE=shadow`、repair apply=false；backend、trading、reconciliation 均为固定 R2 镜像。
- Shadow 周期 1：`mode=shadow, observed=9, final=0, applied=0, blocked=17`，三类错误均为 0。
- Shadow 周期 2：同一候选集，`duration_ms=782, applied=0, network_errors=0, persistence_errors=0, apply_errors=0, in_progress=false`；余额、PnL、settlement 和 journal 未重复增长。
- Live 仍未配置。日志中的 `missing_polymarket_credentials` 仅代表 Live 初始化 fail-closed，不得通过补假凭据或绕过门禁消除该提示。
