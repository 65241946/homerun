# HOMERUN Online Settlement Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变策略阈值、仓位决策和实盘权限的前提下，为 HOMERUN 建立统一、幂等、可恢复、可审计的 Polymarket 在线终局事实、Shadow 结算和 Live 核验链，并先以只读方式预览历史卡单补偿。

**Architecture:** 订单创建时持久化 typed market identity；独立冷 reconciliation 平面在数据库事务外抓取官方终局证据，在短事务内保存 append-only observation 和当前 resolution fact；settlement coordinator 锁定订单/账户并以唯一结算凭证驱动 Shadow 模拟账本，Live 只推进核验/领取状态，不把 Gamma 终局伪装成到账。所有经济效果通过不可变现金 journal 和账户投影对账，旧路径保留兼容入口但不得绕过 v2 协调器。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2 async、PostgreSQL 16、Alembic、pytest/pytest-asyncio、Docker Compose。

---

## 实施约束与统一验收口径

- 当前由根代理单线程内联实施，不启动多模型或子代理。
- 不改变策略参数、信号筛选、Kelly/仓位计算、下单时机、Live 私钥权限或资金上限。
- 不在交易热路径执行 Gamma/Data API REST；联网必须发生在冷 reconciliation worker 且数据库连接已释放。
- 历史订单默认只生成补偿预览；本计划执行结束前不自动 apply，也不启用 Live 模式。
- 每个任务必须先新增或修改测试并观察目标失败（RED），再改生产代码（GREEN）。
- 金额写入使用 `Decimal` 与 PostgreSQL `NUMERIC`；USDC 最终对账统一量化为 6 位小数。
- 当前工作树已有未提交改动。每次修改前运行目标文件的精确 `git diff -- path/to/target.py`，只追加本任务 hunk，不回退既有内容。
- 运行测试统一在仓库根目录执行：

例如运行单个结算测试文件：

```powershell
$env:PYTHONPATH=(Resolve-Path backend).Path
py -3.12 -m pytest backend/tests/test_online_resolution.py -q
```

## 文件映射

| 类别 | 文件 | 责任 |
|---|---|---|
| 配置 | `backend/config.py` | `off/observe/shadow/live` 运行模式及校验 |
| 容器配置 | `.env.example`, `docker-compose.yml` | 将运行模式显式传入各后端容器 |
| 数据模型 | `backend/models/database.py` | typed identity、resolution、settlement、cash journal 与账户账本状态 |
| 迁移 | `backend/alembic/versions/202608100002_online_settlement_ledger.py` | 从实际 head `202608100001` 增量迁移 |
| 身份服务 | `backend/services/market_identity.py` | 无网络、无猜测的市场身份规范化 |
| 订单入口 | `backend/services/trader_orchestrator_state.py` | 在统一 order builder 持久化 identity |
| 终局事实 | `backend/services/online_resolution.py` | 官方 payload 规范化、证据哈希、观测和冲突 |
| 现金账本 | `backend/services/simulation_ledger.py` | append-only journal、账户锁、投影重建与完整性检查 |
| 结算协调 | `backend/services/settlement_coordinator.py` | 扫描、预取、claim、幂等结算、状态机 |
| 模拟兼容 | `backend/services/simulation.py` | 开仓/平仓接入 v2 journal，保留既有收益公式 |
| 生命周期 | `backend/services/trader_orchestrator/position_lifecycle.py` | v2 订单不再走旧的独立终局写账路径 |
| Live 核验 | `backend/services/polymarket_trade_verifier.py` | 将现有链上/钱包核验结果回写 settlement 状态 |
| 调度 | `backend/workers/trader_reconciliation_worker.py` | 冷平面 post-cycle 的独立 settlement cycle |
| 运维 API | `backend/api/routes_simulation.py` | 健康、账本完整性、历史补偿 preview/apply |
| 测试 | `backend/tests/test_online_settlement_schema.py` 等 | schema、纯函数、并发、API、worker、回归 |

---

### Task 0：冻结运行基线与回滚证据

**Files:**
- Modify: `progress.md`
- Create (运行产物，不提交): `data/runtime/settlement-baseline-*`

- [x] 记录当前分支、提交、目标文件 diff 和运行容器镜像 ID。

```powershell
git branch --show-current
git rev-parse HEAD
git status --short --branch
git diff -- backend/models/database.py backend/services/simulation.py backend/services/trader_orchestrator/position_lifecycle.py backend/api/routes_simulation.py docker-compose.yml
docker compose ps
docker inspect homerun-backend --format '{{.Image}}'
```

