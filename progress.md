# Progress Log: HOMERUN 实盘级结算链

## Session: 2026-08-10

### Phase 1: Requirements, Evidence & Existing Architecture
- **Status:** completed
- **Started:** 2026-08-10 20:36 CST
- Actions taken:
  - 核对官方 Gamma 市场状态、实际关闭时间和结算结果。
  - 核对 TraderOrder、TraderPosition、SimulationTrade、SimulationPosition 与账户快照。
  - 对同一 Shadow 结算函数完成热路径/非热路径 A/B 只读验证。
  - 对 watchdog 轻量投影与完整 ORM 订单完成市场信息加载 A/B 验证。
  - 核对运行镜像与本地三份关键源码 SHA-256 一致。
  - 核对相关代码的 Git 来源和当前脏工作树。
  - 启动文件化规划；当前没有写订单、余额或生产结算代码。
  - 初步发现可复用的离线 settlement store、scanner 身份映射、market cache、trade verifier 和 CTF 链上 payout/redeem 能力。
  - 读取既有 runtime 修复规格，确认其为最小范围方案，不覆盖本次完整在线结算架构。
  - 读取离线 MarketSettlement/settlement_store 和在线 trade verifier 的身份、并发、超时与 DB 连接边界。
  - 读取 PolymarketClient 的 condition/token 查询、缓存和市场信息规范化逻辑，确认裸数字 ID 类型猜测是系统性风险。
  - 定位模拟 Shadow close 的 existing idempotency 入口，下一步审计事务和并发边界。
  - 审计 Shadow close 事务：确认当前没有行锁或唯一结算幂等键，顺序重放虽有保护，但并发关闭会面临账户聚合丢更新和重复副作用风险。
  - 发现 `simulation.py` 存在新旧两条结算写账路径且事务所有权不同；已列为必须统一的高风险项，尚未改代码。
  - 审计 Live reconciliation 与 verifier：确认其只覆盖 live 已关闭订单，且一个市场终局 verifier 函数未接入正式调度链。
  - 审计验证 side table / event：发现双写镜像可静默失败、事件缺少唯一结算幂等约束，尚不能作为实盘账本唯一事实源。
  - 核对 worker host 与 redeemer：确认可复用独立冷 reconciliation 平面和现有链上 redemption 能力。
  - 检索模拟账本结构：确认当前只有账户聚合余额，没有可重建余额的不可变现金分录。
  - 使用系统 Python 运行现有 Shadow ledger、verifier 两阶段和 backfill 聚焦测试：15 项全部通过；这些测试验证顺序单会话行为，但未覆盖并发结算、唯一幂等凭证、余额可重建和在线终局调度。
  - 核对运行库 migration head 为 `202608100001`；核对最近 30 分钟运行日志，确认未配置 Live 凭据触发高频日志风暴，且 Shadow 周期存在 6.5–15.4 秒批量 commit 延迟。
- Files created/modified:
  - `task_plan.md`（创建）
  - `findings.md`（创建）
  - `progress.md`（创建）

### Phase 2: Architecture Options & Approved Design
- **Status:** completed
- Actions taken:
  - 已形成三档方案，推荐“冷平面统一终局协调器 + typed identity + 结算凭证 + append-only cash journal + Shadow/Live 分层适配器”。
  - 已确定历史卡单默认先生成只读补偿预览，不自动写账；等待用户批准该设计与补偿策略。
  - 用户已批准完整方案与“先只读补偿预览”历史处理方式。
  - 完成 worktree 适用性检查；因当前运行基线依赖未提交修复，决定在当前工作树按精确文件/hunk隔离实施，不创建一个缺失这些改动的不同基线。
  - 复核既有 runtime 修复规格/计划，确认其最小范围和验证粒度不足以承载在线结算体系；本次将新建独立规格与计划文件。
  - 映射模拟账户全部主要现金写路径，确定 proof-grade cash journal 先覆盖 orchestrator Shadow v2；legacy 通用模拟器保持兼容但显式降级标记。
  - 写入并自审 `docs/superpowers/specs/2026-08-10-online-settlement-ledger-design.md`；placeholder 扫描无命中，设计覆盖身份、终局事实、幂等结算、现金 journal、Live 分层、补偿和分阶段上线。
- Files created/modified:
  - `docs/superpowers/specs/2026-08-10-online-settlement-ledger-design.md`（创建）

### Phase 3: Detailed TDD Implementation Plan
- **Status:** completed
- Actions taken:
  - 开始把设计映射到精确 schema、服务、worker、API 和测试任务。
  - 确定 runtime mode 沿用 Settings 环境变量，补偿/完整性接口沿用现有 simulation router。
  - 确定 settlement cycle 挂在 reconciliation 冷平面 post-cycle，使用独立超时和统计；不进入 per-trader 或交易热路径。
  - 核对 Compose 环境透传方式，确认运行模式必须同时进入 `x-backend-env` 和 `.env.example`。
  - 核对实际 migration head、Postgres 隔离测试 helper、Alembic head round-trip/base replay 测试入口。
  - 写入并自审 `docs/superpowers/plans/2026-08-10-online-settlement-ledger.md`：13 个任务、88 个检查项；真实测试文件与 Compose 服务名已校正，placeholder 扫描和 `git diff --check` 通过。
- Files created/modified:
  - `docs/superpowers/plans/2026-08-10-online-settlement-ledger.md`（创建）

