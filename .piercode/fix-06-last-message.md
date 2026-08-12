# Fix-06 shadow runtime 修复结论

实施日期：2026-08-13
分支：`claude/batch-2-identity-finality-review-jvcsc5`

## 分项提交

- P-1：`7c8c8252 P-1: reuse shadow fill path for manual buys`
- P-2：`81324f06 P-2: decouple shadow from live wallet credentials`
- P-3：`e88e4502 P-3: preserve crypto producer origin on signals`

## P-1 手动 shadow 复用统一成交与 PnL 链

### 调研结论

- 自动 shadow 入口为 `backend/services/trader_orchestrator/order_manager.py:661` 的 `submit_execution_leg(mode="shadow")`：它读取真实订单簿/成交带，调用既有 fill simulator，并返回实际成交价、股数、名义金额或明确拒绝原因。
- 自动/手动 shadow 的结算权威源均为 `TraderOrder`。`backend/services/trader_orchestrator/position_lifecycle.py:4588` 的 `reconcile_shadow_positions` 结算并在 `:5085` 写 `TraderOrder.actual_profit`；`backend/services/trader_orchestrator_state.py:103-106` 已把 `executed` 视为有效活跃仓位状态。
- 旧手动路径只写 `status=submitted`，不会进入真实成交状态。不能恢复已被 fix-01 删除的 `SimulationAccount` 镜像写入；现有 canonical helper 可以直接复用，因此没有建立第二套成交或账本逻辑。
- 当前经典模拟账户余额 UI 仍读取 `SimulationAccount.current_capital`，例如 `frontend/src/App.tsx:1200`、`frontend/src/components/AccountsPanel.tsx:1063`；Trader 机器人订单、持仓与 realized PnL 则来自 `TraderOrder`。本工单不伪造经典账户扣款，手动机器人单与自动机器人单按同一 TraderOrder 口径展示/结算。

### 改动

- `backend/api/routes_traders.py:2248-2431`：shadow 手动买入组装最小 signal/leg，调用 `submit_execution_leg`；只有 helper 返回真实正成交量才持久化 `executed` 和 `executed_at`，并使用实际 `effective_price/filled_size/filled_notional_usd`。未成交统一写明确 `failed`，原始 simulation payload 和原因保留在 `shadow_execution`。
- live 分支继续调用原 `live_execution_service.place_order`，未接入 shadow helper。
- `backend/tests/test_routes_trader_manual_buy.py:54-179`：覆盖 shadow 成交、无订单簿明确失败、不写 `SimulationAccount`、live 路径不变。

## P-2 shadow 与实盘钱包凭证/新鲜度解耦

### 调研结论与方案选择

- 选择方案 **(a) 解耦**。`WalletStateCache` 表示真实 Polymarket 钱包的仓位、订单和余额；shadow 的持仓权威源是本地 `TraderOrder`，没有真实钱包，因此钱包新鲜度对 shadow 不适用。
- 代码现实中 standard 与 fast 两条消费闸已经只在 live 生效：`backend/workers/trader_orchestrator_worker.py:4767-4794` 以 control mode=`live` 为条件，`backend/workers/fast_trader_runtime.py:719-751` 以 trader mode=`live` 为条件。本批不放宽闸门，只补回归锁定。
- 真正的日志噪音来自三个重复初始化驱动点：host 指数退避循环、WalletStateCache 5 秒 bootstrap reseeder、wallet monitor 15 秒 refresh 都会再次进入 `ensure_initialized/initialize`，从而重复输出缺凭证 ERROR。
- spec 写 `backend/services/trader_reconciliation_worker.py`，仓库真实加载路径为 `backend/workers/trader_reconciliation_worker.py`（`host.py` 也加载 `workers.trader_reconciliation_worker`）。按实际生产文件实施，语义与 spec 一致。

### 改动

- `backend/workers/host.py:75,1077-1154`：host 成为 live initializer 单一所有者。第一次真实初始化仍由 service 输出 ERROR；之后保留 2→30 秒指数退避，但只安静探测 DB/env 凭证，凭证仍缺时每 300 秒最多一条 WARNING；检测到凭证齐全后重新进入正常初始化。
- `backend/workers/trader_reconciliation_worker.py:466-480`：wallet monitor 只在 live service 已 ready 时读取执行钱包，不再每 15 秒重复驱动 initializer。
- `backend/workers/trader_reconciliation_worker.py:715-891`：reseeder 不再调用 `ensure_initialized`。service 未 ready 且无 pinned wallet 时周期 WARNING 后返回；已有 pinned wallet 时仍可用公开 REST reseed。shadow 不读该缓存；live freshness gate 保持 fail-close。
- `backend/tests/test_shadow_wallet_freshness_gates.py:35-106`：standard/fast 两条 shadow 路径都证明不读取 live wallet cache；对应 live 路径遇 stale cache 均在 DB/决策前拒绝。
- `backend/tests/test_workers_host.py:102-132`、`backend/tests/test_trader_reconciliation_wallet_cache.py:21-52`：覆盖缺凭证时 initialize 只发生一次、reseeder/wallet monitor 不重复初始化。
- `backend/tests/test_live_execution_adapter.py:16-31`：既有安全回归随本批重跑，确认 live 初始化失败返回 `status=failed/submission=not_ready`，没有下单。

