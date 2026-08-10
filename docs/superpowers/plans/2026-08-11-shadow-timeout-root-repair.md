# HOMERUN Shadow Timeout Root Repair Implementation Plan

> **Execution rule:** 单线程内联执行；每项生产修改前先观察对应 RED；不启动多模型或子代理。

**Goal:** 消除 runtime-trigger Shadow 执行中的 selected-without-session 孤儿状态，使外部行情等待受共享周期预算约束，并在 deadline/取消时留下零经济效果、可审计、可恢复的失败 session。

**Architecture:** worker 继续拥有 cycle deadline，并在提交前计算剩余 Shadow 预算；session engine 在外部 await 前提交轻量 session/legs intent，所有 venue 阶段共享一个 monotonic deadline。内部 deadline 返回 failed 结果供 worker 正常更新 decision；外层 cancel 只作为 shielded terminalization 兜底。Live 原有 placeholder 和取消链不变。

**Tech Stack:** Python 3.12、asyncio、SQLAlchemy 2 async、PostgreSQL 16、pytest/pytest-asyncio、Docker Compose。

---

## 统一实施约束

- 当前分支：`fix/shadow-timeout-root-repair`。
- 当前工作树很脏；只修改本计划列出的 4 个代码/测试文件和规划文档。每步前后都运行精确 `git diff -- <path>`。
- `backend/workers/trader_orchestrator_worker.py` 已有前序未提交 hunk，最终只精确暂存本任务新增 patch，不整文件提交。
- 不修改 `order_manager.py`、数据库模型、migration、策略、配置阈值、账户或余额。
- 测试统一从仓库根目录运行；先设置 `$env:PYTHONPATH=(Resolve-Path backend).Path`，各任务下方列出精确 pytest node。

---

### Task 0：冻结基线与现有契约

**Files:**
- Verify: `backend/services/trader_orchestrator/session_engine.py`
- Verify: `backend/workers/trader_orchestrator_worker.py`
- Modify: `progress.md`

- [x] 记录分支、HEAD、目标文件状态和当前固定运行镜像。

```powershell
git branch --show-current
git rev-parse HEAD
git status --short --branch
git diff -- backend/services/trader_orchestrator/session_engine.py backend/workers/trader_orchestrator_worker.py backend/tests/test_execution_session_engine.py backend/tests/test_trader_orchestrator_worker.py
docker compose ps
docker inspect homerun-backend --format '{{.Config.Image}} {{.Image}}'
```

- [x] 运行三个既有契约测试：Live cancel、Shadow final commit、worker outer timeout。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py::test_execute_signal_finalizes_pre_submit_placeholder_when_live_submit_is_cancelled backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback backend/tests/test_trader_orchestrator_worker.py::test_run_trader_once_with_timeout_cancels_live_signal_cycle_after_soft_timeout -q
```

**Gate:** 三项必须在生产修改前通过；否则先解释基线漂移。

---

### Task 1：RED——复现 Shadow cancel 孤儿状态

**Files:**
- Modify: `backend/tests/test_execution_session_engine.py`

- [x] 新增 `test_execute_signal_shadow_cancel_persists_failed_intent_without_orders`：
  - 使用 `_FailureProjectionDb`；
  - `submit_execution_wave` 阻塞在 `asyncio.Future()`；
  - 在 wave 开始时断言 session/leg 已 commit，TraderOrder/ExecutionSessionOrder 为空；
  - cancel `execute_signal` task；
  - 断言 task 传播 `CancelledError`，但 persisted session/legs 为 failed、filled=0、订单表为空；
  - 断言 `session_failed` 事件包含 decision/signal/stage/elapsed/kind。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_cancel_persists_failed_intent_without_orders -q
```

**Expected RED:** wave 开始时 `ExecutionSession` 尚未持久化，证明当前 selected-without-session 窗口。

---

### Task 2：GREEN——Shadow durable intent 与取消原子收口

**Files:**
- Modify: `backend/services/trader_orchestrator/session_engine.py`
- Verify: `backend/tests/test_execution_session_engine.py`