### Phase 4: TDD Implementation
- **Status:** in_progress
- Actions taken:
  - 按已批准计划进入内联执行；不使用多模型或子代理。
  - Task 0 已完成：冻结分支/HEAD/目标 diff/运行镜像；当前 HEAD `193dc3478f0f62f112e737e395e734809eb7cd3f`，容器共用本地固定镜像 SHA `7524626f...57b9`。
  - 完整 PostgreSQL 备份已写入 `data/runtime/settlement-baseline-20260810-225157/homerun-pre-settlement.dump`，大小 1,040,620,886 字节，SHA-256 `51081E5F6A04A997198D7C2448825C8582DEF1EACD9B6F46F74D48FDEF6EF947`；`pg_restore --list` 通过。
  - 备份时 migration head=`202608100001`；关键行数：TraderOrder 21、SimulationAccount 1、SimulationTrade 16、SimulationPosition 16。
  - 基线聚焦测试再次通过：15 passed in 61.08s。
  - Task 1 RED：`test_online_settlement_schema.py` 初次运行 6 项全部按预期失败，原因仅为目标列、表和 migration 不存在。
  - Task 1 GREEN：新增 typed identity、online resolution/observation、order settlement、simulation cash journal ORM 与 `202608100002` additive migration；旧账户默认 v1/legacy。
  - Task 1 验证：schema 6 passed；Alembic head downgrade/upgrade + 空库 base-to-head replay 2 passed；脚本头唯一为 `202608100002`；`git diff --check` 通过。
  - Task 2 首轮 RED：`services.market_identity` 不存在；第二轮 RED：builder 未持久化 typed identity；第三轮 RED：含糊 identity 会继续触发 venue submit。三轮均先观察目标失败再实施。
  - Task 2 GREEN：实现无网络 `MarketIdentity`/legacy reader，统一 builder 写 typed columns，并在公共 `submit_execution_leg` 的网络/余额/盘口之前增加 identity gate；没有在 post-wire builder 伪造失败状态。
  - 门禁保留既有价格/滑点优先级和 `missing_token_id` 对外 reason；per-leg token 优先，自动忽略其他市场的 stale live context；被拒订单 effective notional 为 0。
  - Task 2 全回归：109 passed in 164.60s；另有 4 条 asyncpg test-connection cancellation warning，测试未失败，已作为测试 harness 清理边界记录，未冒充零警告。
  - 当前 21 笔真实订单只读 identity 预览：16 `legacy_inferred`，5 `ambiguous/missing_selected_token`；未写回数据库。
  - Task 3 首轮 RED：`services.online_resolution` 不存在；第二轮安全 RED 精确暴露 5 个边界缺口，包括 provider 返回其他 condition/market 时会串单。
  - Task 3 GREEN：实现无 DB 依赖的 `ResolutionObservation` 规范化器和可注入 `GammaResolutionProvider`；单请求默认 8 秒、批量并发默认 4、响应 identity 与 lookup 必须严格一致。
  - finality 只接受 `closed=true + acceptingOrders=false + resolved + 唯一 winner`；当前 Gamma 以 `umaResolutionStatus=resolved` 作为 resolved 信号，无显式 winner 时仅允许二元精确 `1/0` one-hot 向量。
  - Task 3 验证：19 项专项测试通过；与既有 Polymarket client/verifier 联合回归共 52 passed in 0.65s；`py_compile` 和 `git diff --check` 通过。
  - 对 2026-08-10 Gamma 最新 3 个已关闭市场做只读烟测：3/3 解析为 final，winner token 均在对齐 token 列表中，未进行任何 DB 写入。
  - Task 4 RED：store API 不存在；防篡改二次 RED 证明 typed market ID 替换后仍可借用旧 evidence hash，随后补齐逐字段证据契约。
  - Task 4 GREEN：确定性 observation/current IDs，PostgreSQL observation 原子去重，current fact 初始 upsert + `FOR UPDATE`，pending→final 单向推进、陈旧观测不回退、final 同 winner 冻结、不同 winner 进入 conflicted 并保留原 winner/evidence。
  - `persist_observation(..., commit=False)` 默认由 caller 提交；`commit=True` 失败会 rollback 并原样向上抛出，没有静默吞异常。
  - Task 4 联合验证：25 passed in 47.32s；真实 PostgreSQL 触发器注入 current fact 失败后，observation/current fact 均为 0 行。
  - Task 5 RED：新账户/开仓/结算/不可变/反向分录/legacy checkpoint 行为先失败；追加审计又证明旧 `execute_opportunity`、旧 `resolve_trade` 会直接改 v2 余额，v2 删除只会在数据库 FK 处报错。
  - Task 5 GREEN：新增 `simulation_ledger.py`，统一 6 位 USDC Decimal、账户行锁、连续 sequence、全局幂等键、entry debit、settlement credit、reversal、legacy checkpoint 和完整性重建。
  - 新建模拟账户默认为 v2/complete；opening marker 为 0，余额公式固定为 `initial_capital + SUM(journal.amount_usdc)`；历史账户仍为 v1/legacy，显式 checkpoint 后只能显示 checkpointed。
  - orchestrator Shadow 开/平仓在 caller-owned transaction 中写 journal 和兼容聚合投影；v2 旧手工 execute/resolve 双写入口被持锁阻断，legacy 账户兼容路径保留。
  - v2 append-only 账户不允许物理删除；服务在触碰子表前明确拒绝，API 返回稳定 HTTP 409，而不是把数据库 FK 异常暴露为 500。
  - Task 5 验证：专项安全/兼容 4 passed；schema 6 passed；模拟账本与既有模拟回归 22 passed；Alembic head round-trip/base replay 2 passed；`py_compile` 与 `git diff --check` 通过。
  - 当前 Python 环境未安装 Ruff（`No module named ruff`）；已记录工具边界，未虚报静态检查通过，Task 11 将使用项目可用环境补跑。
  - Task 6 RED/GREEN 完成：同订单 20 路并发、同账户不同订单并发、三处故障注入、unsafe/manual-review、100 次重放均覆盖；结算/并发/现金账本/模拟适配联合 26 passed in 178.05s。
  - settlement coordinator 只消费已持久化事实且不拥有 commit/rollback/HTTP；同一事务写 settlement intent、cash journal、兼容投影和订单/交易/持仓终态，终态异证据重放明确拒绝。
  - Task 7 完成：v2 Shadow 订单旁路 legacy 终局和 price inference；final/conflicted 仅登记 settlement_pending，v1 legacy 行为保持。生命周期完整回归 79 passed in 552.64s，Shadow backfill 2 passed。
  - Task 8 RED 首次按预期因 `services.online_settlement_cycle` 不存在而 collection failure；随后实现默认 observe 的四态运行门禁、100 市场/4 并发/8 秒单请求边界、DB session 外联网、逐市场错误隔离和非阻塞冷平面单实例调度。
  - Observe 只写 observation/current fact 并报告 would-settle；Shadow 只通过 coordinator 入账；Live 无凭据返回 not_configured，有凭据但 adapter 未完成时仍 fail-closed；off 不开 DB、不联网。
  - Task 8 专项扩展 12 passed in 42.94s；与 Compose/worker-host 联合 23 passed in 38.13s。`docker compose config` 确认 backend 及全部 worker plane 均展开 `HOMERUN_SETTLEMENT_RUNTIME_MODE: observe`。
  - Task 8 静态边界：目标文件 `py_compile`、`git diff --check` 通过，新建 settlement 模块/测试无超过 120 字符行。联合 worker-host 测试结束后出现既有 Redis health probe pending-task 日志噪声，未导致测试失败，保留为后续 harness 清理项。
  - Task 9 完成：Live 严格拆分 `market_final`、`claimable`、`redeem_submitted`、`redeem_confirmed`、`cash_verified`；Gamma final 不写实际盈亏，CTF/redeemer 沿用原链路并保留 fail-closed。
  - Task 10 完成：历史修复只读 preview、canonical digest、默认关闭 apply、显式 legacy checkpoint 和 ledger integrity API 已实现；无自动历史写回。
  - Task 11 完成：183 个唯一核心测试全部通过；由于 worker-cycle 分组重复一次，实际执行 195 次。20 路同单并发、同账户双订单、100 次重放、事务故障注入、迁移 round-trip、worker plane、Live 与 repair 均覆盖。
  - Ruff 对新结算核心服务、两条迁移和对应测试通过；旧 `routes_simulation.py` 仍有 7 个本任务前既有问题，未做无关重构。position lifecycle 组保留既有 UTC deprecation/asyncpg cancellation 警告，worker-host 结束有 Redis health probe pending-task 日志噪声，均无断言失败。
  - 运行态复核：数据库仍在 `202608100001`，目标 head 为 `202608100002`；全部容器仍运行旧固定镜像 `local-20260810-trader-token-bridge-v6`。因此新结算链尚未加载到运行系统，未产生新 schema 或经济写入。
  - 下一步进入 Task 12：先写运维/回滚手册并固定源码构建，再以 `observe` 模式迁移和重启；历史 apply 与 Live 均保持关闭。
  - 已创建 `docs/operations/online-settlement-runbook.md`，固化安全开关、migration/observe/shadow 顺序、账本与 worker 核验、历史 preview、Live 状态、阻断条件和非破坏性优先回滚流程。
  - 构建固定镜像 `local-settlement-20260811`，image ID `sha256:41590fe1c7b9de56864feda5073f2415ec005e735c117fe1f2c2478c4bb7f181`；镜像内关键源码 hash 与主机一致，`.env` 未进入镜像，Alembic head 为 `202608100002`。
  - 备份 SHA-256 与 `pg_restore --list` 复核通过后完成 additive migration `202608100001 -> 202608100002`；旧账户保持 v1/legacy，四张新表 0 行，新增索引均 valid/ready。
  - 只重启 backend 与 worker-reconciliation；两者使用新固定镜像、`observe`、repair apply=false。API `/health/live` 返回 alive。
  - observe 两个完整周期均零经济写入：`applied=0`，账户余额/PnL/订单盈亏不变，settlement/journal 0 行；周期无 network/persistence/apply errors。
  - 运行验证发现规格遗漏：23 个历史订单 typed 列全空，observe `blocked=22/candidates=0`；账户 repair preview 的 17 单全为 ambiguous。已新增 Task 10A，先补 RED 测试再修复，尚未重启新版 trading plane。
  - Task 10A 完成：observe 可在内存中推断历史 identity 并保存官方事实，普通 Shadow 仍不自动固化/结算；repair preview 把 proposed identity 纳入摘要，显式 apply 才能在同一事务固化 identity、checkpoint 和 settlement。可推断历史订单仍计入 `blocked`，避免健康状态误报。
  - Task 10A 验证：行为缺口先得到 4 个预期 RED，监控语义再得到 2 个预期 RED；修复后 worker/repair 23 passed、身份/事实/账本/并发/Live 相关 73 passed、最终 worker-cycle 14 passed。Ruff、format、`py_compile`、`git diff --check` 和禁止项扫描通过。
  - 构建固定镜像 `local-settlement-20260811-r2`，image ID `sha256:9d7fe7f02aa2b40ffb954a74e9d6aee557867a5c24654f0a1cd69b36b2b00aaa`；关键源码 hash 与主机一致、镜像无 `.env`、唯一 Alembic head 为 `202608100002`。
  - R2 observe 两个完整周期：首轮 `candidates=14/observed=14/inserted=14/final=5/blocked=22/applied=0`；次轮 `14/9/2/5/22/0`。网络、持久化、apply 错误均为 0；既有账户余额/PnL/订单状态和 23 个历史订单 typed 列均未改变。
  - 历史只读 preview：17 单中 safe=5、ambiguous=1、blocked=11，digest `fc87957021f43e2edafb600c1fb37057c503de01f3f55f6e65e111dacd332042`，候选净派彩 `26.147042`、候选 PnL `3.054009`；未调用 apply。
  - 隔离 v2 账户 `settlement-validation-r2-20260811` 完成一胜一负：`+147/-100`，余额 `1047`、净 PnL `47`，2 settlements、5 journal，integrity difference `0.000000` 且 sequence 连续。100 轮/200 次 coordinator 重放后所有经济计数不变。
  - provider 超时故障注入得到 `network_errors=9/health=degraded/applied=0`；冷 worker 重启后真实周期恢复 `network_errors=0`，隔离账户经济状态不变。
  - 曾因 `docker compose start worker-trading` 拉起陈旧 migrate 容器而出现“不认识 202608100002”；数据库 head 始终正确。已把 `.env` 固定为 R2、用 R2 重建 migrate 并退出 0，运行手册规定以后使用 `up --no-deps`。
  - 最终 backend、migrate 与全部 Python worker 统一使用 R2；15 个 worker snapshot 均 running/enabled、无 last_error。冷结算已切回 `observe`、repair apply=false、Live 凭据未配置；API alive、前端 HTTP 200。
  - 用户随后明确批准“历史 5 笔安全订单修复 + 持久 Shadow”两项操作。先停止 trading/reconciliation，生成定向 custom-format 备份 `data/runtime/settlement-preapply-20260811-044938/homerun-settlement-preapply.dump`；93 行 TOC 可读，大小 97,423 bytes，SHA-256 `D94973F075EA544982024C4E2958C85E0D0305483F227C23B21B67B08B057EB8`。
  - 冻结后的全量 preview 仍为 safe=5/ambiguous=1/blocked=11；精确 5 单定向摘要为 `6dccfec0bceaf216204bfd083021bea2cc50bf4d8db5057527e63a2a1f622e74`，候选净派彩 `26.147042`、候选 PnL `3.054009`。仅在一次后端进程中短时启用 apply，并在 `finally` 中重建为 false。
  - 5 单 repair 原子提交：`applied_count=5`、全部 `projected`，订单/交易/持仓均为胜方终态。账户余额 `945.550990`、总 PnL `3.054009`、5 胜、11 未平；账本 v2/checkpointed，6 条流水连续，difference `0.000000`。修复后定向 preview 的 5 单均 `already_applied=true`，未出现额外结算。
  - `.env` 已持久设置 `HOMERUN_SETTLEMENT_RUNTIME_MODE=shadow` 和 `HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED=false`；backend、trading、reconciliation 均以 R2 重建并核对容器环境。两个完整 Shadow 周期无 network/persistence/apply error，第二周期 `duration_ms=782`、`applied=0`，账户经济状态未重复变化。Live 凭据仍未配置。