- [x] 对 PostgreSQL 做只读 schema/head/行数快照并生成压缩备份；不恢复、不删库。

```powershell
$settlementRunStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$settlementBaselineDir = Join-Path 'data/runtime' "settlement-baseline-$settlementRunStamp"
New-Item -ItemType Directory -Path $settlementBaselineDir | Out-Null
docker exec homerun-postgres pg_dump -U homerun -d homerun -Fc -f /tmp/homerun-pre-settlement.dump
docker cp homerun-postgres:/tmp/homerun-pre-settlement.dump (Join-Path $settlementBaselineDir 'homerun-pre-settlement.dump')
docker exec homerun-postgres psql -U homerun -d homerun -Atc "select version_num from alembic_version"
```

- [x] 重跑既有基线测试并把精确结果写入 `progress.md`。

```powershell
$env:PYTHONPATH=(Resolve-Path backend).Path
py -3.12 -m pytest backend/tests/test_simulation_orchestrator_ledger.py backend/tests/test_polymarket_verifier_two_phase.py backend/tests/test_trader_orchestrator_shadow_backfill.py -q
```

**Gate:** migration head 必须为 `202608100001`；现有 15 项聚焦测试必须仍通过。否则停止实施并先解释基线漂移。

---

### Task 1：增加 additive schema 与 ORM，不触碰运行逻辑

**Files:**
- Create: `backend/tests/test_online_settlement_schema.py`
- Create: `backend/alembic/versions/202608100002_online_settlement_ledger.py`
- Modify: `backend/models/database.py`

- [x] RED：先写 schema 测试，要求以下结构存在且约束精确：
  - `trader_orders`: `venue`, `provider_market_id`, `condition_id`, `token_id`, `outcome_index`, `identity_status`。
  - `simulation_accounts`: `ledger_version`, `ledger_integrity_status`, `ledger_verified_at`；状态限定为 `legacy/complete/checkpointed/mismatch/blocked`。
  - `online_market_resolution_observations`: append-only 证据，唯一 `(provider, condition_id, evidence_hash)`。
  - `online_market_resolutions`: 当前事实，唯一 `(venue, condition_id)`。
  - `trader_order_settlements`: 唯一 `(trader_order_id, settlement_kind)` 与唯一 `idempotency_key`。
  - `simulation_cash_ledger_entries`: 唯一 `idempotency_key` 与 `(account_id, ledger_sequence)`。

```powershell
py -3.12 -m pytest backend/tests/test_online_settlement_schema.py -q
```

Expected RED: ORM table/column/unique constraint 尚不存在。

- [x] GREEN：在 ORM 中新增模型和列。金额字段使用 `Numeric(20, 6)`；价格/数量证据使用 `Numeric(38, 18)`；时间字段复用项目 `UTCDateTime`（数据库存 UTC-naive、Python 返回 UTC-aware），避免与现有 schema/driver 约定漂移；JSON 原始证据只进入 observation，不复制进 cash journal。
- [x] GREEN：新增迁移 `revision="202608100002"`, `down_revision="202608100001"`，使用 `safe_add_column`、`safe_create_table`、`safe_create_index`；downgrade 按外键反序移除表和列。
- [x] 明确现有账户迁移值：`ledger_version=1`, `ledger_integrity_status='legacy'`；新建账户的应用默认值将在 Task 5 设为 v2，不把历史聚合余额伪装成完整账本。
- [x] 运行 schema、head round-trip 和完整 base replay。

```powershell
py -3.12 -m pytest backend/tests/test_online_settlement_schema.py -q
py -3.12 -m pytest backend/tests/test_alembic_roundtrip.py::test_head_migration_downgrade_upgrade_roundtrip -q
py -3.12 -m pytest backend/tests/test_alembic_roundtrip.py::test_alembic_replay_base_to_head_on_empty_db -q
```

**Rollback:** 代码回滚 migration 文件前，只有在新表无业务行且运行模式仍为 `observe` 时才允许 downgrade 到 `202608100001`。

---

### Task 2：建立 typed market identity，消除数字字符串猜测

**Files:**
- Create: `backend/services/market_identity.py`
- Create: `backend/tests/test_market_identity.py`
- Modify: `backend/services/trader_orchestrator_state.py`
- Modify: `backend/services/trader_orchestrator/order_manager.py`
- Modify: `backend/tests/test_trader_order_manager_live.py`（pre-wire identity gate 契约）
- Verify: `backend/tests/test_trader_data_access_and_strategy_sdk.py`