- [x] 在 `execute_signal` local plan/bundle/self-crossing 检查通过、第一次 venue await 前，Shadow 复用 `_commit_pre_submit_projection()` 提交 session、legs、`session_created`；此时两个 order 列表必须为空。
- [x] 增加函数内 `shadow_intent_persisted` 状态，确保 Live 不进入该分支。
- [x] 增加一个 cancellation-safe await helper：若父 task 被取消，等待正在进行的 intent/final projection 完成，再传播原取消；不遗留使用即将关闭 AsyncSession 的后台 task。
- [x] 增加 `_finalize_shadow_interruption(...)`：清空内存 order projections，所有 leg 归零并 failed，session failed，追加单个结构化事件，调用现有最终 projection 提交 signal/session/legs。
- [x] 将 venue preflight 与所有 submit wave 经统一 wrapper 执行；Shadow `CancelledError` 先 shield finalizer，再传播；Live 继续走 `_finalize_cancelled_live_submit`。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_cancel_persists_failed_intent_without_orders backend/tests/test_execution_session_engine.py::test_execute_signal_finalizes_pre_submit_placeholder_when_live_submit_is_cancelled backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_persists_with_commit_so_async_session_close_does_not_rollback -q
```

**Gate:** Shadow cancel 为 failed session/legs、零 orders；Live placeholder 测试结果不变。

---

### Task 3：RED——共享 deadline 必须早于外层 cancel

**Files:**
- Modify: `backend/tests/test_execution_session_engine.py`
- Modify: `backend/tests/test_trader_orchestrator_worker.py`

- [x] 新增 `test_execute_signal_shadow_deadline_returns_failed_zero_order_result`：传短 deadline，让 submit 阻塞，断言函数自行返回 failed 而不是依赖 task.cancel；session/legs/event 已提交且订单为空。
- [x] 扩充 `test_submit_order_executes_without_forcing_decision_persistence`，断言 `execution_timeout_seconds` 原样透传到 engine。
- [x] 将现有 claim-before-submit 测试参数化为 Live/Shadow：Live 断言 timeout 为 `None`；Shadow 用 `cycle_timeout_seconds=10`，断言提交预算为正且不超过 8 秒。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_deadline_returns_failed_zero_order_result backend/tests/test_trader_orchestrator_worker.py::test_submit_order_executes_without_forcing_decision_persistence backend/tests/test_trader_orchestrator_worker.py::test_run_trader_once_claims_signal_before_submit_and_passes_mode_budget -q
```

**Expected RED:** `execute_signal`/`submit_order` 尚不接受预算参数，worker 也不传递该参数。

---

### Task 4：GREEN——预算透传和单一 monotonic deadline

**Files:**
- Modify: `backend/workers/trader_orchestrator_worker.py`
- Modify: `backend/services/trader_orchestrator/session_engine.py`

- [x] 给 `submit_order` 和 `ExecutionSessionEngine.execute_signal` 增加向后兼容可选参数 `execution_timeout_seconds: float | None = None`。
- [x] `_run_trader_once_inner` 在 selected commit/reset 后、`submit_order` 前，对 Shadow 调用现有 `_remaining_cycle_budget_seconds(..., reserve_seconds=2.0)`；Live 传 `None`。
- [x] session engine 在函数入口将 Shadow 相对预算转换成一次 absolute monotonic deadline；不对 Live 应用。
- [x] venue preflight、initial/reprice/rescue submit 通过同一 `_await_external_stage` 计算 remaining；remaining 耗尽抛出带 stage 的私有异常。
- [x] 每个 deadline 捕获点调用同一个 interruption finalizer并返回 `SessionExecutionResult(status="failed", orders_written=0)`；worker 继续使用现有 failed decision 更新路径。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py::test_execute_signal_shadow_deadline_returns_failed_zero_order_result backend/tests/test_trader_orchestrator_worker.py::test_submit_order_executes_without_forcing_decision_persistence backend/tests/test_trader_orchestrator_worker.py::test_run_trader_once_claims_signal_before_submit_and_passes_mode_budget -q
```

**Gate:** deadline 测试耗时明显小于外层 10 秒；同一 session 只产生一个 failed interruption 事件、零订单。

---

### Task 5：事务/重启/回归故障注入

**Files:**
- Modify: `backend/tests/test_execution_session_engine.py`
- Modify: `backend/tests/test_trader_orchestrator_worker.py`

- [x] 增加 intent commit failure 测试：commit 抛 `DBAPIError` 时 submit mock 未调用，异常向上抛，零经济写。
- [x] 增加正常 Shadow deadline-enabled 回归：预算充足时仍 completed、一个订单、正常最终 projection；intent 不重复创建 session/event。
- [x] 增加多 wave 超时测试：第一波内存 executed、第二波超时后仍为零订单/零 fill，防止部分内存结果冒充成交。
- [x] 复用 `cancel_session(skip_provider_io=True)` 的现有测试或新增 placing Shadow intent 重启扫尾断言；不新增 sweeper。
- [x] 重跑全部 session engine 与 worker 聚焦文件。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py -q
py -3.12 -m pytest backend/tests/test_trader_orchestrator_worker.py -q
```

