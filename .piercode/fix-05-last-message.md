# fix-05 批次 B 策略修复结论

> **重要行为变更（G-2）**：`btc_eth_convergence` 已从旧的 latency/未重定价模式恢复为文档定义的“临近到期收敛”模式：仅在剩余 5–45 秒、方向合约价格 0.85–0.95、oracle 新鲜且费后 EV 通过时入场。该变化会显著改变信号分布。进入 live 前必须由操作员决定 shadow A/B 对照时长；本批不替操作员指定时长。

## 结果

- 分支：`claude/batch-2-identity-finality-review-jvcsc5`
- G-0：`97d8f963` — `G-0: add shared crypto probability estimator`
- G-1：`6ae51abf` — `G-1: restore probability-based directional edge`
- G-2：`8f53c6b0` — `G-2: restore near-expiry convergence semantics`
- G-3：`8cac370c` — `G-3: wire maker quote inventory economics`
- G-4：`732eff0e` — `G-4: repair small crypto strategy economics`
- B0.1 费用统一：只验收，未重做 fix-03。`backend/services/strategies/crypto_strategy_utils.py:100-102` 仍委托 `utils.kelly.polymarket_taker_fee_pct(..., category="crypto")`；`backend/services/strategies/crypto_strategy_utils.py:341-346` 的 fee-aware edge 同样走 canonical helper。生产代码 grep 未发现 `polymarket_taker_fee_legacy_quartic` 调用，唯一非测试命中是 `backend/utils/kelly.py` 的 legacy 函数定义。

## G-0 共享层

- `backend/services/strategies/crypto_strategy_utils.py:110-133`：新增 `estimate_p_win`，按 elapsed ratio 收缩 sigmoid scale，并统一概率上下界。原因：directional/convergence 必须共享同一概率口径。
- `backend/services/strategies/crypto_strategy_utils.py:349-399`：新增不规则 oracle 样本的共享 realized-vol-per-second estimator；`backend/services/strategy_helpers/crypto_strategy_utils.py:27-30` 重新导出。原因：midcycle 与 digital 不再各自维护概率/波动实现。
- `backend/tests/test_crypto_strategy_utils.py:15-70`：覆盖概率单调性、时间衰减、概率 clamp、timeframe 默认值以及 canonical crypto fee 委托。

## G-1 directional edge

- `backend/services/strategies/btc_eth_directional_edge.py:630-737`：接通 `directional_*` phase 阈值/score multiplier，并使用共享 `estimate_p_win`；edge 统一为 `(p_win - entry_price) * 100` 概率点。
- `backend/services/strategies/btc_eth_directional_edge.py:793-819`：仓位改为基于 `p_win` 与 entry price 的 quarter-Kelly，并受现有上限约束。
- `backend/services/strategies/btc_eth_directional_edge.py:2175-2215`：detect 使用 probability edge，并写入 model probability/两侧价格 guardrail context；费用闸只在 evaluate 走 canonical fee-aware helper一次。
- `backend/services/strategies/btc_eth_directional_edge.py:2846-2877`：skip 路径不构建 decision payload；同时加入有界 runtime cache/熔断配置，避免热路径重复工作。
- 为什么：旧 edge 量纲混用、费用闸默认关闭、guardrail 缺字段以及反向 edge sizing 会让信号与真实 EV 脱节。
- `backend/tests/test_btc_eth_directional_edge_fix05.py:51-115`：覆盖概率单调/时间衰减、0.5 点费后边界、Kelly 数值表、guardrail context 和 skip 懒构建。

## G-2 convergence