- Files created/modified:
  - `backend/models/database.py`
  - `backend/alembic/versions/202608100002_online_settlement_ledger.py`
  - `backend/tests/test_online_settlement_schema.py`
  - `backend/services/market_identity.py`
  - `backend/services/trader_orchestrator_state.py`
  - `backend/services/trader_orchestrator/order_manager.py`
  - `backend/tests/test_market_identity.py`
  - `backend/tests/test_trader_order_manager_live.py`
  - `backend/services/online_resolution.py`
  - `backend/tests/test_online_resolution.py`
  - `backend/tests/test_online_resolution_store.py`

## Test Results
| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| 官方终局状态 | Gamma IDs 3412941/3412928 | closed/resolved, NO | 两者均 closed/resolved, NO | PASS |
| 热路径 Shadow 只读结算 | 两笔卡住订单 + `hot_path_no_rest` | 复现未结算 | held=2, would_close=0, blocked=8 | PASS（复现） |
| 非热路径 Shadow 只读结算 | 同两笔完整订单 | 识别终局并计算候选收益 | would_close=2, resolved_win=2, +$1.274244 | PASS（诊断） |
| watchdog 投影身份 | 只含数字 market ID 的 SimpleNamespace | 暴露身份缺失 | 仅返回 id/market_id，无 closed/result | PASS（复现） |
| 当前代码写回检查 | dry_run 与数据库复查 | 不改变订单/余额 | 订单仍 open，账户 PnL 仍 0 | PASS |
| 现有聚焦单元测试 | simulation ledger + verifier prefetch + shadow backfill | 建立当前测试基线 | 15 passed in 77.46s | PASS（覆盖不足已记录） |
| 实施前聚焦回归 | 同上，备份完成后的当前工作树 | 基线无漂移 | 15 passed in 61.08s | PASS |
| PostgreSQL 完整备份 | 8.5 GB 运行库，custom/gzip archive | 可读取归档目录且本地有 hash | 1,040,620,886 bytes；`pg_restore --list` PASS；SHA-256 已记录 | PASS |
| Task 1 schema RED | 新 schema 测试 | 当前缺能力时定向失败 | 6 failed，均为目标表/列/migration 不存在 | PASS（RED） |
| Task 1 schema GREEN | 新 ORM + migration | schema 契约通过 | 6 passed in 0.56s | PASS |
| Task 1 Alembic | head round-trip + base replay | 对称且空库可重放 | 2 passed in 39.76s | PASS |
| Task 2 identity RED | 模块/链路/pre-wire 三阶段 | 分别暴露缺模块、未落盘、会继续送 venue | 三阶段均出现精确目标失败 | PASS（RED） |
| Task 2 focused GREEN | identity + order manager | 纯函数/门禁契约 | 40 passed | PASS |
| Task 2 full regression | identity/order manager/data access/session/fast submit | 不改变既有下单与持久化语义 | 109 passed in 164.60s；4 warnings 已披露 | PASS |
| Task 2 current-row preview | 21 个现有 TraderOrder，只读 | 安全与含糊项分离 | 16 legacy_inferred；5 ambiguous；0 写回 | PASS |
| Task 3 finality RED | 缺模块 + 二次安全边界 | 精确失败后再实施 | 模块不存在；二次 5 failed | PASS（RED） |
| Task 3 focused GREEN | 规范化/finality/provider 限流与 identity 校验 | 全部通过 | 19 passed | PASS |
| Task 3 Polymarket regression | online resolution + client + verifier | 旧解析链不退化 | 52 passed in 0.65s | PASS |
| Task 3 live read-only smoke | Gamma 最新 3 个 closed market | 严格 final 且 winner token 对齐 | 3/3 final；0 DB 写入 | PASS |
| Task 4 store RED | 缺 store API + typed/evidence 脱节 | 精确失败 | ImportError；防篡改测试落到 AttributeError | PASS（RED） |
| Task 4 store GREEN | 去重/单向/freeze/conflict/stale/commit/atomic failure | 全部通过 | 25 passed in 47.32s | PASS |
| Task 5 cash journal RED | v2 journal + legacy 双写/删除门禁 | 精确暴露缺失能力 | journal import failure；旧入口 3 failed/legacy 1 passed | PASS（RED） |
| Task 5 cash journal GREEN | v2 open/close/replay/reversal/checkpoint/gates | 账本可重建且旧账户兼容 | 22 passed in 103.83s | PASS |
| Task 5 schema/migration | cash constraints + Alembic head/base replay | ORM/migration 对称 | schema 6 passed；Alembic 2 passed | PASS |
| Task 9 Live RED | 状态分层、redeemer、mirror 原子性 | 缺少严格桥接时定向失败 | 首轮 6 failed / 1 passed，均命中目标能力 | PASS（RED） |
| Task 9 Live GREEN | verifier + redeemer + CTF + schema 联合回归 | 终局不冒充到账、重放幂等、失败原子回滚 | 52 passed，1 skipped in 98.73s；`py_compile` PASS | PASS |
| Task 10 repair RED | preview/digest/apply/完整性 API | 服务不存在时精确失败 | collection error：`services.settlement_repair` 不存在 | PASS（RED） |
| Task 10 repair GREEN | repair API + coordinator 联合回归 | 只读预览、摘要防漂移、checkpoint、重放幂等 | 13 passed in 117.75s；`py_compile` PASS | PASS |
| Task 11 schema/identity/resolution/store | 规范化、事实持久化和 schema | 新链基础契约不退化 | 42 passed in 47.08s | PASS |
| Task 11 ledger/coordinator/concurrency | 20 路并发、双订单、100 replay、故障注入 | exactly-once 且无部分写 | 28 passed in 181.61s | PASS |
| Task 11 Live + repair | Live 状态机与历史 repair 门禁 | 状态/权限/摘要边界保持 | 19 passed in 128.31s | PASS |
| Task 11 lifecycle | 旧/新生命周期兼容 | 既有行为不退化 | 79 passed，85 warnings in 515.92s | PASS（警告已披露） |
| Task 11 migration/worker/routes | Alembic、plane、compose、API 安全 | 迁移可回放、调度边界正确 | 15 passed in 29.40s | PASS |
| Task 11 final targeted rerun | CTF/Live/verifier/worker host | 格式化后最终聚焦验证 | 56 passed，1 skipped in 99.50s | PASS（测试结束有 Redis task 日志噪声） |
| Task 10A worker + repair | 历史 identity 只读发现/显式升级 | observe 零 identity 写、Shadow 不自动升级、apply 原子回滚 | 23 passed in 132.21s | PASS |
| Task 10A related regression | identity/finality/store/schema/ledger/coordinator/concurrency/Live/equity/routes | 不降低生产校验且共享 fixture 合法 | 73 passed in 252.25s | PASS |
| R2 observe | 两个真实冷周期 | 只增长 facts/observations，经济数据不变 | 14 facts/16 observations；applied=0；既有账户不变 | PASS |
| R2 historical preview | 既有账户 17 单 | 只读生成 proposed identity 和经济证书 | safe=5/ambiguous=1/blocked=11；未 apply | PASS |
| R2 controlled Shadow | 隔离 v2 一胜一负 + 100 轮重放 | exactly-once、账本可重建 | PnL +47；difference 0；200 replay 零增写 | PASS |
| R2 timeout recovery | 9 个 provider 超时后重启 | 显式 degraded、零经济写、恢复无错误 | errors 9→0；settlement/journal 不变 | PASS |

