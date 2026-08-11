# HOMERUN 钱包共识手动下单原子链设计

## 1. 目标

修复用户在“钱包共识信号”中手动确认买入后，界面提示已提交，但模拟账户余额、持仓和可见订单均不变化的问题。

本次把手动操作接入 HOMERUN 已有的统一执行会话、盘口撮合、风险门禁和订单投影链；Shadow 只有在真实模拟撮合产生正数成交后，才允许在同一数据库事务内写入订单、模拟交易、模拟持仓、现金账本、账户余额和交易员持仓投影。任一步失败均不得返回成功。

## 2. 已确认事实

1. 用户在北京时间约 09:19 和 09:22 的两次操作分别留下 `$50`、`$25` 的 `TraderOrder`，状态为 `shadow/submitted`，但都没有 `simulation_ledger`。
2. 原路由在任何 Shadow 撮合和资金写账之前提交 `TraderOrder`，随后即返回 `success`。
3. 前端把钱包信号构造成 `token_id=""`，后端又把展示名称拼成 `buy_球员名/队名`；维护工作器随后稳定报 `Unsupported direction`，所以无法补建模拟交易、持仓和现金分录。
4. 两条订单仍能从全局订单 API 查询到，但持仓页按 `payload.simulation_ledger.account_id` 过滤当前模拟账户，因此它们被隐藏。
5. 当前钱包机会快照已经携带权威的 `positions_to_take[].token_id`、`outcome`、`action`、价格和市场身份；匹配的信号还带有规范方向 `buy_yes/buy_no`。缺失身份是前端归一化和旧路由丢弃字段造成的，不需要猜测或新增外部接口。
6. 该缺陷来自 `origin/main` 已包含的上游手动下单实现，不是当前 `f1f48c55` Shadow timeout 修复引入。

## 3. 非目标

- 不放宽钱包策略、价差、流动性、WS 新鲜度、余额或持仓上限。
- 不把“手动买入”改成自动跟单，也不改变钱包发现、实体聚类或共识评分。
- 不清理、改写或补成交现有两条错误 `submitted` 历史记录；它们另行生成只读清理预览后再由用户授权。
- 不删除第二个隔离证明账户，不切换当前主 Shadow 账户。
- 不注入实盘密钥，不在本次验证中发送真实订单。
- 不新建另一套撮合、风控或结算算法。

## 4. 必须成立的不变量

### 4.1 服务端身份权威

- 请求必须携带 `opportunity_id` 和客户端幂等键 `client_request_id`。
- API 必须从当前 scanner/traders snapshot 重新读取同一个机会；客户端的 token、市场、方向和价格只作为待校验声明，不能作为唯一交易事实。
- 服务端从 `positions_to_take` 和 market token 映射生成规范 `buy_yes` 或 `buy_no`；普通手动买入不允许生成 `buy_展示名称`。
- token、outcome、market 或 position 数量与当前快照不一致时，在任何执行和经济写入前拒绝。
- 当前 `manual-buy` 只接受买入 outcome；真实 SELL/平仓继续走既有平仓链，不能伪装成买入。

### 4.2 复用统一执行链

- API 为本次人工确认生成一条可审计的 manual decision 和 runtime signal view，然后调用现有 `ExecutionSessionEngine.execute_signal`。
- `ExecutionSessionEngine` 继续拥有 execution plan、typed identity、order-book、spread/slippage/daily spend、Shadow 微观撮合、Live placeholder、CLOB idempotency 和 session 终态。
- 不再直接调用旧路由里的 `live_execution_service.place_order`，也不再直接构造“submitted 即成功”的订单。
- 所选机器人决定执行模式和风险参数；信号来源仍是原机会来源。机器人策略代码不被冒充为该手动信号的预测来源。

### 4.3 Shadow 原子经济写入

- `execute_signal` 增加可选 `shadow_account_id`；只有显式传入时启用 inline ledger，现有自动执行调用在本次不改变。
- Shadow 盘口估算无成交、身份不完整或门禁拒绝时：`orders_written=0`、余额不变、无 SimulationTrade/SimulationPosition/CashLedgerEntry，并向 API 返回明确拒绝原因。
- Shadow 有正数成交时，在最终 execution projection 的同一事务中依次：
  1. flush `TraderOrder`；
  2. 调用现有 `SimulationService.record_orchestrator_shadow_fill(..., commit=False)`；
  3. 把返回的 `account_id/trade_id/position_id/cash_ledger_entry_id` 写入订单 `payload.simulation_ledger`；
  4. 同事务同步 `TraderPosition`；
  5. 一次 commit。
- 账户不存在、余额不足、现金账本冲突或 commit 失败时，订单和全部经济写入一起回滚，API 不得返回成功。
- 费用和滑点沿用 execution result 中的 `estimated_fee_usd`、`slippage_usd`，余额扣减使用实际成交 notional 加费用，而不是 UI 输入名义金额。

### 4.4 幂等与并发

- `client_request_id + trader_id` 派生固定 manual decision id；decision 是 provider/Shadow 执行前的 durable reservation。
- 同一幂等键、相同请求重放时不得再次执行，只返回已存在的 decision/session/order 状态。
- 同一幂等键但金额、机会或 order type 不同，返回 idempotency conflict。
- Live 继续由现有 execution session 的 pre-submit placeholder 和 CLOB idempotency key 防止未知结果下重复发单；本次运行验证保持 Live 未配置。

