# HOMERUN Shadow 执行超时根因修复设计

## 1. 目标

修复 runtime-trigger 周期中已经写入 `selected` 决策、但 Shadow 执行在外部行情等待中被 10 秒周期超时取消，最终没有 `execution_session`、没有订单且无法解释失败阶段的问题。

本次只修复执行状态机的持久化顺序、超时预算和取消审计。Live 下单行为、策略阈值、仓位算法、账户余额公式、结算账本、行情安全门禁和实盘权限均不改变。

## 2. 已确认事实

1. 历史 4 个 `traders_confluence` 样本均已持久化 `selected`，没有 execution session/order，并与同轮 `cycle_timeout after 10.0s` 一一对应。
2. 事故时运行镜像的 `order_manager.py`、`session_engine.py`、`ws_feeds.py` 与上游原版一致；当时尚无本地 typed identity 模块。因此不能把这 4 次超时归因于后续身份校验改造。
3. worker 外层周期默认预算是 10 秒；Polymarket HTTP fallback 内层允许等待 30 秒，形成确定的 timeout budget inversion。
4. worker 在执行前先提交 decision/cursor/claim；Shadow 的 session、legs、orders 和事件则直到全部执行波结束后才提交。取消发生在前者和后者之间时会留下 selected-without-session。
5. Shadow `submit_execution_leg` 在行情和 Redis await 后仅返回内存撮合结果。账户、`TraderOrder`、`ExecutionSessionOrder` 和余额投影尚未写入，因此取消收口不能把这些内存估算当作成交。
6. Live 已有 pre-submit placeholder、CLOB 幂等键和 cancellation finalizer；本次不得改变这套语义。

## 3. 非目标

- 不调大 10 秒周期来掩盖内层 30 秒等待。
- 不放宽 spread、流动性、WS freshness、余额或仓位门禁。
- 不修改 `backend/services/trader_orchestrator/order_manager.py`。
- 不新增数据库表、字段、唯一约束或 Alembic migration。
- 不清理、合并或切换模拟账户。
- 不配置体育、加密、新闻或钱包策略；这些在状态机验证通过后分开处理。
- 不启用 Live，不注入私钥，不发送真实订单。

## 4. 必须成立的不变量

### 4.1 Shadow durable intent

- local plan/bundle/self-crossing 校验通过后，第一次外部 await 之前，必须提交一个 `placing` 状态的 `ExecutionSession`、全部 `ExecutionSessionLeg` 和 `session_created` 事件。
- durable intent 不得创建 `TraderOrder`、`ExecutionSessionOrder`、模拟交易、持仓或现金分录，不得改变模拟余额。
- intent 提交失败时不得进入 venue preflight 或 submit。
- 正常 Shadow 完成仍由现有最终 projection 写订单、持仓和 signal 状态；intent 只是同一 session 的先行状态，不产生第二个 session。

### 4.2 预算所有权

- worker 继续拥有整个 trader cycle 的 deadline；在 selected decision 已提交后，用现有 monotonic `cycle_started_mono` 计算剩余预算。
- Shadow 提交预算等于外层剩余预算减 2 秒终态持久化预留。维护周期 `cycle_timeout_seconds <= 0` 时不强加新 deadline。
- 该相对预算通过 `submit_order` 显式传给 `ExecutionSessionEngine.execute_signal`；Live 永远传 `None`，保持现有至少 60 秒的外层语义。
- session engine 将相对预算转成单一 monotonic absolute deadline；venue preflight、初始 submit、reprice submit 和 rescue submit 都必须使用同一个 deadline，不能每一波重新获得完整预算。

### 4.3 超时与取消收口

- 内部 deadline 到期时，Shadow 以结构化 `failed` 结果返回，worker 走现有 decision `selected -> failed` 路径；外层强制 cancel 只做兜底。
- 外层 `CancelledError` 落在 Shadow venue preflight/submit 时，必须先 shield 并完成 session/legs 的失败收口，再继续传播取消。
- 失败收口必须清除尚未提交的 order projection，所有 legs 的 filled notional/shares 归零，`orders_written=0`；不得产生幽灵模拟成交。
- 正常最终 projection 在 durable intent 之后被取消时，持久化必须完成或明确失败；不能让 AsyncSession close 回滚已经向 worker 返回成功的 Shadow 订单。
- 进程在 intent 已提交后硬退出时，现有 `expires_at` 与 reconciliation/cancel session 链负责后续终态化；不另造第二套 sweeper。