- [x] RED：定义并测试不可变 `MarketIdentity`：`venue`, `provider_market_id`, `condition_id`, `token_id`, `outcome_index`, `status`, `reason`；状态限定为 `complete/legacy_inferred/ambiguous/invalid`。
- [x] RED：覆盖身份优先级：显式顶层字段 > `live_market` > `market` > `markets[0]` > signal payload；冲突或多解返回 `ambiguous`，字段格式非法返回 `invalid`。
- [x] RED：验证 Gamma 数字 ID（如 `3412941`）只能进入 `provider_market_id`，绝不被当作 token ID；condition ID 必须是 `0x` + 64 hex；token ID 必须来自明确 token 列表/字段。
- [x] RED：二元市场依据 token 列表和 direction 确定 outcome index；真正多结果市场若无法唯一映射则保持 `ambiguous`，不猜 YES/NO。

```powershell
py -3.12 -m pytest backend/tests/test_market_identity.py -q
```

Expected RED: 模块不存在。

- [x] GREEN：实现纯函数 `resolve_market_identity(...)` 和 `identity_from_order(...)`，禁止网络 I/O。
- [x] GREEN：只在统一 `build_trader_order_row` 中调用该服务并写 typed columns；所有快速/会话下单调用方继续复用该 builder，不分别打补丁。
- [x] GREEN：在公共 `submit_execution_leg` 的网络/余额/盘口调用前增加纯内存 identity gate；缺 identity 的 live/shadow 订单分别 failed/skipped，executed notional 固定为 0。
- [x] 对旧订单仅在读取时从完整 `payload_json` 解析，并标记 `legacy_inferred`；不得把 `market_id` 的裸数字回填为 token ID。
- [x] 回归现有 order builder、order manager、fast submit、session engine 测试。

```powershell
py -3.12 -m pytest backend/tests/test_market_identity.py backend/tests/test_trader_data_access_and_strategy_sdk.py -q
py -3.12 -m pytest backend/tests/test_trader_order_manager_live.py backend/tests/test_order_manager_slippage_gate.py -q
py -3.12 -m pytest backend/tests/test_execution_session_engine.py backend/tests/test_fast_trader_runtime.py -q
```

**Gate:** identity 非 `complete` 时可保留 rejected/failed 审计订单，但不得进入 executed/open 或结算；校验是纯内存逻辑，不得引入交易热路径 REST。

---

### Task 3：规范化官方终局证据与严格 finality

**Files:**
- Create: `backend/services/online_resolution.py`
- Create: `backend/tests/test_online_resolution.py`

- [x] RED：测试 Gamma payload 规范化为 `ResolutionObservation`，字段包含 provider、provider market ID、condition ID、token/outcome 对齐、closed、accepting_orders、UMA 状态、winner、provider 时间和 canonical evidence hash。
- [x] RED：严格终局规则：只有 `closed=true`、`accepting_orders=false`、resolved、token/outcome 唯一对齐且 winner 唯一时为 `final`。
- [x] RED：没有显式 winner 时，仅允许完全 `1/0` 的二元价格向量作为终局；`0.98/0.02`、缺 token、长度不一致、两项同为 1、仅到期但仍 active 均为 `pending/invalid`。
- [x] RED：winning token ID 优先于 label；Up/Down、YES/NO、体育队名均不得硬编码成同一语义。
- [x] RED：同一规范化 JSON 产生稳定 SHA-256；字段顺序变化不改变 hash，经济字段变化必须改变 hash。

```powershell
py -3.12 -m pytest backend/tests/test_online_resolution.py -q
```

Expected RED: 模块不存在。

- [x] GREEN：实现纯规范化器和 `GammaResolutionProvider`。provider 接收注入的 `PolymarketClient`/HTTP fetcher，单请求 8 秒，调用层并发控制 4；该模块不自行持有数据库 session。
- [x] GREEN：复用现有 `_extract_market_info` 的字段适配能力，但把 finality 判断收敛在本模块，旧 lifecycle 的近似价格推断不得成为 v2 终局事实。
- [x] 运行纯函数测试和既有 Polymarket 解析回归。

**Result:** 首轮 RED 为模块不存在；安全边界二次 RED 精确暴露 5 项问题（陌生 label 覆盖 token、三元 one-hot 误终局、condition alias 冲突未拒绝、provider condition/market 串单）。修正后聚焦及既有解析回归 `52 passed in 0.65s`；当前 Gamma 最新 3 个已关闭市场只读烟测均为 `final`，winner token 与对齐 token 列表一致。