## Error Log
| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-08-10 | UTCDateTime 与字符串比较 TypeError | 1 | 改用 timezone-aware datetime |
| 2026-08-10 | Gamma 请求因默认 urllib UA 返回 403 | 1 | 明确 User-Agent 后成功 |
| 2026-08-10 | httpx client 生命周期提前结束 | 1 | 调整任务执行到 client 上下文内 |
| 2026-08-10 | 并行架构检索输出截断且一个子命令非零 | 1 | 改为按模块分段读取，避免重复大范围检索 |
| 2026-08-10 | 首次更新规划文件补丁包含错误路径和空 hunk | 1 | 拆成有效 hunk 并使用正确项目路径 |
| 2026-08-10 | backend `.venv` 缺少 pytest，生产镜像不包含 tests | 1 | 使用系统 Python 3.12 + 已安装 pytest 运行本地源码测试 |
| 2026-08-10 | 首次读取 Superpowers 技能使用了不存在的旧别名路径 | 1 | 按技能根目录映射改用插件实际目录并完整读取 |
| 2026-08-10 | worktree/catchup 组合探测因无 superproject/目录返回非零 | 1 | 分开执行并确认普通工作树、无未同步上下文 |
| 2026-08-10 | 规划状态更新补丁上下文顺序不匹配 | 1 | 先读取精确段落，再按实际文件顺序更新 |
| 2026-08-10 | 迁移与测试辅助检索的 PowerShell 组合命令存在未闭合字符串 | 1 | 改为逐文件、逐命令读取，不再复用错误命令 |
| 2026-08-10 | 可选 `rg` 无匹配返回 exit 1，导致组合检索整体非零 | 1 | 将可选检索拆开并显式允许无匹配 |
| 2026-08-10 | 广范围 conftest/迁移输出超过上下文上限 | 1 | 后续只读取精确文件和相关行段 |
| 2026-08-10 | 8.5 GB 数据库完整 `pg_dump` 超过首次 60 秒工具时限 | 1 | 容器内进程未被终止，持续监测至完成；只在完成后复制和校验 |
| 2026-08-10 | `pg_restore --list` 输出截断触发 broken pipe 非零码 | 1 | 完整读取归档目录到空设备，验证成功；不是备份损坏 |
| 2026-08-11 | 项目配置了 Ruff，但系统 Python 未安装模块 | 1 | 记录为 Task 11 工具边界；本阶段使用 `py_compile`、`git diff --check` 和测试验证，不虚报 lint |
| 2026-08-11 | 单次完整 pytest 超过 904 秒且未返回最终汇总 | 1 | 按互斥文件集拆分执行并核对唯一测试数；183 个唯一核心测试全部通过 |
| 2026-08-11 | 恢复会话时使用不存在的旧 Superpowers 技能路径 | 1 | 改用实际插件目录；该失败发生在项目外且无写入 |
| 2026-08-11 | 查询 WorkerSnapshot 表名时 PowerShell 字符串未闭合 | 1 | 改用单引号正则并成功读取表名；无项目写入 |
| 2026-08-11 | 业务基线 SQL 使用不存在的 trade/order 字段 | 1 | 通过 information_schema 读取真实列后重查；失败查询均只读 |
| 2026-08-11 | 公共 settlement 测试夹具用伪 condition/token 却标记 complete | 1 | 改为确定性合法链上 ID，不放宽生产校验；相关回归全绿 |
| 2026-08-11 | 受控账本查询误用 `sequence` | 1 | 按 ORM/information_schema 改用 `ledger_sequence`；失败 SELECT 无写入 |
| 2026-08-11 | `docker compose start` 拉起陈旧 migrate 镜像 | 1 | 数据库未变；固定 `.env` R2、重建 migrate 退出 0，并固化 `up --no-deps` 规则 |
| 2026-08-11 | Phase 8 一次多文件 `git diff` 输出超过工具上限且可选子命令返回 1 | 1 | 已保留 worker/order manager 关键 diff；后续拆分按配置模块查询，未触碰源码或运行数据 |
| 2026-08-11 | Phase 8 天气检索误用 Windows 路径通配符，随后动态 `git blame` 行号参数解析失败 | 1 | 改用显式目录和分段读取；关键 prewarm 代码已成功取得，失败仅为只读检索 |
| 2026-08-11 | 直接打印完整 `weather_snapshot.opportunities_json` 超过输出上限 | 1 | 改为只查询时间、数组长度、活动字段和 stats 摘要，不再输出完整机会 JSON；只读查询未写入 |
| 2026-08-11 | 路由检索包含不受 PowerShell 支持的 `backend/routes*` 路径通配符 | 1 | 有效目录结果已返回；后续仅用真实 `backend/api` 等显式路径，未触碰代码 |
| 2026-08-11 | Shadow 写账链检索的双引号正则被 PowerShell 拆成路径参数 | 1 | 改用单引号正则并拆分关键词；失败仅为只读检索，未修改项目 |
| 2026-08-11 | 固定镜像构建的外层工具等待窗口仅 1 秒，返回 124 | 1 | 复查目标镜像而非重复假设失败；镜像实际已完成，并用主机/镜像双侧 SHA-256 验证源码一致 |
| 2026-08-11 | 镜像与 compose 组合检查输出被截断 | 1 | 拆为精简单项检查，取得可审计的镜像 ID、创建时间与文件哈希 |
| 2026-08-11 | 规划日志补丁对本文件使用了不精确句尾上下文 | 1 | 整个补丁校验失败且零修改；拆分文件后按实际行成功追加 |
| 2026-08-11 | 生产镜像内没有 `tests/`，首次镜像内 pytest 返回 no tests | 1 | 不计为通过；将主机测试目录只读挂载，执行进程和导入均来自固定镜像，随后 136 项通过 |
| 2026-08-11 | 运行证据补丁误把已完成的聚焦回归写成待办上下文 | 1 | 整个补丁校验失败且零修改；按当前真实复选状态拆分追加 |
| 2026-08-11 | PostgreSQL 测试设施检索包含不存在的根级可选路径，`rg` 返回 1 | 1 | 保留已返回的有效结果，后续只检索真实模型和测试目录 |
| 2026-08-11 | 模型类正则使用 PowerShell 双引号，管道字符被解释为命令 | 1 | 改用单引号正则后成功；失败命令只读 |
| 2026-08-11 | 静态配置检索包含不存在的根级 `pyproject.toml`，`rg` 返回 1 | 1 | 改用真实 `backend/pyproject.toml`；新测试范围行长与 B023 检查通过 |
| 2026-08-11 | PowerShell 管道给 `git add -p` 的首个回答带 BOM，hunk 选择错位 | 1 | 完整撤销 worker 暂存且保持工作树不变；随后用显式 index patch 精确暂存并验证历史 backfill 关键词为 0 |
| 2026-08-11 | PostgreSQL 测试初版依赖未提交的现金账本 ORM 类 | 1 | 改成通过 `to_regclass` 检测可选表，存在时才执行只读 count；本次 commit 可独立检出 |
| 2026-08-11 | 精确暂存树全文件测试在 Windows 有 1 个既有 cleanup flush 断言失败 | 1 | 原始 HEAD 同环境复现相同失败；不放宽无关测试。Linux 固定镜像为 137 passed，本次相关暂存树为 37 passed |
| 2026-08-11 | pytest 未安装 repeat 插件，`--count=3` 无法识别 | 1 | 未将其计为测试；改用显式三次运行并完成原始 HEAD 对照 |
| 2026-08-11 | 提交后更新计划状态的首个补丁上下文遗漏空格 | 1 | 整个补丁校验失败且零修改；按精确原文重新应用 |