### 4.4 审计

- `session_failed` 事件和 session payload 至少包含：`kind`、`stage`、`decision_id`、`signal_id`、`elapsed_ms`、`timeout_seconds`、`orders_written=0`。
- stage 只使用低基数值：`intent_commit`、`venue_preflight`、`venue_submit_wave`、`projection_persist`。
- 不记录 signal 原始 payload、API key、钱包私钥、授权头或完整 provider 响应。
- 每次 interruption 只写一个 session 终态事件，不增加轮询日志或高频前端推送。

### 4.5 Live 隔离

- Live 不预提交轻量 Shadow intent，不接受 `execution_timeout_seconds`，不改变 placeholder 创建时机。
- Live 取消仍由 `_finalize_cancelled_live_submit` 处理，既有 provider/idempotency/reconciliation 语义和测试必须保持通过。

## 5. 状态序列

### 5.1 正常 Shadow

`decision:selected commit` → `session/legs:placing commit` → `venue preflight` → `shadow match` → `orders/session/signal final commit` → `decision final commit`

### 5.2 内部 deadline

`decision:selected commit` → `session/legs:placing commit` → `venue await exceeds shared deadline` → `session/legs:failed commit, zero orders` → engine returns failed → `decision:failed commit`

### 5.3 外层取消兜底

`decision:selected commit` → `session/legs:placing commit` → `outer task.cancel()` → shielded `session/legs:failed commit, zero orders` → propagate `CancelledError` → worker emits trader-level `cycle_timeout`

### 5.4 进程硬退出

`decision:selected commit` → `session/legs:placing commit` → process loss → existing reconciliation finds expired active session → `cancel_session(skip_provider_io=True)` terminalizes it

## 6. 实现边界

| 文件 | 修改责任 |
|---|---|
| `backend/services/trader_orchestrator/session_engine.py` | durable Shadow intent、共享 deadline、interruption finalizer、审计 payload；Live 分支保持原样 |
| `backend/workers/trader_orchestrator_worker.py` | 计算 Shadow 剩余提交预算并经 `submit_order` 透传 |
| `backend/tests/test_execution_session_engine.py` | 取消孤儿、内部 deadline、零经济写、正常 Shadow、Live 回归 |
| `backend/tests/test_trader_orchestrator_worker.py` | budget 透传、Live 为 None、decision 正常收口契约 |

不修改 `order_manager.py`、模型、migration、策略文件、账户和结算服务。

## 7. 失败语义

- `deadline_exceeded`：可预期预算耗尽，session/decision 为 failed，允许下一轮按现有冷却和幂等规则重新评估。
- `cancelled`：外层兜底取消，session failed，trader-level cycle timeout 继续保留；不自动重试同一 submit。
- intent DB commit failure：异常向上抛出，venue I/O 不发生；由现有 worker/DB 告警处理。
- final projection DB failure：异常向上抛出，既有 rollback 行为保留；不得伪报完成。

## 8. 验证与部署门禁

1. 先运行现有 Live cancel、Shadow commit、worker timeout 测试建立基线。
2. 每个修复先写 RED 测试，确认失败原因正是缺少 durable intent/deadline，而非测试夹具错误。
3. 聚焦测试通过后运行 session engine、worker、Shadow ledger/settlement 相关回归。
4. `py_compile`、`git diff --check` 和目标文件 diff 必须通过；不得混入脏工作树其他 hunk。
5. 从当前固定源码构建新标签，不使用 `latest`；只更新 Shadow/worker 容器，Live 保持未配置。
6. 隔离账户重放一个可控慢 preflight：修复版必须留下 failed session/legs 和 failed decision、零订单、余额不变。
7. 用事故时原版镜像/源码在隔离数据库重放同一场景：应复现 selected-without-session，作为差异证据；不得连接真实资金。

## 9. 回滚

- 代码回滚只涉及上述两个生产文件，无 schema downgrade。
- 部署前记录当前 R2 镜像 ID；出现回归时用固定 R2 标签重建 Shadow worker/backend，不执行 `docker compose start` 拉起陈旧 migrate。
- 回滚不删除已产生的 failed session 审计行；它们是事实记录，不是经济成交。