```powershell
py -3.12 -m pytest backend/tests/test_online_resolution.py backend/tests/test_polymarket_trade_verifier.py -q
```

---

### Task 4：持久化 observation/current fact，并冻结冲突终局

**Files:**
- Modify: `backend/services/online_resolution.py`
- Create: `backend/tests/test_online_resolution_store.py`

- [x] RED：使用隔离 Postgres 测试 observation 去重、pending→final 单向推进、相同 final 重放不新增经济效果。
- [x] RED：测试 final 后出现不同 winner/evidence 时，current fact 进入 `conflicted`，保留旧 final 和新 observation，禁止自动覆盖 winner。
- [x] RED：测试事务失败后 observation/current fact 均不出现半写；DB 连接只在持久化阶段持有。

```powershell
py -3.12 -m pytest backend/tests/test_online_resolution_store.py -q
```

Expected RED: store API/模型行为不存在。

- [x] GREEN：实现 `persist_observation(session, observation, commit=False)`；对 current fact 使用行锁或 PostgreSQL原子 upsert，并明确 caller 拥有 commit。
- [x] GREEN：重复证据通过唯一约束返回已存在结果；冲突以业务状态记录，不依赖吞掉 `IntegrityError`。
- [x] GREEN：所有异常向上抛出，worker 层负责节流日志与重试；不得 `except Exception: pass`。

**Result:** 首轮 RED 为 store API 不存在；后续防篡改 RED 证明 typed market ID 可与旧 hash 脱节。实现采用确定性 row ID、observation `ON CONFLICT DO NOTHING`、current fact 原子初始化 + `FOR UPDATE` 状态推进；联合测试 `25 passed in 47.32s`。PostgreSQL 触发器故障注入确认 observation/current fact 在 current insert 失败时均为 0 行。

```powershell
py -3.12 -m pytest backend/tests/test_online_resolution_store.py backend/tests/test_online_resolution.py -q
```

---

### Task 5：建立 Shadow v2 append-only 现金 journal

**Files:**
- Create: `backend/services/simulation_ledger.py`
- Create: `backend/tests/test_simulation_cash_ledger.py`
- Modify: `backend/services/simulation.py`

- [x] RED：新建账户默认为 `ledger_version=2`, `ledger_integrity_status='complete'`，且 `current_capital == initial_capital + SUM(journal.amount_usdc)`。
- [x] RED：开仓在同一事务写 `entry_debit`（负的 entry cost）和账户投影；唯一键 `shadow-open:{trade_id}` 重放不重复扣款。
- [x] RED：平仓写 `settlement_credit`（净 payout）并更新 total PnL/胜负计数；唯一键由 settlement ID 派生。
- [x] RED：journal ORM 层阻止 UPDATE/DELETE；业务更正只能追加 `reversal`，其 `reversal_of_entry_id` 必填且金额相反。
- [x] RED：旧账户保持 `legacy`；允许经显式补偿记录 checkpoint/repair delta，但完整性结果必须是 `checkpointed`，不能显示为完整可重建。

```powershell
py -3.12 -m pytest backend/tests/test_simulation_cash_ledger.py -q
```

Expected RED: journal service/table行为不存在。

- [x] GREEN：实现账户 `SELECT ... FOR UPDATE`、递增 `ledger_sequence`、唯一 idempotency key 和 Decimal 量化。
- [x] GREEN：抽取既有 close 纯计算，不改变现有公式：`gross=quantity*close_price`、winner fee/explicit fee 继续沿用 `close_orchestrator_shadow_fill` 当前语义；本任务只改变原子性与可审计性。
- [x] GREEN：`record_orchestrator_shadow_fill` 和 v2 close 在 caller 事务中 flush；commit 所有权显式，不提交共享 session 中无关状态。
- [x] 回归现有模拟测试，确认 UI 兼容聚合字段仍能读取。

```powershell
py -3.12 -m pytest backend/tests/test_simulation_cash_ledger.py backend/tests/test_simulation_orchestrator_ledger.py backend/tests/test_simulation_account_equity.py -q
```

---

### Task 6：实现原子、幂等、并发安全的 settlement coordinator

**Files:**
- Create: `backend/services/settlement_coordinator.py`
- Create: `backend/tests/test_settlement_coordinator.py`
- Create: `backend/tests/test_settlement_concurrency.py`
- Modify: `backend/services/simulation.py`