## 2026-08-11 Phase 7 只读排障

- 用户报告：共识钱包跟随没有余额变化/订单；模拟账户从一个变成两个；要求检查天气、体育、加密、新闻完整交易链。
- 本阶段遵循系统化排障：先检查配置、数据库事实、worker snapshot 与真实 dispatch path；在根因和可重复证据明确前不修改交易代码，不启用 Live，不执行经济写入。
- 已确认钱包采集、rollup 身份字段和 orchestrator 调度均在运行；零订单主要发生于策略/执行门禁，另有 4 个 selected-without-order 样本进入 execution session 深查。
- 4 个 selected-without-order 均与同轮 `cycle_timeout after 10.0s` 一一对应；源码路径确认 selected 先 commit、submit 后执行且 Shadow 可在 10 秒外层被取消，取消后缺少决策终态收口。该项已从“待定”升级为代码/状态机缺陷；尚未修改生产代码。
- 已完成新闻与天气的采集→intent→strategy opportunity→signal→decision 运行核对：news 当前是质量阈值未达，weather 当前是 WS 报价覆盖/宽 spread/Shadow fill 门禁，无账户写账。
- 已核对体育 scanner 的逐轮诊断与策略加载：采集/分类正常、当前无满足赔率移动的信号；同时确认 autonomous trader 未配置，交易链尚未接通。
- Phase 7 最终运行核对完成：唯一 tracked wallet 采集正常但未贡献最新共识信号；当前无 direct copy-trade trader。共识最新 6 个信号均被 channel/spread 门禁拒绝，历史 4 个 selected 孤儿与 10 秒 timeout 一一对应。
- 两账户来源和选择完成复核：主账户仍是唯一 Shadow 交易账户，第二账户仅为隔离结算证明，不参与调度；未执行删除或任何经济写入。
- 天气、体育、加密、新闻均已分别核对 worker、策略、signal、decision/order 边界：上游采集均活跃，但四者都尚未形成可声明“端到端已跑通”的自动订单证据；天气是报价覆盖/价差，体育是无信号且缺 trader，加密是当前阈值未达及订阅策略不对齐，新闻是质量阈值未达。
- 本轮只执行只读 SQL、HTTP 状态和源码链路检查；没有修改生产代码、账户余额、策略配置或运行开关。

