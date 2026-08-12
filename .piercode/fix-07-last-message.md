# Fix-07 WS 价格覆盖修复结论

实施日期：2026-08-13
分支：`claude/batch-2-identity-finality-review-jvcsc5`

## 第一步：三问调研结论

### 1. 热订阅由谁触发，traders token 是否会走到

- `backend/services/strategy_signal_bridge.py:18-45` 在发布所有 Opportunity 前调用 `IntentRuntime.prewarm_source_tokens()`；后者位于 `backend/services/intent_runtime.py:1278-1324`，会从 durable signal contract 提取执行 token，调用 `:1189-1276` 的 `_ensure_hot_subscriptions()`，再等待最多 0.5 秒的新鲜 WS 报价。
- `traders_confluence` 生产链是 `backend/workers/tracked_traders_worker.py:752-757 → strategy_signal_bridge`，运行在 discovery plane。生产侧订阅的是 discovery 进程自己的 FeedManager；交易 plane 的 `signal_bus_redis_bridge` 只把跨进程 batch 放进本地 runtime queue，没有在交易进程重订阅 token。这是 confluence 信号进入编排器后仍为 `source=unknown` 的缺调用点。
- copy-trade 生产链是 `backend/services/traders_copy_trade_signal_service.py:594-600 → strategy_signal_bridge`，运行在 trading plane，能走既有本地热订阅；但原逻辑等待 0.5 秒后不检查结果，仍立即发布，首 tick 未到时同样会进入严格价格闸。
- `_PREWARM_SOURCES` 仍仅含 scanner（`backend/services/intent_runtime.py:117`）。本批没有把 traders 简单加入生产侧 gate，因为这不能解决 discovery 与 trading 的进程隔离，反而会把交易决策依赖错误地留在生产进程。

### 2. traders_confluence / copy-trade payload 是否带 token_id

- **带。** copy-trade service 在 `backend/services/traders_copy_trade_signal_service.py:494-543` 强制从钱包事件取得 token，并写入事件 payload；策略在 `backend/services/strategies/traders_copy_trade.py:356-417,510-520` 校验并写入 `positions_to_take[].token_id`。
- **带。** confluence 在 `backend/services/strategies/traders_confluence.py:629-643` 读取 YES/NO token，在 `:723-743` 按目标 side 选择 token 并写入 `positions_to_take[].token_id`。
- `backend/services/intent_runtime.py:463-503` 的 `_extract_required_token_ids()` 已覆盖 execution plan、positions、selected/top-level token。根因不是 token 缺失，也无需放宽消费闸。

### 3. WS 订阅容量上限

- `backend/services/ws_feeds.py:772-793` 的 Polymarket 动态订阅按每批 100 token 发送；订阅集合本身没有连接级硬上限或分片上限。
- 现有 `WS_MARKET_SUBSCRIPTION_BUDGET=15000`（`backend/config.py:182`）仅在 detection plane 生效：`backend/services/ws_feeds.py:2028-2073` 明确让 trading plane 豁免，避免开放持仓所需行情被淘汰。
- 因此本批不做“全量市场订阅”，只按进入交易编排器的信号 token 订阅；没有新增 config/schema key，也没有重复实现 LRU。

## 方案选择

选择 **方案 A：交易进程按需订阅**。

理由：信号已有 token，FeedManager 已支持动态订阅，且 trading plane 有意不受 detection 的容量预算约束。正确修复点是编排器构建 live context 之前的本进程订阅，不需要降低 shadow 的价格标准，也不需要新增第二套 shadow 放宽开关。

## 改动