- [x] RED：同一订单同一 resolution 并发 20 次，只产生 1 个 settlement、1 个 credit、1 次胜负计数变化。
- [x] RED：同一账户两笔不同订单并发结算，两笔 payout/PnL 均保留，账户余额无 lost update。
- [x] RED：在 settlement insert 后、journal 前注入异常，订单/交易/持仓/settlement/journal/账户全部回滚。
- [x] RED：resolution 为 pending/conflicted、identity 非 complete/legacy_inferred、trade/position 引用缺失或订单非 Shadow 时明确返回 manual review，不改变余额。
- [x] RED：重启重放 100 次经济结果完全一致，按 6 位 USDC 对账差额为 `0.000000`。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_coordinator.py backend/tests/test_settlement_concurrency.py -q
```

Expected RED: coordinator 不存在。

- [x] GREEN：候选扫描只返回 ID/identity，不联网；网络预取在 session 外完成；apply 短事务使用非阻塞 transaction advisory claim，随后保持 account → trade → position → order → settlement 锁顺序，等价避免多 worker 重复领取且不倒置经济行锁。
- [x] GREEN：先创建唯一 settlement intent，再写 journal 和兼容投影，最后把 settlement/order/trade/position 标为终态；整个经济效果一个事务提交。
- [x] GREEN：唯一冲突视为 idempotent replay 后重新读取现有 settlement，不把 DB 异常当成功。
- [x] GREEN：settlement status 限定为 `detected`, `applied`, `projected`, `verified`, `manual_review`, `reversed`, `failed`；终局冲突属于 resolution state，settlement 写 `manual_review` 并使用稳定 reason code/detail。
- [x] 保留 `close_orchestrator_shadow_fill` 兼容 wrapper，但 v2 订单必须委托 coordinator；旧 `resolve_trade` 对 v2 trade 返回明确错误，避免双写入口。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_coordinator.py backend/tests/test_settlement_concurrency.py backend/tests/test_simulation_orchestrator_ledger.py -q
```

---

### Task 7：切断 v2 订单的旧 lifecycle 终局写账路径

**Files:**
- Modify: `backend/services/trader_orchestrator/position_lifecycle.py`
- Modify: `backend/tests/test_trader_position_lifecycle_resolution.py`
- Modify: `backend/tests/test_trader_orchestrator_shadow_backfill.py`

- [x] RED：v2 Shadow 订单进入 legacy reconcile 时只记录 `settlement_pending`/保持 open，由 coordinator 结算；不得直接调用 simulation close。
- [x] RED：legacy v1 测试账户仍保持现有兼容行为，避免一次升级破坏普通模拟器。
- [x] RED：热路径 `hot_path_no_rest()` 下 v2 逻辑不进行 REST，也不因 identity incomplete 猜测终局。

```powershell
py -3.12 -m pytest backend/tests/test_trader_position_lifecycle_resolution.py -k "shadow or resolution" -q
py -3.12 -m pytest backend/tests/test_trader_orchestrator_shadow_backfill.py -q
```

Expected RED: v2 订单仍会进入旧 close path。

- [x] GREEN：在明确的 ledger/version gate 上旁路旧终局写账，仅保留 mark/风险和 legacy 兼容；不要删掉旧功能。
- [x] GREEN：旧 price inference 仅作 legacy operational hint，不得写入 `online_market_resolutions`。

---

### Task 8：把 settlement cycle 接入冷 reconciliation worker

**Files:**
- Modify: `backend/config.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `backend/workers/trader_reconciliation_worker.py`
- Create: `backend/tests/test_settlement_worker_cycle.py`
- Modify: `backend/tests/test_docker_compose_worker_planes.py`
- Modify: `backend/tests/test_workers_host.py`

- [x] RED：配置仅接受 `off|observe|shadow|live`，默认 `observe`；Compose 所有 backend plane 可见该变量。
- [x] RED：只有 `_IS_COLD_RECONCILE_PLANE` 调用 settlement cycle；trading/news/discovery 等平面均不运行。
- [x] RED：`observe` 只抓取/持久化证据并报告 would-settle；`shadow` 才能写 Shadow；`live` 仍需 credentials/readiness gate；`off` 不联网不写入。
- [x] RED：每轮最多 100，HTTP concurrency=4、per request=8s、cycle timeout 独立；网络调用期间测试 session 未处于事务。
- [x] RED：settlement 超时/异常只更新独立 stats 和结构化错误，不使 trader reconciliation 主周期失败。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_worker_cycle.py backend/tests/test_docker_compose_worker_planes.py backend/tests/test_workers_host.py -q
```