- `backend/services/strategies/btc_eth_convergence.py:614-624`：5–45 秒窗口、0.30% oracle move、0.85–0.95 价格带与 probability 参数集中到 `default_config`。
- `backend/services/strategies/btc_eth_convergence.py:660-708`：共享概率 estimator 计算 p_win、gross edge、canonical fee hurdle 和 net edge。
- `backend/services/strategies/btc_eth_convergence.py:1484-1510`、`backend/services/strategies/btc_eth_convergence.py:2396-2460`：detect/evaluate 统一临期收敛语义并硬拒 stale/missing oracle；不再把“尚未重定价”作为 convergence 定义。
- `backend/services/strategies/btc_eth_convergence.py:4102-4114`：oracle 方向翻转优先触发退出，不被 binary hold-to-resolution 吞掉。
- 为什么：恢复策略文档语义，并将旧 latency 信号职责留给 directional 策略。
- `backend/tests/test_btc_eth_convergence_fix05.py:53-125`：覆盖窗口内入场、窗口外拒绝、ML prior 对称调整、oracle 新鲜度硬闸、oracle flip 退出顺序和懒 snapshot。

## G-3 maker quote

- `backend/services/strategies/btc_eth_maker_quote.py:592-608`：maker spread/score/skew/combined edge、2% leg tolerance、300 秒 session、per-timeframe hedge timeout 等全部进入 `default_config`。
- `backend/services/strategies/btc_eth_maker_quote.py:641-711`：oracle skew 封顶并保持 YES/NO 权重和为 1；评分由 spread、thin-book bonus 和 max score 构成。
- `backend/services/strategies/btc_eth_maker_quote.py:826-979`：组合成本闸使用 `maker_min_combined_edge`；maker 两腿用 `polymarket_maker_fee(..., category="crypto")=0`，只有容忍比例对应的补腿跨价成本使用 taker fee；未对冲上限由 leg notional × tolerance 派生。
- `backend/services/strategies/btc_eth_maker_quote.py:2523-2565`：maker 主路径启用最小 spread、流动性和临近到期撤单窗口；oracle direction 不再豁免。
- 为什么：修复对称固定权重、硬编码组合成本、库存容忍矛盾、平坦 hedge timeout 和 maker 费被高估的问题。
- `backend/tests/test_btc_eth_maker_quote_fix05.py:15-118`：覆盖 skew cap/方向/权重和、组合成本边界、score、库存与费用、临期撤单和懒 snapshot。

## G-4 五个小策略

### midcycle

- `backend/services/strategies/crypto_5m_midcycle.py:105-105`、`:195-207`：`win_prob_estimate=0.80` 进入 default/schema；catalog 同步在 `backend/services/opportunity_strategy_catalog.py:1054-1063`。
- `backend/services/strategies/crypto_5m_midcycle.py:396-405`：CycleTracker 周期从硬编码 300 秒改为 `timeframe_seconds`。
- `backend/services/strategies/crypto_5m_midcycle.py:528-544`：有 oracle history 时优先用共享 realized vol + `StrategySDK.prob_above`，否则用配置 fallback；ROI/EV 显式扣 `polymarket_taker_fee(..., category="crypto")`，已删除 `fee=0` 与 `skip_fee_model`。
- `backend/tests/test_crypto_5m_midcycle_strategy.py:382-424`：覆盖 canonical fee/net EV、配置 fallback 与 history probability 覆盖。

### digital sigma

- `backend/services/strategies/crypto_digital_sigma_edge.py:104-125`：oracle age 改为 per-timeframe helper override；`fee_buffer` 改为 0，仅表示 canonical fee 之外的额外 margin；新增缺 z-score 的 reject/reduce-confidence 配置。
- `backend/services/strategies/crypto_digital_sigma_edge.py:369-385`：realized vol 按 `(market_id, len(history), last_ts)` 缓存。
- `backend/services/strategies/crypto_digital_sigma_edge.py:453-477`：UP/DOWN 每侧分别用 canonical taker fee 后再比较 net edge；删除 `fee=0`/`skip_fee_model`。
- `backend/services/opportunity_strategy_catalog.py:1135-1269`：同步 oracle override、fee margin 与缺 z-score schema。
- `backend/tests/test_crypto_digital_sigma_edge_fix05.py:52-105`：覆盖 reject 策略、降 confidence + canonical fee 数值、realized-vol cache 命中。

### spike reversion

