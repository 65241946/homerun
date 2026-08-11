# HOMERUN Wallet Consensus Manual Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本项目按用户要求单线程内联执行，不使用子代理或多模型。

**Goal:** 让钱包共识信号的人工确认订单只在统一执行引擎产生真实成交且订单/模拟账本/余额原子提交后返回成功，并具备服务端身份校验和请求幂等。

**Architecture:** API 从当前机会快照重建权威 execution plan，用固定 manual decision 作为请求幂等 reservation，再复用 `ExecutionSessionEngine` 的盘口、风险、Shadow/Live 执行状态机。Shadow 通过引擎可选的 `shadow_account_id` 在最终 projection 内调用现有 SimulationService，使订单、模拟交易、持仓、现金账本和余额一次提交；前端只负责传递声明、稳定 request id 和刷新正确缓存。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2 async、PostgreSQL 16、pytest/pytest-asyncio、React 19、TypeScript、TanStack Query、Docker Compose。

## Global Constraints

- 当前分支为 `fix/shadow-timeout-root-repair`，HEAD 基线为 `f1f48c55`。
- 工作树存在大量已批准但未提交改动；只编辑本计划列出的文件，逐文件核对 diff，不使用 `git add -A`。
- 不修改钱包策略、风控阈值、账户选择、结算公式、数据库 schema 或 migration。
- 不改写两条历史错误 `submitted` 记录。
- Live 凭证保持未配置；任何运行验证均为 Shadow。
- 每项生产代码必须先有能在当前源码上正确失败的 RED 测试。

---

### Task 0：冻结现状和错误样本

**Files:**
- Verify: `backend/api/routes_traders.py`
- Verify: `backend/services/trader_orchestrator/session_engine.py`
- Modify: `progress.md`

- [ ] 记录分支、HEAD、目标文件当前 diff、运行镜像和两个错误订单的只读基线。

```powershell
git status --short --branch
git diff -- backend/api/routes_traders.py backend/services/trader_orchestrator/session_engine.py frontend/src/components/TraderSignalViews.tsx frontend/src/components/BuyButton.tsx frontend/src/services/apiTraders.ts
docker inspect homerun-backend --format '{{.Config.Image}} {{.Image}}'
docker inspect homerun-frontend --format '{{.Config.Image}} {{.Image}}'
```

- [ ] 用只读 SQL 固化主账户余额、两个旧订单、对应事件和 `simulation_ledger` 缺失；不执行 UPDATE/DELETE。

**Gate:** 目标生产文件无本任务外未识别 hunk；历史数据数量和余额基线可复查。

---

### Task 1：RED——服务端必须拒绝丢失或冲突的交易身份

**Files:**
- Create: `backend/tests/test_routes_trader_manual_buy.py`
- Modify later: `backend/api/routes_traders.py`

**Interfaces:**
- Consumes: `TraderManualBuyRequest`、trader `ScannerSnapshot` opportunity。
- Produces: `_resolve_manual_buy_opportunity(...)` 和 `_build_manual_runtime_signal(...)` 的可观察路由行为。

- [ ] 新增 PostgreSQL 路由集成测试：当前钱包机会含 `token_id` 和 `outcome=NO`，客户端空 token 时服务端仍必须从同一 opportunity 重建 `buy_no`；当前实现应因不存在该权威解析行为而失败。
- [ ] 新增身份冲突测试：客户端 token/outcome 与快照不一致时，执行引擎 mock 必须零调用、订单/decision/账本为零。
- [ ] 新增 stale opportunity 测试：当前快照不存在 `opportunity_id` 时返回 404，不能使用客户端字段继续下单。

```powershell
py -3.12 -m pytest backend/tests/test_routes_trader_manual_buy.py -k "identity or stale" -q
```

**Expected RED:** 旧路由不读取快照，空 token 被直接写入订单且方向为展示名称。

---

### Task 2：GREEN——权威 opportunity 与规范 execution plan

**Files:**
- Modify: `backend/api/routes_traders.py`
- Verify: `backend/tests/test_routes_trader_manual_buy.py`

- [ ] 给请求增加 `client_request_id`、`order_type` 和可选规范 `direction`；金额、价格和 ID 保留严格长度/范围校验。
- [ ] 从 `scanner_shared_state.read_traders_snapshot` 和 `read_scanner_snapshot` 查找当前 opportunity；只接受当前快照中的 positions。
- [ ] 按 token/market/outcome 校验客户端声明；从 `positions_to_take`、market `condition_id/tokens` 构造规范 `buy_yes/buy_no`、typed token 和 execution plan。
- [ ] 拒绝真实 SELL/平仓和 Shadow resting limit；不猜 token，不把 outcome 展示名称拼进 direction。
- [ ] 运行 Task 1 测试直到 GREEN。