Expected RED: 配置和调度不存在。

- [x] GREEN：在 cold worker post-cycle 单实例后台调用 `run_online_settlement_cycle`，返回 `observed/final/blocked/applied/conflicted/network_errors/duration_ms`，不阻塞 reconciliation 心跳。
- [x] GREEN：为当前未配置 Live credentials 输出稳定 `not_configured` 健康状态，并对相同错误做时间节流；不得降低 Shadow/observe 健康度。
- [x] GREEN：共享 `x-backend-env` 显式传入 `HOMERUN_SETTLEMENT_RUNTIME_MODE`，`.env.example` 默认 `observe`。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_worker_cycle.py backend/tests/test_docker_compose_worker_planes.py backend/tests/test_workers_host.py -q
docker compose config | Select-String HOMERUN_SETTLEMENT_RUNTIME_MODE
```

---

### Task 9：连接 Live 最终性、领取与到账核验状态，不启用资金权限

**Files:**
- Modify: `backend/services/polymarket_trade_verifier.py`
- Modify: `backend/workers/redeemer_worker.py`（仅状态回写 hook）
- Create: `backend/tests/test_live_settlement_state_machine.py`
- Modify: `backend/tests/test_polymarket_verifier_two_phase.py`

- [x] RED：Live 状态严格分层：`market_final`、`claimable`、`redeem_submitted`、`redeem_confirmed`、`cash_verified`；Gamma final 不能直接成为到账收益。
- [x] RED：现有 wallet/on-chain verifier 成功后以相同 idempotency key 更新 settlement verification；重复核验不重复事件。
- [x] RED：redeemer dry-run 不改变状态；真实提交缺 credentials 时保持 `not_configured`；链上确认前不得 `cash_verified`。
- [x] RED：verification mirror 写失败必须被观测/抛出，不能因静默吞异常形成“看似已核验”。

```powershell
py -3.12 -m pytest backend/tests/test_live_settlement_state_machine.py backend/tests/test_polymarket_verifier_two_phase.py -q
```

Expected RED: settlement 状态桥接不存在。

- [x] GREEN：复用现有 verifier/redeemer，不重写签名、下单和 CTF 逻辑；只增加 settlement 状态关联与审计事件。
- [x] GREEN：本阶段保持 runtime mode `observe`/`shadow`，不注入私钥，不提交 redeem。

---

### Task 10：历史补偿 preview/digest/apply 与账本完整性 API

**Files:**
- Create: `backend/services/settlement_repair.py`
- Modify: `backend/api/routes_simulation.py`
- Create: `backend/tests/test_settlement_repair_api.py`

- [x] RED：`POST /api/simulation/accounts/{account_id}/settlement-repair/preview` 只读返回 `safe/ambiguous/blocked`、订单版本、resolution evidence hash、候选 payout/PnL、总额与 `preview_digest`。
- [x] RED：preview 不抓网络，只读取已保存官方 facts；缺事实即 blocked。
- [x] RED：`POST .../apply` 必须包含 `confirm=true`、精确 order IDs 和 digest；任一订单/事实版本变化返回 HTTP 409 且零写入。
- [x] RED：重复 apply 返回同一 settlement 结果；跨账户 order ID、Live order、conflicted resolution、identity 非 complete/legacy_inferred 均拒绝。
- [x] RED：`GET /api/simulation/accounts/{account_id}/ledger-integrity` 返回 ledger version、覆盖起点、journal/projected/rebuilt balances、6 位差额和状态。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_repair_api.py -q
```

Expected RED: 路由/服务不存在。

- [x] GREEN：实现 Pydantic DTO：`SettlementRepairPreviewRequest(order_ids: list[str] | None)` 与 `SettlementRepairApplyRequest(order_ids, preview_digest, confirm)`。
- [x] GREEN：digest 使用稳定 canonical JSON，包含订单 `updated_at`、trade/position 状态、fact evidence hash、候选经济值和账户 ID。
- [x] GREEN：apply 委托同一 coordinator，不复制结算公式；legacy account 建立显式 checkpoint 并记录 repair journal delta 后状态为 `checkpointed`。
- [x] GREEN：保持 API 无自动 apply、无新前端图表；当前纽约/西雅图候选只生成 preview，等待用户另行批准。