---

### Task 6：静态检查与相关回归

**Files:**
- Verify only

- [x] 编译目标文件并检查 whitespace/diff。

```powershell
py -3.12 -m py_compile backend/services/trader_orchestrator/session_engine.py backend/workers/trader_orchestrator_worker.py backend/tests/test_execution_session_engine.py backend/tests/test_trader_orchestrator_worker.py
git diff --check
git diff -- backend/services/trader_orchestrator/session_engine.py backend/workers/trader_orchestrator_worker.py backend/tests/test_execution_session_engine.py backend/tests/test_trader_orchestrator_worker.py
```

- [x] 运行 Shadow 账本、结算、orchestrator 相关回归，不重复全仓库无关测试。

```powershell
py -3.12 -m pytest backend/tests/test_trader_orchestrator_shadow_backfill.py backend/tests/test_simulation_cash_ledger.py backend/tests/test_settlement_coordinator.py backend/tests/test_settlement_worker_cycle.py -q
```

**Gate:** 无 Live 行为变化、无 schema 变化、无账户写账回归。

---

### Task 7：固定源码部署与隔离验证

**Files:**
- Modify: `progress.md`
- Create (运行证据，不提交): `data/runtime/shadow-timeout-repair-*`

- [x] 记录 R2 当前镜像 ID和运行库基线，只构建固定标签 `local-shadow-timeout-20260811-r1`，不使用浮动 `latest`。
- [x] 保持 Live 未配置；先在隔离临时 PostgreSQL 验证，再只重建 Shadow 运行所需 backend/worker，不重建数据库、Redis 或 frontend。
- [x] 注入可控慢 venue submit，核对数据库：engine session/legs failed、orders=0、账户余额和 journal 不变；worker decision failed 更新契约由聚焦测试覆盖。
- [x] 恢复正常行情，观察超过两个 runtime-trigger 周期，确认没有 selected-without-session 新样本、没有重复 session/event、没有 timeout 日志放大。

---

### Task 8：原版隔离重放与差异证明

**Files:**
- Modify: `progress.md`
- Create (运行证据，不提交): `data/runtime/shadow-timeout-baseline-replay-*`

- [x] 在独立临时数据库使用事故时固定 R2 镜像运行同一慢 venue submit；未连接真实账户、未复用主业务数据库。
- [x] 记录原版 selected-without-session 与修复版 failed-session/zero-order 的差异 SQL、时间线和镜像哈希。
- [x] 临时容器退出后核对目标临时数据库数量为 0；证据文件保留，当前数据卷未删除。

---

### Task 9：提交边界与交付

**Files:**
- Verify: all target files

- [x] 对 `trader_orchestrator_worker.py` 生成仅本任务 hunk 的 patch 并精确暂存；其他目标文件也逐项核对，禁止 `git add -A`。
- [x] 运行 cached diff check 和最终聚焦测试后提交；未自动 push 或合并。

```powershell
git diff --cached --check
git diff --cached --stat
git diff --cached
```

**交付证据:** 根因、代码 hunk、RED/GREEN 结果、运行镜像、隔离 SQL、账户不变证明、回滚命令与仍未解决的配置问题分开列出。
