# 修复 spec:fix-01 编排器影子结算(CONFIRMED-1)

> 基座:`65241946/homerun` @ `389d246`
> 缺陷:编排器 shadow 交易在 SimulationAccount 上**只扣不还**(开仓 backfill 无门控扣款;平仓入账被 `enable_simulation_ledger` 默认 False 挡住,全库无人置 True)。
> 本 spec 是给 codex 的实现方案。**codex 落地前需操作员确认方案 A/B 与目标 checkout。**

---

## 根因(已逐行核实)

| 环节 | 位置 | 现状 |
|---|---|---|
| 开仓扣款 | `workers/trader_orchestrator_worker.py:5173` → `_backfill_simulation_ledger_for_active_shadow_orders`(:2788) | **无门控**,只要 `shadow_account_id` 配了就扣 `SimulationAccount.current_capital`(`simulation.py:456`) |
| 平仓入账 | `workers/trader_orchestrator_worker.py:5192` → `reconcile_shadow_positions`(未传 `enable_simulation_ledger=True`)→ `position_lifecycle.py:5062` 门禁 | `enable_simulation_ledger` 默认 False(:4601),全库无 True 调用方 → `close_orchestrator_shadow_fill`(`simulation.py:479`)**永不执行** |
| 权威 PnL | `TraderOrder.actual_profit`(结算时写于 `position_lifecycle.py:5136`) | ✅ 正常;`routes_traders.py:692` 直接读它显示 |

**净效果**:配了 `shadow_account_id` 的 trader,其编排器 shadow 单在 SimulationAccount 上只扣不还,`total_pnl` 永不更新,`current_capital` 单调下降直到 `insufficient shadow capital` 抛错卡死。

**读取侧事实**:
- `routes_simulation.py`(“模拟交易”UI)读 `SimulationAccount.total_pnl/current_capital`。
- `routes_traders.py:692`(“自动交易器”UI)读 `TraderOrder.actual_profit`。
- 二者是**两套独立账本**。编排器 shadow 的权威已是 `TraderOrder.actual_profit`。

---

## 方案对比(操作员决策点)

### ✅ 方案 B(推荐):单一权威源 —— 删除编排器→SimulationAccount 镜像

编排器 shadow PnL 只认 `TraderOrder.actual_profit`。移除半成品镜像:
- 删开仓 backfill 的 SimulationAccount 扣款(`_backfill_simulation_ledger_for_active_shadow_orders` 及其调用点 `worker:5173`)。
- 删平仓入账死代码(`position_lifecycle.py:5062-5075` 的 `close_orchestrator_shadow_fill` 分支)与 `enable_simulation_ledger` 参数(`:4601`)。
- 删 `close_orchestrator_shadow_fill`(`simulation.py:479`)与只服务它的 `record_orchestrator_shadow_fill`(`simulation.py:338`)—— **前提:确认这两个函数无其它调用方**(spec 要求 codex 先 grep 证明)。
- 保留 `SimulationAccount` 及 `routes_simulation` 经典手动/套利模拟路径(`execute_opportunity`/`PositionMonitor`/`resolve_trade`)不动。

**优点**:单一真相源,消除双账本分歧;符合 agents.md 原则2。
**代价**:“模拟交易”UI 不再显示编排器 shadow 活动(改在“自动交易器”UI 看)。

### 方案 A(备选):对称镜像 —— 让开仓平仓门控一致

保留镜像,修对称性:
- 平仓调用点(`worker:5192` 等 4 处)传 `enable_simulation_ledger=True`,与无门控的开仓对齐;
- 或给开仓 backfill 也加同一门控,由单一配置项统一开关开仓+平仓。

**优点**:改动小,保留镜像功能。
**代价**:双账本仍在,需保证 `SimulationAccount.total_pnl` 与 `SUM(TraderOrder.actual_profit)` 一致(引入对账负担)。

---

## Your task(按操作员选定的方案执行,默认 B)

1. **先证明可删性**:grep 全仓,证明 `close_orchestrator_shadow_fill` / `record_orchestrator_shadow_fill` / `enable_simulation_ledger` 的所有引用点,列进产出文件。若发现方案 B 会破坏其它调用方,停止并报告。
2. 按选定方案改代码(B:删除;A:对称门控)。
3. **加回归测试**(必须):
   - 一个编排器 shadow 单走"开仓→结算"全程,断言 `TraderOrder.actual_profit` 被正确写入(权威 PnL 正常)。
   - 方案 B:断言全程 `SimulationAccount.current_capital` **不被编排器路径改动**(镜像已移除)。
   - 方案 A:断言开仓扣款后,平仓**必定入账**,`current_capital` 回补 + `total_pnl` 更新;开仓+平仓对称。
   - 回归判据对齐 `docs/architecture-baseline.md` 的 **V7**。
4. 结论写入 `.piercode/fix-01-last-message.md`。

## Hard constraints

- 目标 checkout 与分支由操作员指定(clean 基座 checkout)。
- 允许修改:`workers/trader_orchestrator_worker.py`、`services/trader_orchestrator/position_lifecycle.py`、`services/simulation.py`、以及对应测试文件。**不改** `models/database.py` schema(本 fix 不涉及建表)、不改 `routes_simulation.py` 经典路径。
- **测试必须起 Postgres**(集成测试覆盖持仓结算路径);缺库不算跑过。
- 遵循 agents.md:删除不留 tombstone;不加兼容 shim;删死代码要跟到调用链尽头。
- 不 commit/push,除非操作员明确要求。

## Output format(产出 `.piercode/fix-01-last-message.md`)

### 结论
一句话:选了哪个方案,改了什么,V7 回归是否转绿。

### 引用点证明
`close_orchestrator_shadow_fill` / `record_orchestrator_shadow_fill` / `enable_simulation_ledger` 的全部引用(`文件:行号`),证明删除安全。

### 改动清单
每处 `文件:行号` + 改了什么 + 为什么。

### 测试证据
新增/修改的测试名 + 运行结果(passed 数);证明起了 Postgres。

### 未能验证
不确定的、没查证的。不许留空充数。