```powershell
py -3.12 -m pytest backend/tests/test_settlement_repair_api.py backend/tests/test_settlement_coordinator.py -q
```

---

### Task 10A：补齐历史 typed identity 的只读发现与原子升级

**Files:**
- Modify: `backend/services/online_settlement_cycle.py`
- Modify: `backend/services/settlement_repair.py`
- Modify: `backend/tests/test_settlement_worker_cycle.py`
- Modify: `backend/tests/test_settlement_repair_api.py`

- [x] RED：历史订单 typed 列为空、payload 可唯一推断时，observe 应抓取并持久化官方 fact，但订单 typed 列、settlement 和 journal 保持不变。
- [x] RED：`shadow` 模式也不得把运行时 `legacy_inferred` 直接自动结算；只有已持久化安全 identity 才进入普通自动 apply。
- [x] RED：历史 preview 使用 proposed identity 匹配已保存 fact，返回候选经济值和 identity 升级信息；含糊/非法订单仍拒绝。
- [x] RED：显式 repair apply 在同一事务固化 proposed identity、checkpoint 和 settlement；摘要漂移或故障注入时全部回滚。
- [x] GREEN：候选加载在数据库 session 内只做纯函数解析，网络仍在 session 外；不扩大交易热路径。
- [x] GREEN：普通 worker 不回写 inferred identity；只有现有 repair apply 开关、confirm、精确 IDs 和 digest 四重门禁可写历史身份。

**Gate:** observe/普通 shadow 对历史订单 typed identity 必须零写入；apply 任一阶段失败必须保持订单身份、余额、settlement、journal 全部原状。

**Result:** 新增行为测试先得到 4 个预期 RED，另对 blocked 监控语义得到 2 个预期 RED；实现后 worker/repair 两组 23 项、相关身份/账本/并发/Live 回归 73 项及最终 worker 14 项全部通过。运行 observe 成功发现 14 个历史市场并保存官方事实，23 个旧订单 typed 列保持全空；普通 Shadow 只处理隔离的 2 个 `complete` 测试订单，没有自动升级历史身份。

---

### Task 11：全链兼容回归、静态审计与故障注入

**Files:**
- Modify: `progress.md`
- Modify: `findings.md`

- [x] 运行结算相关完整测试集。

```powershell
$env:PYTHONPATH=(Resolve-Path backend).Path
py -3.12 -m pytest backend/tests/test_online_settlement_schema.py backend/tests/test_market_identity.py backend/tests/test_online_resolution.py backend/tests/test_online_resolution_store.py backend/tests/test_simulation_cash_ledger.py backend/tests/test_settlement_coordinator.py backend/tests/test_settlement_concurrency.py backend/tests/test_settlement_worker_cycle.py backend/tests/test_live_settlement_state_machine.py backend/tests/test_settlement_repair_api.py backend/tests/test_simulation_orchestrator_ledger.py backend/tests/test_trader_position_lifecycle_resolution.py backend/tests/test_trader_orchestrator_shadow_backfill.py backend/tests/test_polymarket_verifier_two_phase.py -q
```

- [x] 运行 migration、worker plane、API/schema 相关回归；根据项目现有 linter 配置运行 Ruff/类型检查，若仓库无配置则记录边界，不虚报。

```powershell
py -3.12 -m pytest backend/tests/test_alembic_roundtrip.py backend/tests/test_workers_host.py backend/tests/test_docker_compose_worker_planes.py -q
py -3.12 -m ruff check backend/services/market_identity.py backend/services/online_resolution.py backend/services/simulation_ledger.py backend/services/settlement_coordinator.py backend/services/settlement_repair.py backend/tests/test_market_identity.py backend/tests/test_online_resolution.py backend/tests/test_settlement_coordinator.py
```

- [x] 扫描禁止项：结算热路径 REST、Float 新资金列、吞异常、裸数字 token 猜测、历史自动 apply、Live 自动开启。

```powershell
rg -n "except Exception:\s*pass|float\(|get_market|httpx|requests" backend/services/market_identity.py backend/services/online_resolution.py backend/services/simulation_ledger.py backend/services/settlement_coordinator.py backend/services/settlement_repair.py
$deferredMarkers = @(("TO" + "DO"), ("TB" + "D"), ("FIX" + "ME"))
Get-ChildItem backend/services/market_identity.py,backend/services/online_resolution.py,backend/services/simulation_ledger.py,backend/services/settlement_coordinator.py,backend/services/settlement_repair.py,backend/tests/test_*settlement*.py | Select-String -Pattern $deferredMarkers
```