## 2026-08-11 Phase 8 变更归因与修复设计

- 用户批准按既定顺序修复，并明确要求先查是否为此前二开侵入造成，禁止叠加症状补丁。
- 已启动系统化排障与 TDD 流程；在根因和设计批准前暂停生产代码修改。
- 首轮 Git 归因确认：10 秒 runtime-trigger timeout 与取消 wrapper 来自上游历史提交，当前 ahead commits/未提交 timeout 区域没有改动；但本地 submit/账本/结算相关改造仍需继续核对是否改变时序。
- 已逐文件对比下单链：本地 `order_manager.py` 新增提交前 typed identity 校验，worker 本地差异是 Shadow ledger backfill；暂未修改代码，正在核对四个 timeout 的阶段事件和固定运行镜像哈希，以判断“上游潜在缺陷 + 本地触发条件”是否同时成立。
- 数据库逐单复核确认四个孤儿 selected 均没有 execution session/order；同窗正常拒单的提交只耗 9–308ms，排除“所有提交普遍超过 10 秒”。已将根因继续收敛到 Shadow 执行投影在取消前不持久化、且外层 timeout 不携带 decision/stage 的状态机边界；下一步审计具体 await 点及二开是否新增可阻塞依赖。
- 已审计 Shadow submit await 链：同步 typed identity 二开不是阻塞点；潜在 10 秒等待位于上游既有 Redis/WS→HTTP fallback，且 book resolver 在 preflight 与正式 submit 可重复调用。下一步核实 token cache 覆盖、HTTP client timeout 与当时运行镜像。
- 已确认 HTTP fallback 内层允许 30 秒、外层 runtime cycle 仅 10 秒，构成确定预算倒置；当前 host/container 五个关键文件哈希一致，接下来对比历史镜像确认问题发生时实际代码。
- 已完成历史镜像哈希对比：问题发生时的 token-bridge 镜像仍使用原版 order manager/session engine/WS feed，尚无 market identity 模块；因此排除当前结算身份校验直接导致这四次 timeout。开始转入配置归因（账户、体育、加密、钱包筛选）。
- 配置归因完成第一轮：体育 trader 缺失、digital-sigma 无消费者以及第二证明账户均来自此前 Codex 配置/验证过程，不是原项目代码 bug；主 Shadow 账户选择未漂移。钱包当前是多钱包共识策略，不是直接单钱包 copy trader。
- 天气链确认已有项目原生 token prewarm，不是简单缺 subscribe；正在核对为何 prewarm 后 strict WS 仍无报价，暂未修改天气/行情代码。
- 已确认 prewarm 自身存在 8 并发、HTTP 30 秒 fallback 与 0.5–1 秒 WS 等待组合；trading plane 不受订阅数量预算，但会清理 600 秒无更新 token。继续从真实 blocked token/Redis quote 证据判断是无盘口、身份错误还是预算问题。
- 已核对现有回归测试：Live cancel 与 Shadow 正常 commit 各有覆盖，但 Shadow 外层取消后的 durable intent/终态没有测试；先按该缺口设计 RED 测试，不直接修改 timeout 数字。
- 已抽样最新天气周期：部分市场正常取得盘口后被价差门禁拒绝，至少一单进入 Shadow 撮合但未排到成交，只有部分 token 报 strict WS unknown；天气不是整条链失效，下一步只追踪 unknown token 的缓存/官方盘口事实。
- 当前库天气最近 signal/decision 分别为 2026-08-10 20:10/20:29 UTC，历史天气订单为 0；当前 trading worker 在该轮之后重启，需用精简 snapshot 与下一轮事件区分“尚未重新触发”与“重启后订阅仍失败”。
- 精简 snapshot 确认天气按 4 小时正常调度，下一轮约北京时间 08:29。官方 CLOB 抽样表明 unknown 同时包含真实单边盘口和当前可双边交易盘口；下一步用可重复 prewarm 测试定位等待预算/WS snapshot 边界，不放宽安全门禁。
- R2 启动后未再出现 timeout 孤儿，但钱包信号没有跨过前置门禁，故不能用“零复现”宣称修好；当前窗口唯一订单是手工模拟单，自动交易仍为 0。
- 已核对项目默认：75bps 是原生通用 trader 风控值，wallet baseline 只是沿用；原生同时有 confluence/copy_trade 两个独立策略。后续不通过改代码放宽门禁，也不把直接跟单偷换进共识策略。
- 用户批准推荐方案 A 并要求按推荐开发；已创建 `fix/shadow-timeout-root-repair` 专用分支，现有脏工作树原样保留。开始编写正式设计/实施计划，尚未修改生产代码。
- 已定位实施边界到现有 `ExecutionSessionEngine` 的 pre-submit projection/cancellation finalizer；worker 只负责 deadline 与触发审计，避免在 worker 复制 session/order 事务逻辑。
- 已完整读取 session build 与 pre-submit commit 段：Shadow 可在现有 ORM 行构建后、外部 gate 前持久化 session/legs，不引入订单或余额写入；计划将用现有 staged flush 顺序。
- 主流程复核发现取消窗口从 preflight 就已开始，且原版为避免 5–13 秒无效 placeholder commit 把持久化后移。正在比较“预持久化 intent”与“取消时 shield 收口 + 重启扫尾”的性能/一致性边界，尚未写实现。
- worker 事务边界已确认：selected 先提交，Shadow submit 使用独立 session；外层 timeout 只掌握 trader 级信息。下一步读取项目已有 stage 跟踪与 session reconciliation，选定不引入竞态的审计和重启恢复方式。
- 已找到原生 `cancel_session(..., skip_provider_io=True)` 终态收口，可由 Shadow submit wrapper 在任意取消点通过新 session shield 调用；下一步必须先审计 Shadow 经济写入时点，防止“撮合已扣款但 session 被标失败”。
- Shadow 经济时点审计完成：leg submit 仅返回内存估算，账户/订单尚未写入，故取消终态不会吞掉已扣款成交。下一步把 cycle 剩余预算沿 worker→session→order-book await 传递，并先写 RED 测试。
- 预算设施确认可复用：worker 已有 monotonic remaining-budget helper，外层 cancel grace 5 秒。设计将使 Shadow 内部 deadline 提前失败并完成 session/decision 正常收口，外层强制 cancel 仅作兜底；Live timeout 语义保持不变。
- 已确认不使用新 worktree：当前固定运行基线依赖已批准但未提交的改动，新 worktree 会得到不同基线。改用专用分支、目标文件 diff 和精确 hunk 隔离。
- 正式修复边界已收敛：只改 Shadow session intent、取消/内部 deadline 收口及 worker 预算传递；Live、策略阈值、账户、订单经济公式和 `order_manager.py` 均保持不变。
- 正式规格与逐项计划已写入 `docs/superpowers/specs/2026-08-11-shadow-timeout-root-repair-design.md` 和 `docs/superpowers/plans/2026-08-11-shadow-timeout-root-repair.md`；自审移除了泛化测试占位符，准备执行 Task 0 基线与 Task 1 RED。
- Phase 9 Task 0 基线通过：现有 Live cancel、Shadow final commit、worker outer timeout 为 `3 passed in 2.16s`；运行镜像仍为固定 `local-settlement-20260811-r2`（sha256 `9d7fe7f0...b00aaa`）。
- Phase 9 Task 1 RED 精确失败：进入阻塞 Shadow submit 时 session=0、legs=0、commit=0、orders=0；证明测试复现的是 durable intent 缺口，而不是夹具或当前环境漂移。
- Phase 9 Task 2 GREEN：Shadow 在 venue await 前提交同一 session/legs intent；取消时 shield 收口为 failed、filled=0、orders=0，并写 decision/signal/stage/elapsed 审计。目标测试通过，连同既有 Live cancel 与正常 Shadow commit 为 `3 passed in 1.05s`。
- Phase 9 Task 3 RED 为 4 个目标失败：engine/submit wrapper 均缺 timeout 参数，worker Live/Shadow 均未透传预算。GREEN 后 4 项为 `4 passed in 1.27s`；Shadow 使用共享绝对 deadline，Live 参数固定为 None。
- Phase 9 Task 5 故障注入为 `4 passed in 1.33s`：provider 自有 TimeoutError 不被误标 cycle deadline；多 wave 第一波内存 fill 在第二波 deadline 后归零且零订单；intent commit 失败零 venue I/O；预算充足正常 Shadow 不重复 session/event。
- Phase 9 核心回归：`test_execution_session_engine.py` 为 `33 passed in 1.57s`；`test_trader_orchestrator_worker.py` 为 `103 passed in 4.78s`。worker 的 36 条 warning 均来自既有 naive datetime/SQLAlchemy mock resource，用例无失败。
- Phase 9 相关 Shadow 账本/结算回归：4 个文件合计 `29 passed in 202.60s`；最新聚焦复跑 `10 passed in 1.65s`，`py_compile` 与目标 `git diff --check` 均通过。
- Ruff 仅作差异审计：全文件存在 157 项历史 lint 债务；新代码曾命中 1 项 B023，已修正并复跑确认新行无 B023/E501。未借机重构历史 lint。
- 固定镜像 `local-shadow-timeout-20260811-r1` 已存在，镜像内 `session_engine.py` 与 `trader_orchestrator_worker.py` 的 SHA-256 分别为 `b57b9d2f...48c6`、`68c40b32...6e38`，与主机目标源码完全一致；尚未切换运行容器。
- `.env` 已切至 `local-shadow-timeout-20260811-r1`；`docker compose run --rm migrate` 退出 0。仅 backend 与 8 个 Python worker 被 force-recreate，均运行镜像 ID `sha256:914841f...779a`；PostgreSQL、Redis、frontend 未重建。
- API `/health` 返回 `{"status":"ok"}`。启动日志无导入/迁移/traceback；只出现“未配置 Polymarket 实盘凭证”拒绝 Live 初始化，符合当前明确不配置实盘账户的边界。
- 在固定修复镜像内只读挂载测试目录后，执行 session engine + worker 两个完整文件：`136 passed in 4.49s`；33 条警告为既有 naive datetime 和只读 pytest cache。
- 同一强制慢 Shadow submit 测试在事故前 R2 镜像稳定失败：submit 时 `session_count=0`、`leg_count=0`、`commit_calls=0`；在修复镜像为 `1 passed in 1.74s`，证明差异来自镜像代码而非生产数据库或测试数据。
- 独立 PostgreSQL 重放进一步确认：R2 为 `pre_session_statuses=[]`、`1 failed in 23.18s`；修复镜像相同测试 `1 passed in 11.30s`，并验证取消后 session/leg failed、fill=0、订单/模拟账户/现金账本均为 0。临时数据库清理后数量为 0。
- 部署后观察窗口累计 470 次 runtime iteration、942 个 session、99 个活跃秒；新增 selected-without-session=0、重复事件组=0、cycle timeout=0、Traceback=0、新订单=0。主账户仍为 `945.550990`、PnL `3.054009`，隔离证明账户仍为 `1047.000000`，均与部署前基线一致。
- 提交自包含检查：从暂存区生成临时 detached tree，不携带其他未提交代码；本次相关 37 项全部通过。全文件 137 项在 Windows 为 136 passed + 1 个原始 HEAD 可复现的无关 cleanup-flush 断言，固定 Linux 镜像为 137 passed。两个临时 worktree 均验证 clean 后已通过 `git worktree remove` 清理。
- 本任务已在 `fix/shadow-timeout-root-repair` 创建最终本地 commit `f1f48c55`；未 push、未 merge，其他未提交工作树改动保持原样。
- 最终 commit 独立检出后，`py_compile` 与 37 个本次相关测试通过（`37 passed in 15.63s`）。运行态复查为：2489 个部署后 session、selected-without-session=0、重复事件组=0、新订单=0、cycle timeout=0、Traceback=0；API 健康，9 个后端/worker 容器镜像 ID 全部匹配。
- Phase 10 启动：用户报告手动选择钱包共识信号后无订单/余额变化。首轮只读 DB 核对确认 traders signal 与 decision 正常产生，但最新结果均被 edge/channel、strict WS 或 75bps spread 门禁拒绝；尚需确认点击控件是否承诺“立即下单”还是只做池/跟踪配置。
- Phase 10 前端入口首轮已收敛：`AddWalletToBotDialog`/`traderBotActions` 看起来是机器人范围配置，`TraderSignalViews` 才可能包含信号动作。广泛检索输出被截断，另一次检索因不存在的 `frontend/src/locales` 返回非零；均未触碰运行态。一次计划日志补丁因标题上下文不精确整体失败、零修改，已按实际内容重试。
- 已确认本次不是“添加跟踪”语义：`TraderSignalViews` 内的 `BuyButton` 会调用 `traderManualBuy`，目标路由为 `/traders/{id}/manual-buy`。前端从共识信号构造请求时 token_id 为空，下一步逐行核对后端身份解析、返回状态和事务提交，并与用户点击时的事件/DB 记录对时。
- 已完成点击对时：09:19 `$50` 与 09:22 `$25` 均写入 TraderOrder/事件，但 token 为空、状态只到 `shadow/submitted`，没有统一 Shadow 成交和账户扣款。Git blame 初步归因到上游历史，不是本轮 timeout 修复；继续核对 origin ancestor、TraderPosition 和 UI 查询过滤。
- 根因闭环：上游路由将 outcome 名称写成非规范 direction，Shadow backfill 每 30 秒稳定报 `Unsupported direction`，因此模拟账户/持仓/现金账本不变；API 订单存在但因无 account link 被当前账户筛选隐藏。origin ancestor 已确认，问题不来自本轮 timeout 修复。下一步核对信号中已有的 outcome/token identity，设计单一原子 Shadow 手动成交链，禁止继续先写“假 submitted 成功”。