```powershell
py -3.12 -m pytest backend/tests/test_routes_trader_manual_buy.py -k "identity or stale" -q
```

---

### Task 3：RED——手动请求必须幂等并走统一执行会话

**Files:**
- Modify: `backend/tests/test_routes_trader_manual_buy.py`
- Modify later: `backend/api/routes_traders.py`

- [ ] 新增测试：相同 `client_request_id + trader_id` 连续调用两次，ExecutionSessionEngine 只执行一次，返回同一 decision/session/order。
- [ ] 新增测试：相同幂等键但金额或机会不同返回 409，执行引擎不再次调用。
- [ ] 新增测试：trader disabled/paused/block_new_orders、orchestrator paused/kill switch 时在 durable reservation 和执行前拒绝。
- [ ] 新增测试：engine `skipped/failed/orders_written=0` 时 API 不返回 success，账户不变。

```powershell
py -3.12 -m pytest backend/tests/test_routes_trader_manual_buy.py -k "idempotent or gate or no_fill" -q
```

**Expected RED:** 当前路由每次生成随机订单、绕过 ExecutionSessionEngine，并在 Shadow 无成交时返回 success。

---

### Task 4：GREEN——manual decision reservation 与严格返回语义

**Files:**
- Modify: `backend/api/routes_traders.py`
- Verify: `backend/tests/test_routes_trader_manual_buy.py`

- [ ] 用 `client_request_id + trader_id` 派生固定 decision id，在任何执行前提交 `selected` manual decision，并保存请求指纹。
- [ ] 重复请求先读取 decision；指纹一致返回已有 session/order，冲突返回 409，禁止重复 provider/Shadow submit。
- [ ] 调用 `ExecutionSessionEngine.execute_signal`，传所选 trader 的 mode/risk limits、manual runtime signal 和 Shadow account id。
- [ ] 把 engine status 映射到 decision；Shadow 仅在 `orders_written > 0` 且订单带 `simulation_ledger` 时返回 success，Live 仅在既有 provider 订单进入可识别成交/工作状态时返回 success，否则返回明确 HTTP error。
- [ ] 删除旧路由直接构造 `TraderOrder` 和直接调用 `live_execution_service.place_order` 的旁路。

```powershell
py -3.12 -m pytest backend/tests/test_routes_trader_manual_buy.py -q
```

---

### Task 5：RED——Shadow 最终投影必须原子写入模拟账本

**Files:**
- Modify: `backend/tests/test_execution_session_engine.py`
- Modify later: `backend/services/trader_orchestrator/session_engine.py`

**Interfaces:**
- Consumes: `ExecutionSessionEngine.execute_signal(..., shadow_account_id: str | None = None)`。
- Produces: 每个实际 Shadow fill 的 `TraderOrder.payload_json.simulation_ledger`。