## P-3 crypto signal 生产者补齐 `strategy_origin`

### 生产路径定位

1. `backend/services/market_runtime.py:1843`：crypto dispatch 调用 `IntentRuntime.publish_opportunities(..., source="crypto")`。
2. `backend/services/intent_runtime.py:2135`：未覆盖 signal type 时生成 `crypto_opportunity`。
3. `backend/services/signal_bus.py:940-1030`：Opportunity 转 durable signal payload。各 crypto strategy 已把 `strategy_origin=crypto_worker` 放在 `strategy_context` 和/或 position `_crypto_context`，旧 builder 只保留嵌套数据，没有写到消费闸读取的 payload 顶层。

### 改动

- `backend/services/signal_bus.py:1010-1030`：只把生产者已经显式提供的 `strategy_origin` 提升到 durable `payload_json` 顶层；优先 `Opportunity.strategy_context`，后备 position `_crypto_context`。不根据 `source=crypto` 猜测或凭空补造来源。
- `backend/services/strategies/btc_eth_directional_edge.py:4583-4610` 的 `source_origin_predicate` 未修改，消费侧来源校验保持严格。
- `backend/tests/test_intent_runtime_ws_freshness.py:911-976`：覆盖已标记 crypto opportunity 进入顶层，以及无标记 opportunity 不被自动赋予来源。
- `backend/tests/test_strategy_migrations.py:384-406`：固定 `source=crypto + signal_type=crypto_opportunity + 无 strategy_origin` 仍被原闸门拒绝。

## 默认值与缺省行为变化

| 项目 | 旧行为/值 | 新行为/值 | 理由 |
|---|---|---|---|
| P-1 shadow 手动单状态 | 永久 `submitted` | 有真实模拟成交为 `executed`，否则 `failed` | 复用自动 shadow fill 路径，避免幽灵挂单 |
| P-2 缺凭证日志 | host 2→30 秒重试 ERROR，reseeder 约 5 秒、wallet monitor 约 15 秒也可重复触发 | 第一次 ERROR；之后安静退避探测，最多每 300 秒 WARNING | 保持运行时凭证自恢复，同时消除日志淹没 |
| P-3 producer origin | 仅嵌套 context，durable payload 顶层缺失 | 仅提升已有显式 origin 到顶层 | 对齐生产/消费合同，不放宽来源闸 |

没有新增或修改策略 `default_config`/schema key；没有费用计算改动。

## 测试与审计证据

- P-1：`python -m pytest backend/tests/test_routes_trader_manual_buy.py backend/tests/test_routes_trader_create_copy.py -q` → `25 passed`。
- P-2：`python -m pytest backend/tests/test_workers_host.py backend/tests/test_shadow_wallet_freshness_gates.py backend/tests/test_trader_reconciliation_wallet_cache.py backend/tests/test_live_execution_adapter.py backend/tests/test_wallet_state_cache.py -q` → `53 passed`。
- P-3：`python -m pytest backend/tests/test_intent_runtime_ws_freshness.py backend/tests/test_strategy_migrations.py -q` → `49 passed`。
- fix-06 最终合并回归：上述九个直接/相邻文件同跑 → `127 passed, 2 warnings in 5.62s`。
- 两条 warning 均为测试退出期既有 asyncpg `Connection._cancel was never awaited` ResourceWarning；用例全部通过，本批未修改数据库连接管理。
- `git diff --check` / 每次 `git diff --cached --check`：通过。
- 未调用 `polymarket_taker_fee_legacy_quartic`，未新增平坦费用常数，未触碰费用路径。

## 未验证项与部署提醒

- 未运行无筛选的全部 `backend/tests/`；已运行本批全部新增测试和直接/相邻的 127 项。
- 未用真实 Polymarket 钱包或真实 CLOB 下单验证；live 缺凭证 fail-close、shadow 账本与 producer contract 使用纯逻辑/mock 验证。
- 未在本批构建或重启 Docker；当前运行容器仍是此前镜像。源码推送后如要手测，必须重新 `docker compose build` 并重启。
- 未更改经典 `SimulationAccount.current_capital`；因此经典模拟账户余额不会因 Trader 手动单下降，这是 fix-01 单一权威源约束下的预期。机器人收益应从 TraderOrder/机器人绩效视图验证。