- `backend/services/strategies/crypto_spike_reversion.py:296-314`：scoring pass 增加显式 `edge >= min_edge_percent` GateResult。
- `backend/services/strategies/crypto_spike_reversion.py:113-124`、`:517-525`：拒绝原因直接取同一次 scoring pass 的失败 GateResult；旧 `_rejection_reason` 重算函数已删除。
- `backend/services/strategies/crypto_spike_reversion.py:311-318`：confidence clamp 下限 0.44→0.30（默认 `min_confidence=0.44` 不变）；`backend/services/strategies/crypto_spike_reversion.py:730` evaluate sizing 改读 `liquidity_cap_fraction` 配置。
- `backend/tests/test_crypto_strategy_safety_gates.py:126-145`：覆盖显式 min-edge 与单次 reversion-shape 计算。

### entropy maker

- `backend/services/strategies/crypto_entropy_maker.py:72-80`、`:222-232`、`:704-708`：`min_entropy` 默认与 detect/evaluate inline fallback 全部从 0.82 改为 0.0；注释固定数学冲突 `H(0.80)=0.722 < 0.82`。
- `backend/services/strategies/crypto_entropy_maker.py:134-147`、`:598-606`：拒绝原因来自同一次 scoring GateResult，旧 `_rejection_reason` 已删除。
- `backend/services/strategies/crypto_entropy_maker.py:433-450`：所有 confidence clamp 下限 0.40→0.30（默认 `min_confidence=0.40` 不变）。
- `backend/services/opportunity_strategy_catalog.py:1482-1521`：cancel recovery、orderflow、recent move 参数明确标成 bonus threshold，不是过滤器。
- `backend/tests/test_crypto_strategy_safety_gates.py:262-286`：覆盖默认 entropy 互斥解除与单次 entropy 计算。

### distance edge

- `backend/services/strategies/crypto_distance_edge.py:100-122`、`:226-292`：新增 `distance_cost_tiers_bps=[]` 与校验/选择 helper；非空 bps tiers 优先，空列表保留旧 USD tiers 行为。
- `backend/services/strategies/crypto_distance_edge.py:409-423`：最短剩余时间默认 10→60 秒，并使用共享 timeframe helper 作为无显式配置时的 fallback；oracle age 同样改为 timeframe helper。
- `backend/services/strategies/crypto_distance_edge.py:621-646`：history 可用时用 realized vol + `prob_above`；canonical crypto taker fee 后必须 `EV>0` 才 emit。
- `backend/services/opportunity_strategy_catalog.py:1297-1353`：同步 bps tiers、60 秒与 oracle-age override schema。
- `backend/tests/test_crypto_distance_edge_strategy.py:371-414`：覆盖默认负 EV 拒绝、history probability 正 EV、bps 优先于 USD tiers。

## 默认值变化表