- [ ] PostgreSQL RED：传入有效 v2 account 和一个 executed Shadow leg，返回前账户从 1000 精确扣除实际成交 notional+fee，订单/SimulationTrade/SimulationPosition/CashLedger/TraderPosition 均为一条且相互引用。
- [ ] RED：余额不足时 engine 不得返回成功；最终订单和所有经济表为零，账户仍为 1000。
- [ ] RED：Shadow leg skipped/no fill 时余额和经济表为零。
- [ ] 回归：不传 `shadow_account_id` 的既有自动 Shadow 测试行为保持不变，避免本次无意迁移全部自动链。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py -k "inline_shadow_ledger" -q
```

**Expected RED:** `execute_signal` 尚不接受 `shadow_account_id`，订单 projection 与模拟账本仍分离。

---

### Task 6：GREEN——订单、余额、持仓和现金账本一次 commit

**Files:**
- Modify: `backend/services/trader_orchestrator/session_engine.py`
- Verify: `backend/tests/test_execution_session_engine.py`

- [ ] 给 `execute_signal` 增加向后兼容的 `shadow_account_id: str | None = None`。
- [ ] 最终 projection 在 TraderOrder flush 后，只对 `mode=shadow`、正成交 notional、无 ledger marker 的订单调用 `simulation_service.record_orchestrator_shadow_fill(..., session=self.db, commit=False)`。
- [ ] 从 order payload 读取 fee/slippage/token，传规范 direction；把 ledger result 重新赋给 `order.payload_json`。
- [ ] ledger 完成后复用现有 `sync_trader_position_inventory(commit=False)`，最后由原 projection 单次 commit。
- [ ] 任何 ledger/commit 异常向上抛出且不返回成功；保留 durable session 供现有 reconciliation 收口。

```powershell
py -3.12 -m pytest backend/tests/test_execution_session_engine.py -k "inline_shadow_ledger" -q
```

---

### Task 7：前端身份透传、请求幂等和即时刷新

**Files:**
- Create: `frontend/scripts/check-manual-buy-contract.ts`
- Modify: `frontend/src/components/TraderSignalViews.tsx`
- Modify: `frontend/src/components/BuyButton.tsx`
- Modify: `frontend/src/services/apiTraders.ts`

- [ ] 先新增一个无框架的 TypeScript contract check，验证钱包 NO position 保留 token 和 `buy_no`、成功缓存 key 包含 `simulation-accounts/positions-panel/accounts-panel`；在生产实现前运行并观察失败。
- [ ] `UnifiedTraderSignal` 保留当前 opportunity 的 execution positions，而不是只保留展示方向；请求仍由服务端权威校验。
- [ ] modal 生命周期生成并复用一个 `client_request_id`；响应丢失后的 retry 使用同一 key，关闭后再生成新 key。
- [ ] Shadow 禁用误导性的 resting limit；Market 文案对应现有 marketable-limit/即时撮合语义。
- [ ] success 仅识别 `data.status === 'success'`，并失效真实查询 key；显示后端具体拒绝原因。

```powershell
npm --prefix frontend run build
```

**Gate:** TypeScript 编译和 Vite 构建通过，不改变其他机会卡片布局。

---

### Task 8：聚焦回归、静态检查和差异审计

**Files:**
- Verify only

- [ ] 运行手动路由、session engine、现金账本、Shadow backfill、position inventory 和 Live cancel 回归。

```powershell
py -3.12 -m pytest backend/tests/test_routes_trader_manual_buy.py backend/tests/test_execution_session_engine.py backend/tests/test_simulation_cash_ledger.py backend/tests/test_trader_orchestrator_shadow_backfill.py backend/tests/test_trader_order_manager_live.py -q
```

- [ ] 编译和差异检查。

```powershell
py -3.12 -m py_compile backend/api/routes_traders.py backend/services/trader_orchestrator/session_engine.py backend/tests/test_routes_trader_manual_buy.py backend/tests/test_execution_session_engine.py
git diff --check
git diff -- backend/api/routes_traders.py backend/services/trader_orchestrator/session_engine.py backend/tests/test_routes_trader_manual_buy.py backend/tests/test_execution_session_engine.py frontend/src/components/TraderSignalViews.tsx frontend/src/components/BuyButton.tsx frontend/src/services/apiTraders.ts
```

---

### Task 9：固定镜像与隔离账户 API 证明

**Files:**
- Modify: `progress.md`
- Runtime evidence only: `data/runtime/wallet-manual-execution-*`

- [ ] 从当前固定源码构建新的 backend/frontend 唯一标签，核对镜像内目标文件 SHA-256；不使用 `latest`。
- [ ] 保持 Live credentials absent，先只切换 backend/frontend；数据库和 Redis 不重建。
- [ ] 创建或复用隔离验证账户，通过真实 HTTP API 执行一个当前、可交易的钱包机会。
- [ ] 下单前后核对 API、TraderDecision、ExecutionSession、TraderOrder、SimulationTrade、SimulationPosition、SimulationCashLedgerEntry、SimulationAccount 和 TraderPosition。
- [ ] 使用同一 `client_request_id` 重放，证明订单数、trade 数、ledger 数和余额均不再变化。
- [ ] 注入余额不足或无成交样本，证明 API 明确失败且经济表不变化。

**Gate:** 隔离账户形成一次成功原子成交和一次幂等重放证据后，才允许用户用主 Shadow 账户手动验证。

---

### Task 10：主账户受控验证与提交边界

**Files:**
- Modify: `progress.md`
- Verify: all target files

- [ ] 用户选择一条新的钱包共识信号并确认一次 Shadow 下单；记录下单前后余额、订单 ID、session ID、trade ID、cash ledger ID 和持仓。
- [ ] 不处理两条历史错误 submitted；另列只读修复候选。
- [ ] 仅精确暂存本任务 hunk，运行 cached diff check 和最终聚焦测试；不 push、不 merge，除非用户另行批准。

```powershell
git diff --cached --check
git diff --cached --stat
git diff --cached
```

**交付证据:** 根因、RED/GREEN、固定镜像、隔离 API 账本对账、主账户手动验证、历史错误记录边界和回滚标签分别列出。