- [x] 对失败注入、20 路并发、100 次 replay 结果留存精确测试输出；对所有失败项先修根因再继续部署。

**Result:** 按互斥分组完成 183 个唯一核心测试，全部通过；因 worker-cycle 分组重复执行一次，实际执行 195 次。覆盖 20 路同单并发 exactly-once、同账户双订单无丢更新、100 次重放、三处事务故障注入、Live 状态机、历史 repair 门禁、Alembic round-trip、worker plane 与 API 安全。新结算核心文件及迁移的 Ruff 检查通过；旧大文件保留 7 个既有 Ruff 问题，未借本任务重构。position lifecycle 组有既有 `datetime.utcnow()` 与 asyncpg cancellation 警告；worker-host 结束时有 Redis health probe pending-task 日志噪声，均未形成测试断言失败，但已列为非上线阻断的测试基础设施债务。

**Gate:** 任何 journal 差额、重复 settlement、部分提交、finality 冲突自动覆盖或 hot-path REST 都是阻断上线的问题。

---

### Task 12：固定源码重建、observe/shadow 分阶段验证与交付

**Files:**
- Modify: `progress.md`
- Create: `docs/operations/online-settlement-runbook.md`

- [x] 从当前固定源码本地构建，不拉取/运行上游浮动 latest。

```powershell
$env:HOMERUN_IMAGE_TAG='local-settlement-20260811-r2'
docker compose build backend worker-reconciliation worker-trading
docker compose up -d --no-deps backend worker-reconciliation worker-trading
docker compose ps
```

- [x] 首先设置 `HOMERUN_SETTLEMENT_RUNTIME_MODE=observe`，运行至少两个完整冷平面周期；核对 worker stats、fact/observation 行、零 settlement applied、零余额变化。
- [x] 运行当前历史订单补偿 preview，保存 safe/ambiguous/blocked 和 digest；不调用 apply。
- [x] 创建全新的 v2 模拟账户和受控测试订单，切到 `shadow`，验证一胜一负、重复 replay、worker 重启和网络超时恢复；核对 journal 与账户差额 `0.000000`。
- [x] Live 仍保持未配置/未启用；只有另一次用户明确授权、凭据安全检查和 dry-run 通过后才进入 live readiness。
- [x] 写运行手册：运行模式、健康字段、blocked reason、preview/apply、对账命令、备份路径、回滚条件和紧急停用（切回 `observe`/`off`）。
- [x] 最后运行 `git diff --check`、目标文件 diff 审计和精确测试结果汇总；当前工作树含大量前序已批准修改，未做混合提交。

```powershell
git diff --check
git diff -- backend/config.py backend/models/database.py backend/services/market_identity.py backend/services/online_resolution.py backend/services/simulation_ledger.py backend/services/settlement_coordinator.py backend/services/settlement_repair.py backend/services/simulation.py backend/services/trader_orchestrator_state.py backend/services/trader_orchestrator/position_lifecycle.py backend/services/polymarket_trade_verifier.py backend/workers/trader_reconciliation_worker.py backend/api/routes_simulation.py .env.example docker-compose.yml
```

**Result:** R2 固定镜像已部署到 backend、migrate 与全部 Python worker；数据库为 `202608100002`，API/前端 smoke 通过，15 个 worker snapshot 均 running/enabled、无 last_error。observe、历史 preview、隔离一胜一负、200 次幂等重放、进程重启和 provider 超时恢复均已取得运行库证据。用户后续单独批准精确 5 单历史 repair 和持续 Shadow：5 单原子结算后账户账本差额为 `0.000000`，repair apply 已恢复 false；冷结算持久切到 `shadow` 并通过两个完整周期，Live 仍未配置。

**最终上线门槛：**

1. 同一订单重复/并发结算只产生一次经济效果。
2. 同账户并发不同订单无余额丢失。
3. journal 可重建 v2 账户，差额固定为 `0.000000` USDC。
4. 外部网络失败、进程重启和事务故障均不会产生部分写入。
5. finality 冲突会冻结并告警，不自动选择赢家。
6. observe 模式零余额/订单写回；historical apply 未经批准不会发生。
7. Live 市场终局、可领取、链上兑换和到账核验状态明确分离。
8. 固定源码镜像健康，冷平面 settlement 指标可见，交易热路径无新增 REST 和明显延迟回归。