## 2026-08-11 Phase 11 钱包共识手动执行原子修复

- 用户已批准按推荐方案开发；正式规格与内联 TDD 计划写入 `docs/superpowers/specs/2026-08-11-wallet-consensus-manual-execution-design.md` 和 `docs/superpowers/plans/2026-08-11-wallet-consensus-manual-execution.md`，本地设计 commit 为 `890961a1`，未 push/merge。
- Task 0 基线冻结：分支 `fix/shadow-timeout-root-repair`、HEAD `890961a1`；目标生产文件当前无未提交 diff。运行 backend 为 `local-shadow-timeout-20260811-r1`（`sha256:914841f...779a`），frontend 为 `local-20260810-trader-token-bridge-v6`（`sha256:823f327c...26a6`）。
- 主 Shadow 账户 `4473f507-5e6e-4269-b04a-de4a967284a0` 当前余额 `945.550990`、PnL `3.054009`、16 笔交易；隔离证明账户仍为 `1047.000000`、2 笔。两条用户手动请求仍是 `submitted`、空 token、非规范方向、无 `simulation_ledger`，本轮未修改。
- 规划/技能只读阶段出现三类零写入工具错误：一次错误技能占位路径、两次可选 `rg` 无匹配导致组合调用非零、一次 `git diff --no-index` 因存在新文件差异返回 1；均已改用真实技能路径和独立命令，不影响项目或运行数据。
- Task 1-4 已完成：服务端从当前 snapshot 重建 token/condition/outcome，规范为 `buy_yes/buy_no`；stale/conflict fail-closed；固定 request id 幂等复用同一 decision/session/order；所有运行门禁在 reservation 前拒绝。
- Task 5-6 已完成：手动 Shadow fill 在 ExecutionSessionEngine 最终 projection 内原子写入 TraderOrder、SimulationTrade、SimulationPosition、CashLedger、SimulationAccount 与 TraderPosition；余额不足和 no-fill 均零经济写入。
- Task 7 已完成：前端保留 opportunity/token/canonical direction，modal retry 复用稳定 request id，Shadow 禁止 resting limit，成功后刷新账户/订单/持仓真实 cache key；生产构建通过。
- 新增账户一致性 RED/GREEN：账本若落入与 decision 记录不同的 Shadow 账户，接口返回 409，不再误报成功；账户选择优先级继续与原 worker 一致（全局设置优先、trader metadata 后备）。
- Task 8 验证：专项 `7 passed`；扩大交易链回归 `83 passed, 1 warning in 169.16s`。warning 为既有 asyncpg cancel 清理协程告警，断言全部通过；`py_compile`、前端 contract check、`npm run build` 均通过。
- 当前真实配置只读复核：主账户仍为 `945.550990`/16 笔/v2 checkpointed，证明账户 `1047.000000`/2 笔/v2 complete；全局 Shadow 账户仍指向主账户。运行镜像尚未切换，数据库没有因本任务验证产生新交易。
- Task 9 固定镜像完成：backend `local-wallet-manual-20260811-r1` / `sha256:52ade7e...59aa`，frontend 同标签 / `sha256:f0046a5d...e603`；镜像内目标源码和静态 bundle 与主机 SHA-256 一致。PostgreSQL/Redis 未重建，Live 四项凭证 presence 均为 false。
- 隔离 HTTP 成功证明：60.914 bps 钱包机会在 75 bps 风控内完成，decision `a9957f...f5bf`、session `0b97b4...8153`、order `e114d2...7d94`、trade `ff4674...3188`、position `4e0ef5...c93a`、cash `e03759...5f21` 全链互相引用；账户 `1047.000000 -> 1042.308725`，交易数 `2 -> 3`。
- 同请求幂等重放返回完全相同 ID；账户余额、trade/position/cash/order 数量均零变化。1621.622 bps 宽价差信号返回 409，decision skipped、session 1、order 0，经济表零变化。
- 临时账户切换期间已暂停调度并停止 trading/reconciliation 容器；证明后全局账户恢复主账户，两个 worker 已重启。主账户仍 `945.550990`/16 笔；部署与证明后日志无 Traceback、manual failure 或 `shadow_ledger_backfill_failed`。
- 完整运行证据：`data/runtime/wallet-manual-execution-20260811-r1/README.md`。下一步只剩用户在主 Shadow 账户界面确认一条新信号，以及精确提交本任务代码；两条历史错误 submitted 保持不动。

## 5-Question Reboot Check
| Question | Answer |
|----------|--------|
| Where am I? | Phase 11 Task 9 已完成：固定镜像已部署，隔离账户 HTTP 原子成交、幂等重放和拒单零写入均已证明 |
| Where am I going? | 等用户在主 Shadow 账户界面确认一次新钱包信号，再做最终精确 diff/commit；保持 Live 未配置 |
| What's the goal? | 建立实盘级、幂等、可恢复、可审计的统一结算链 |
| What have I learned? | 根因是身份/终局责任断裂；可推断历史 identity 能安全抓事实，但必须继续 blocked 直到显式修复 |
| What have I done? | 完成权威身份、幂等 reservation、统一执行会话、Shadow 原子账本、前端请求契约、83 项回归、固定镜像部署与真实 HTTP 证明 |