- `backend/services/intent_runtime.py:1326-1382`：新增 `prewarm_execution_signals()`，按信号提取执行 token，在当前交易进程复用 `_ensure_hot_subscriptions()`，并用既有 0.5 秒 prewarm timeout 有界等待新鲜 WS 中间价。返回按 signal id 定位的 `ws_subscribe_timeout`；payload 缺 token 则返回 `ws_token_id_missing`。
- `backend/services/intent_runtime.py:858,1175,1716`：把 `ws_subscribe_timeout` 纳入既有 deferred quote 再激活语义；后续新鲜 tick 到达后使用原 runtime sequence/WS callback 路径重新发布，不新建第二套重试器。
- `backend/workers/trader_orchestrator_worker.py:5969-6004`：严格定价启用时，只对 `source=traders` 的候选在构建 live context 前执行本地 prewarm。已有新鲜 WS 价继续进入原 live-context 和严格闸门。
- `backend/workers/trader_orchestrator_worker.py:6217-6240`：订阅超时/缺 token 时，本轮在 strict gate 前延后，原因记录为 `ws_subscribe_timeout`/`ws_token_id_missing`，不写消费记录、不推进游标；因此不会再把本质为订阅覆盖不足的问题记录成 `source=unknown` 并永久吃掉信号。
- `backend/tests/test_intent_runtime_ws_freshness.py:193-291`：覆盖 traders token 确实触发订阅、无首 tick 返回明确 timeout、已有新鲜 WS 价通过、timeout signal 在新 tick 后可重新激活。

未修改以下正确行为：

- `strict_ws_pricing` 判定逻辑及 live fail-close；
- `Book spread ... exceeds max_spread_bps`；
- `copy_trade_gate_failed:min_notional`；
- fix-06 P-2 的 shadow/live 钱包凭证口径；
- 费用 helper、费用曲线和任何策略阈值。

## 默认值与缺省行为变化

| 项目 | 旧值/行为 | 新值/行为 | 理由 |
|---|---|---|---|
| traders 交易进程 WS 覆盖 | 只依赖生产进程 prewarm；跨进程后交易 feed 未订阅 | 编排器前按信号 token 在 trading feed 订阅 | FeedManager/订阅集合是进程本地状态 |
| 首 tick 等待 | 生产侧等待 0.5 秒但不以结果门控 traders 发布 | 交易侧复用 0.5 秒有界等待；超时本轮延后 | 避免 `source=unknown` 永久 blocked，同时不无限阻塞周期 |
| timeout 诊断 | strict gate 仅见 `source=unknown age=unknown` | `ws_subscribe_timeout`（缺 token 为 `ws_token_id_missing`） | 可直接定位上游订阅/报价覆盖问题 |

没有新增/修改 `default_config`、schema 或环境配置默认值。

## 测试与审计证据

- 红测：`python -m pytest backend/tests/test_intent_runtime_ws_freshness.py -k "prewarm_execution_signals" -q` → `2 failed`，均因生产代码尚无 `prewarm_execution_signals`，符合预期。
- 新增覆盖：`python -m pytest backend/tests/test_intent_runtime_ws_freshness.py -k "prewarm_execution_signals or ws_subscribe_timeout" -q` → `3 passed`。
- strict WS 安全回归：`python -m pytest backend/tests/test_trader_orchestrator_decision_gates.py -k "strict_ws_pricing" -q` → `6 passed`；覆盖 fresh `ws_strict` 通过、非 WS/缺价及过期价拒绝，live 严格性未放宽。
- orchestrator 相邻回归：`python -m pytest backend/tests/test_trader_orchestrator_worker.py -k "strict_ws_context or full_live_context or cached_live_context" -q` → `3 passed`。
- traders payload/策略回归：`python -m pytest backend/tests -k "traders_confluence or traders_copy_trade" -q` → `25 passed, 2638 deselected`。
- `python -m py_compile backend/services/intent_runtime.py backend/workers/trader_orchestrator_worker.py`：通过。
- `git diff --check`：通过。

## 未验证项

- 未连接真实 Polymarket market WS 做端到端首 tick 延迟实测；订阅、超时、fresh pass 和再激活均由纯逻辑/mock 回归验证。
- 未构建/重启 Docker；运行中的容器仍需在三批源码全部完成后统一重建，当前只提交源码与测试。
- 四个相关测试文件无筛选合跑结果为 `193 passed, 1 failed`；唯一失败是既有 `test_run_worker_loop_releases_session_before_hot_state_maintenance` 对 finally 第二次 audit flush 的次数期望（期望 1 次，实际 2 次）。该测试在本批修改前/单文件运行状态并不稳定，且不经过 traders WS prewarm 路径；遵守范围约束，未修改无关 worker 生命周期代码或测试。上列本批新增与直接相关 37 项全部通过。
- trading plane 动态订阅集合没有硬预算，这是现有持仓安全设计。长期运行的实际集合规模/消息吞吐仍应在部署后监控，但不在本 spec 内新增容量策略。