### 4.5 状态与界面语义

- Shadow API 只有在至少一笔成交订单及其 `simulation_ledger` 已提交后返回 `status=success`；Live 只有在既有 execution session 返回可识别的 provider 成交/工作中订单后才返回成功，不要求模拟账本。
- 无成交返回 `rejected/skipped` 的明确错误；执行异常返回 failed，不允许 200 + success。
- 返回值包含 `decision_id`、`session_id`、`account_id` 和订单列表，便于日志对账。
- 前端成功后必须立即失效 `simulation-accounts`、`positions-panel`、`accounts-panel` 和交易员订单缓存；不能只失效当前不存在的 `trader-orders` key。
- Shadow 的“限价”不冒充可长期挂单：本次只允许即时盘口撮合；Live 限价仍由现有 GTC execution session 处理。

## 5. 数据流

### 5.1 Shadow 成功

`用户确认` → `服务端重读机会并校验 identity` → `幂等 manual decision commit` → `ExecutionSession durable intent` → `现有盘口/风控/Shadow 撮合` → `TraderOrder + SimulationTrade + SimulationPosition + CashLedger + TraderPosition 同事务 commit` → `API success` → `前端刷新余额/订单/持仓`

### 5.2 Shadow 无成交或拒绝

`用户确认` → `身份校验` → `manual decision` → `执行门禁或撮合无成交` → `session/decision skipped 或 failed` → `API 明确失败`；余额、模拟交易、模拟持仓和现金账本均不变化。

### 5.3 重复请求

`相同 client_request_id` → `命中已有 decision` → `校验请求指纹` → `返回原 session/order 结果`；不再次访问 provider 或 Shadow 撮合。

## 6. 实现边界

| 文件 | 责任 |
|---|---|
| `backend/api/routes_traders.py` | 请求契约、当前快照权威校验、幂等 decision、构造 manual runtime signal、调用统一执行引擎、严格返回语义 |
| `backend/services/trader_orchestrator/session_engine.py` | 可选 Shadow inline ledger；最终投影中订单/账户/持仓/现金账本同事务 |
| `backend/tests/test_routes_trader_manual_buy.py` | 身份、幂等、模式/暂停门禁、成功/拒绝返回契约 |
| `backend/tests/test_execution_session_engine.py` | inline ledger 原子成功、余额不足回滚、无成交零经济效果、默认调用行为不变 |
| `frontend/src/components/TraderSignalViews.tsx` | 保留机会已有的 token/canonical outcome 信息，不再只保留展示方向 |
| `frontend/src/components/BuyButton.tsx` | 稳定 client request id、诚实的 Shadow order type、正确 success 判定和查询刷新 |
| `frontend/src/services/apiTraders.ts` | 请求/响应类型与后端契约一致 |

不修改策略阈值、钱包发现/聚类、结算公式、数据库 schema 或 Alembic migration。

## 7. 风险与失败处理

- **机会过期或快照轮换：** 返回 stale/not found；不得使用客户端旧 token 猜测下单。
- **盘口缺失/价差过宽/无成交：** 返回具体 execution reason；不创建经济订单。
- **账户余额不足：** inline ledger 抛错，整个最终 projection 回滚，decision/session 保留可审计失败状态。
- **Shadow account 未配置：** 在创建 execution session 前拒绝，不能退回“先写订单、以后补账”的旧模式。
- **提交结果未知：** Shadow 无外部经济效果；Live 依赖既有 placeholder/reconciliation，不允许 API 盲目重试。
- **前端响应丢失：** 同一 modal 生命周期复用相同 `client_request_id`，重试返回原结果。
- **旧错误订单：** 继续保留事实，不将其反填为成交，也不据此修正余额。

## 8. 验证门禁

1. 先用当前源码跑 RED：现有路由仍会用空 token/非规范方向写假 submitted，且没有 inline ledger。
2. PostgreSQL 集成测试必须证明：成功一次只扣一次余额；重复请求不再扣款；余额不足时所有经济表数量和余额不变。
3. 真实 `submit_execution_leg` 的聚焦测试必须证明：盘口无成交/身份不完整不返回 success。
4. 运行 session engine、manual route、cash ledger、Shadow backfill、position inventory 相关回归。
5. 前端 `npm run build`、Python `py_compile`、`git diff --check` 全部通过。
6. 从固定源码构建新 backend/frontend 标签，不使用 `latest`；保持 Live credentials absent。
7. 先用隔离模拟账户通过真实 API 下单并核对六方一致：API response、TraderOrder、SimulationTrade、SimulationPosition、CashLedger、SimulationAccount。
8. 再由用户在当前主 Shadow 账户手动选择一条新钱包共识信号；记录下单前后余额、订单 ID、账本 ID和持仓。旧两条错误记录不参与验证。

## 9. 回滚

- 无 schema 变化；回滚只需切回当前固定 backend/frontend 镜像标签。
- 回滚前停止新手动请求，等待正在执行的 session 终态化；不得直接删除 placing/submitted 行。
- 已成功提交的 Shadow 经济记录是真实模拟事实，回滚代码不撤销它们；若需冲正，必须走现金账本 reversal/显式 repair 流程。