| 策略/键 | 旧值 | 新值 | 理由 |
|---|---:|---:|---|
| directional `opening_directional_buy_yes_enabled` | `true` | `false` | 禁止周期开端默认抢跑 |
| directional `opening_directional_buy_no_enabled` | `true` | `false` | YES/NO 开端闸保持对称 |
| directional `min_edge_percent` | `3.0`（旧 edge 量纲） | `1.5` 概率点 | edge 改为 p_win-entry_price 后重标定 |
| directional `min_execution_adjusted_edge_percent` | `0.0` | `0.5` | 默认启用费后执行边际 |
| directional `min_oracle_move_pct` | 缺省路径约 `0.30` | `0.15` | detect 仅作粗筛，最终由概率 EV 闸决定 |
| directional `kelly_fraction` | 无 | `0.25` | quarter-Kelly |
| convergence 5–45 秒/0.85–0.95/0.30% | 数值已存在但旧 latency 语义未接线 | 数值不变，改为权威 convergence 闸 | 行为大改，不伪装成参数微调 |
| convergence `exit_on_oracle_flip` | 无 | `true` | oracle 翻转必须先于 binary hold 退出 |
| convergence `kelly_fraction` | 无 | `0.25` | p_win 驱动 sizing |
| maker `reentry_cooldown_seconds_per_market` | `0` | `5` | 避免同市场立即重挂 |
| maker `min_oracle_move_pct` | 两处 `0.15/0.30` | 单一 `0.15` | 消除默认值分叉 |
| maker combined edge | 硬编码 `1-0.998=0.002` | `maker_min_combined_edge=0.015` | 组合成本成为可审计单一真值源 |
| maker unhedged tolerance | 硬编码 `$0` 与 ratio `0.02` 冲突 | `leg_notional × 0.02` | 库存上限与 leg tolerance 同源 |
| maker hedge timeout | 平坦 `20s` | `{5m:5,15m:10,1h:20,4h:30}` | 按周期控制补腿风险 |
| midcycle `win_prob_estimate` | 硬编码 `0.80` | 配置 `0.80` | 数值不变，进入 default+schema；history 优先 |
| digital `fee_buffer` | `0.015` 平坦费用近似 | `0.0` 额外 margin | 真实费用逐侧走 canonical 曲线 |
| digital `max_oracle_age_ms` | `4000` | `None`→5m `5000`/15m `7500` helper | 与 timeframe 新鲜度策略统一 |
| digital 缺 z-score policy/multiplier | 无（缺失按 0 fail-open） | `reduce_confidence` / `0.80` | 缺数据不再等同正常波动 |
| entropy `min_entropy` | `0.82` | `0.0` | 与 `min_entry_price=0.80` 的数学互斥解除 |
| distance `distance_cost_tiers_bps` | 无 | `[]` | 非空时优先；默认空不回退用户旧 USD 行为 |
| distance `min_seconds_to_resolution` | `10` | `60` | 15m helper 的 resolution safety 默认 |
| distance `max_oracle_age_ms` | `5000` | `None`→15m `7500` helper | 与 timeframe 新鲜度策略统一 |

## 验证证据

1. `python -m py_compile`：本批所有触碰的 strategy、catalog、shared helper 与测试文件通过。
2. `python -m pytest backend/tests/test_crypto_strategy_utils.py backend/tests/test_btc_eth_directional_edge_fix05.py backend/tests/test_btc_eth_convergence_fix05.py backend/tests/test_btc_eth_maker_quote_fix05.py backend/tests/test_crypto_5m_midcycle_strategy.py backend/tests/test_crypto_digital_sigma_edge_fix05.py backend/tests/test_crypto_distance_edge_strategy.py backend/tests/test_crypto_strategy_safety_gates.py -q`：`103 passed`。
3. `python -m pytest backend/tests/test_strategy_catalog_seed_create_only.py backend/tests/test_backtest_crypto_on_event_dispatch.py backend/tests/test_crypto_market_reconstruction.py -q`：`23 passed, 1 warning`；warning 为既有 `services/scanner.py:60` “There is no current event loop”。
4. G-4 最后单一真值源修正后补跑 `python -m pytest backend/tests/test_crypto_strategy_safety_gates.py -q`：`17 passed`。
5. `git diff --check` 与 staged `git diff --cached --check` 均通过。
6. 配置 key grep：`win_prob_estimate=14`、`missing_recent_move_zscore_policy=3`、`missing_recent_move_confidence_multiplier=3`、`distance_cost_tiers_bps=6`、`min_entropy=9`、`liquidity_cap_fraction=7`，均非死配置。

## 未验证项与操作注意

- 未运行真实资金或真实下单；本批只验证纯逻辑、catalog seed 和 backtest dispatch。
- 策略文件顶部已说明：已存在数据库中的 `strategies.source_code` 是运行时 master。本批未修改数据库/迁移；现存安装是否需要 reset-to-factory/reseed 由操作员按部署流程核对，不能仅凭源码提交断言运行库已更新。
- 未做真实 WS 深度、maker 排队位置、补腿成交和真实滑点验证；这些必须在 shadow 运行中观测。
- G-2 必须单独做 shadow A/B；操作员决定对照时长后再评估 live。
- 新阈值尚未用用户的真实市场样本重新标定；当前值严格按 reconciled spec 落地。
