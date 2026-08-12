# Fix-04 批次 A 策略修复结论

## 分支与提交

- 分支：`claude/batch-2-identity-finality-review-jvcsc5`
- 最终 rebase 基点：`7c51d161`
- F-1：`33335814 F-1: reconcile copy-trade defaults and live gates`
- F-2：`6cbae424 F-2: weight and single-pass trader confluence`；远端并发补充裁决后的修正为 `dd485e58 F-2: remove dead medium tier weight`
- F-3：`5bb6ef08 F-3: unify and harden news edge estimation`
- F-4：`b13d2cb0 F-4: repair news momentum thresholds and exits`
- F-5：`be0cd53c F-5: enforce recent side-aware certainty shocks`

严格按 `docs/fix-04-batch-a-reconciled.md` 的第零节裁决实施；未应用作废的 `docs/repair/traders_copy_trade_p0_draft.patch`，也未恢复 WO-A1.1/A1.2 的旧方案。

## F-1 `traders_copy_trade`

- `backend/services/strategies/traders_copy_trade.py:30,387-395,608-610`：detect/evaluate 的缺省 confidence 都读取 `min_confidence=0.45`；继续使用 fix-02 的 `edge_ev=confidence-entry_price`。缺 confidence、entry=0.40 的新固定行为为 confidence/payout=0.45、edge EV=0.05、ROI=12.5%。原因：消除 detect 0.70 与 evaluate 0.0 的双缺省，同时不回退 EV 语义。
- `backend/services/strategies/traders_copy_trade.py:49,55,618-627,934`：`max_copy_drawdown_pct 100→50`，`leader_allocation_cap_pct 100→25`；`backend/services/strategies/traders_copy_trade.py:101-117` 给三个 1,000,000 预算加“上线实盘前必须按账户规模设置”，数值不改。
- `backend/services/strategies/traders_copy_trade.py:398-403,509`：同一 copy timestamp 只解析一次；仅使用真实 `end_date/endDate`，取不到不设置 `resolution_date`。
- `backend/services/strategies/traders_copy_trade.py:41,82,712-731`：新增 `require_live_context=false`。默认维持缺上下文 fail-open；显式开启后，缺 liquidity/drift 仍用原检查名 `live_liquidity`/`entry_drift` 拒绝。
- `backend/services/strategies/traders_copy_trade.py:307`：配置只在 `configure()` 校验/缓存，不在每个信号热路径重复校验。
- 回归：`backend/tests/test_traders_copy_trade_strategy.py:244-264,267-379` 覆盖 confidence+EV、风控默认、真实结算时间、live-context 两态、configure 调用计数和 SELL 库存减仓。

对账项仍生效：`backend/services/strategies/traders_copy_trade.py:647-667,835-847` 继续用 `effective_max_age=min(hard_ceiling, copy_delay+requested_max_age)`，不篡改用户 max-age；`backend/services/strategies/traders_copy_trade.py:395-397` 继续用 fix-02 EV/payout 公式，未恢复 `edge_midpoint/edge_multiplier`。

## F-2 `traders_confluence`

- `backend/services/strategies/traders_confluence.py:195,694`：方向不能明确解析时返回 `None` 并跳过，不再猜 NO。
- `backend/services/strategies/traders_confluence.py:99,509`：`firehose_max_age_minutes 720→60`；firehose runtime 不再先用通用 720 覆盖策略默认。
- `backend/services/strategies/traders_confluence.py:91,157-173,298-337,534-536`：新增实际可产生的三档 tier weights（low/high/extreme）；有效钱包数优先 cluster-adjusted 名单/计数，再退 wallets/原计数，最小钱包闸按加权和判断。未知 tier 保守使用 low 权重，配置中的 medium/未知 key 被丢弃，不形成死配置。
- `backend/services/strategies/traders_confluence.py:703-795,983-1006`：tier weight 进入 score，strength/tier/weighted count 都经当前 payload 传递；无 `self._confluence_strength` 实例态。
- `backend/services/strategies/traders_confluence.py:117-153,571-581,676-680`：配置按 version memoize；filtered row 携带 validation，builder 不重跑 evaluate。
- `backend/services/traders_firehose_pipeline.py:95-97,165,229-231`：pipeline 只调用策略 prepare/filter/build，不做提前或尾部 normalize，保持 fix-03 的“缺 source_flags”与“显式全 False”可区分。
- `backend/services/opportunity_strategy_catalog.py:1502-1505`：schema 明确最小值是 tier-weighted wallet count，并暴露 `tier_weights`。
- 回归：`backend/tests/test_traders_firehose_provenance.py:181-206,288-368` 覆盖未知方向、cluster-adjusted 加权、单次 normalize/evaluate、pipeline 不提前 normalize、payload 局部并发状态。

对账项仍生效：`backend/services/strategies/traders_confluence.py:88-89,521-524,953-964` 保持 fix-03 的 `min_confluence_strength=0.50`、`min_tier=low` 单一回退值。`backend/services/wallet_intelligence.py:478-483` 只产生 EXTREME/HIGH/WATCH，WATCH 归一化为 low；依照 2026-08-13 pull 到的最新权威裁决，medium 不进入 `tier_weights`，同时不越界修改上游 tier 分档。

## F-3 `news_edge`

- `backend/services/news/edge_estimator.py:84,176-331,530,676-700`：semantic scan 与 workflow 共用 `EdgeEstimator`；所有 LLM 调用保留 20 秒 timeout、novelty/relevance/confidence 过滤。
- `backend/services/news/edge_detector.py:1-13`：旧 import 契约仅别名到同一 estimator/singleton，无第二套 schema、prompt、LLM 或 edge 算法。选择委托入口而非删除，是因为现有 AI tool 仍从该模块读取 cached edges；WO-A3 明确允许“兼容壳或删除”二选一。
- `backend/services/news/edge_estimator.py:191-237,260-293`：LLM cache key 为 `(article_id, market_id, round(price,2))`，TTL 至少 `3 * NEWS_SCAN_INTERVAL_SECONDS`；同 key 命中不再调用 LLM。
- `backend/services/strategies/news_edge.py:193-203,709-716`：embedding 只传本实例尚未处理的 article_id，调用完成后再标记。
- `backend/services/strategies/news_edge.py:329-360`：age 按 `is not None` 链读取，0 不再被后续 truthy 字段覆盖；edge 按 `exp(-ln2*age/half_life)` 衰减。
- `backend/services/strategies/news_edge.py:74-76,145-156,365-435`：新增 CI 闸、`llm_shrinkage_k=0.7`、类别 half-life 默认 45 分钟；顺序为 probability shrinkage → CI → decay → canonical category fee → min net edge。
- `backend/services/strategies/news_edge.py:372-385`：费用直接调用 fix-03 `utils.kelly` canonical helper，Polymarket 尽可能传 estimator category；无 legacy 四次曲线。
- `backend/services/strategies/news_edge.py:748-768,832-878`：发出前按真实 SDK 签名 `get_live_price(market, prices, side)` 刷新 side 价并重算所有 edge 闸；刷新后低于阈值的数量进入日志。
- `backend/services/strategies/news_edge.py:1017-1021`：score 改为 `fee_adj_edge_pct + confidence*10`，代码注明权重需 shadow/backtest 校准。
- 回归：`backend/tests/test_news_edge_repair.py:59-209` 覆盖 age=0、半衰期、CI/收缩独立、canonical category fee、实时价刷新、cache、增量 embedding、score、TTL；`backend/tests/test_strategy_threshold_single_source.py:98-132` 继续验收 fix-02 detect/evaluate 阈值单源。

## F-4 `news_momentum_breakout`

- `backend/services/strategies/news_momentum_breakout.py:115,242-254,424,511-518`：新增相对突破 25%，按 rise/baseline 判定；0.20 与 0.70 基线同为 25%。relative>0 时优先，显式设 0 才使用旧 absolute=0.10 后备；schema 关系见 `backend/services/opportunity_strategy_catalog.py:579-603`。
- `backend/services/strategies/news_momentum_breakout.py:97-104,143,887`：scale-out 改为 +25%/+45% 各减 0.33，remainder trailing=12%；`trailing_stop_pct 18→12`。Base 的 target 字段虽名为 `trigger_bps`，实际与 pnl_pct 比较，故值为 25/45；Base 的 trailing 真按 bps 除 10000，故用 1200 表示 12%。机器人模板同步见 `backend/services/trader_orchestrator/templates.py:286`，避免模板把新默认覆盖回 18。
- `backend/services/strategies/news_momentum_breakout.py:148,892,915-929`：新增 `stall_giveback_fraction=0.5`；45 分钟无新高且价格低于 `high-0.5*(high-entry)` 退出，不再要求跌回 entry。
- `backend/services/strategies/news_momentum_breakout.py:257-289,450-476`：deque maxlen 改为 `ceil(stale_history_seconds/5)`，默认 360；每周期按 `2*stale` GC history/last_emit，兼容升级前没有 last_seen 的历史行。
- `backend/services/strategies/news_momentum_breakout.py:184,226-239`：内置短词 regex 类加载预编译，动态排除词首次编译后缓存。
- `backend/services/strategies/news_momentum_breakout.py:760-775`：score 全部读取 `ScoringWeights`，删除 0.65/28/12/10000 inline 副本。
- `backend/services/strategies/news_momentum_breakout.py:117,426-427,546`：`target_distance_to_one_fraction 0.55→0.35`，标注仍需校准。
- 回归：`backend/tests/test_news_momentum_breakout_repair.py:24-158` 覆盖相对阈值两价位、absolute fallback、deque 容量、GC、stall 回吐、scale-out 单位、scoring weights、新默认。

对账项仍生效：`backend/services/strategies/news_momentum_breakout.py:643-648,729-731` 的 custom liquidity gate 继续从策略 config/default 读取 3000；`backend/tests/test_strategy_threshold_single_source.py:135-160` 固定 2500 被拒，未恢复旧 1500。

## F-5 `certainty_shock`

- `backend/services/strategies/certainty_shock.py:80-81,221-239,302-304,392-398`：新增 recent window=900 秒、recent share≥0.6。recent move 和 total move 都按目标 side 的 `current-min` 计算；total 使用该市场保留 history，使 key 不会因 lookback 与 recent 同为 900 而恒真。
- `backend/services/strategies/certainty_shock.py:349,362-389`：history 改 `(ts, yes, no)`；YES/NO 各用自身序列算 move、peak/trough、retrace 和 target，NO 不再从 YES 反推。
- `backend/services/strategies/certainty_shock.py:89,134-144,314`：`shock_min_expected_move 0.03→0.04`；configure 强制 `expected_move >= min_edge_percent/100`。
- `backend/services/strategies/certainty_shock.py:77,114`：风险闸采用 spec 允许的默认值方案，`max_risk_score 0.70→0.60`，同步真正由 Base evaluate 读取的 pipeline default。理由：这是比新增未经标定的流动性/临期风险公式更小、可审查的改动；risk=0.61 的信号现会被默认闸拒绝。
- `backend/services/strategies/certainty_shock.py:24,207-218,241-258,277,320-349`：calendar 顶层 import；deadline 按 market id + end_date/question signature 缓存；单周期只取一次 now；deadline/日期窗口通过后才追加 history；超时 market 同步 GC history/deadline cache。
- schema：`backend/services/opportunity_strategy_catalog.py:262-268` 暴露 recent window/share 与 min expected move。
- 回归：`backend/tests/test_certainty_shock_strategy.py:30-239` 覆盖 fix-02 词边界/900 秒窗口，以及 slow 6h drift recent share<0.6、末 14 分钟集中重定价、NO side、dead-zone、0.60 risk、deadline cache、history 顺序、GC。

对账项仍生效：`backend/services/strategies/certainty_shock.py:300,332-334` 继续用 fix-02 预编译 `\b...\b` 排除词，不恢复子串匹配；`shock_lookback_seconds` 保持 900 秒。

## 默认值与缺省行为变更

| F | key/行为 | 旧值 | 新值 | 理由 |
|---|---|---:|---:|---|
| F-1 | missing confidence（detect/evaluate） | 0.70 / 0.0 | 0.45 / 0.45 | 与 min_confidence 同源；固定 fix-02 EV 新结果 |
| F-1 | `leader_allocation_cap_pct` | 100 | 25 | 限制单 leader 暴露 |
| F-1 | `max_copy_drawdown_pct` | 100 | 50 | 收紧跟单回撤容忍 |
| F-1 | `require_live_context` | 不存在 | false | 默认兼容；live 可显式 fail-close |
| F-2 | `firehose_max_age_minutes` | 720 | 60 | 避免旧钱包流被当实时共识 |
| F-2 | `tier_weights` | 不存在 | low 1 / high 2 / extreme 3；未知→low | 只保留实际上游会产生的 tier，避免 medium 死权重 |
| F-3 | `require_ci_clears_market` | 不存在 | true | 拒绝 CI 跨市场价的非显著 edge |
| F-3 | `llm_shrinkage_k` | 不存在 | 0.7 | 缓解 LLM 过度偏离市场 |
| F-3 | `edge_half_life_minutes_by_category` | 不存在 | default 45 | 新闻 edge 随时间衰减 |
| F-3 | LLM cache TTL | 不存在 | ≥3 个 scan 周期（当前默认 180 秒） | 避免重复付费/延迟 |
| F-4 | `breakout_threshold_rel` | 不存在 | 0.25 | 不同价位使用对称相对阈值 |
| F-4 | scale-out targets/fractions | 30/60；0.33/0.50 | 25/45；0.33/0.33 | 统一为实际 pct 语义 |
| F-4 | scale-out trailing | 120 bps（实际 1.2%） | 1200 bps（实际 12%） | 与 pct 出场族统一 |
| F-4 | `trailing_stop_pct` | 18 | 12 | 与 scale-out remainder trailing 对齐 |
| F-4 | `stall_giveback_fraction` | 不存在 | 0.5 | 用峰值利润回吐判断 stall |
| F-4 | history maxlen（默认 stale=1800） | 240 | ceil(1800/5)=360 | 覆盖完整 stale window |
| F-4 | `target_distance_to_one_fraction` | 0.55 | 0.35 | 降低目标虚高，待回测校准 |
| F-5 | `shock_recent_window_seconds` | 不存在 | 900 | 定义近因窗口 |
| F-5 | `shock_recent_share_min` | 不存在 | 0.6 | 区分集中冲击与慢漂移 |
| F-5 | `shock_min_expected_move` | 0.03 | 0.04 | 与默认 min edge 4% 消除死区 |
| F-5 | `max_risk_score`（default/pipeline） | 0.70 / 0.70 | 0.60 / 0.60 | 让默认风险闸可拒绝 0.61 信号 |

未改变但需明确：F-1 三个 1,000,000 预算只改说明；F-2 `min_confluence_strength=0.50`、`min_tier=low` 保持 fix-03；F-5 `shock_lookback_seconds=900` 保持 fix-02。

## 测试与审计证据

- F-1：`python -m pytest backend/tests/ -k "copy_trade" -q` → `25 passed, 2589 deselected, 1 warning`。
- F-2：最新裁决修正后 `backend/tests/test_traders_firehose_provenance.py` → `15 passed`；`python -m pytest backend/tests/ -k "confluence or firehose" -q` → `47 passed, 2600 deselected, 1 warning`；StrategyLoader compile/load smoke 默认 age=60。
- F-3：直接相关 22 tests passed；`python -m pytest backend/tests/ -k "news_edge or news_workflow" -q` → `29 passed, 2602 deselected, 1 warning`。
- F-4：直接/相邻 33 tests passed；`python -m pytest backend/tests/ -k "news_momentum" -q` → `13 passed, 2628 deselected, 1 warning`。
- F-5：专项与 `python -m pytest backend/tests/ -k "certainty" -q` 均 `9 passed, 2638 deselected, 1 warning`。
- 最终合并回归：`python -m pytest backend/tests/ -k "copy_trade or confluence or firehose or news_edge or news_workflow or news_momentum or certainty" -q` → `123 passed, 2524 deselected, 1 warning in 15.16s`。
- warning 均为既有 `backend/services/scanner.py:60`：`DeprecationWarning: There is no current event loop`，不在本批范围。
- `python -m compileall -q`：全部触碰的 strategy/news/service/test 文件通过。
- `git diff 7c51d161..HEAD --check`：通过。
- 新增 config key 全部引用≥2；F-4 全 32 个 default key、F-5 全 23 个 default key 引用≥2。
- production legacy fee audit：`polymarket_taker_fee_legacy_quartic(` 除 `utils/kelly.py` 历史 helper 定义和专门历史回归测试外无调用。
- 精确残留 audit：生产代码无 `edge_midpoint/edge_multiplier` copy key、无 `self._confluence_strength`、旧 `edge_detector.py` 无 `structured_output/PROBABILITY_SCHEMA/_estimate_edge`、firehose pipeline 无 normalize/source_flags=False 回退。

## 未验证项与部署提醒

- 未跑全部 `backend/tests/` 无筛选套件；本批全部直接/相邻纯逻辑测试已跑。未涉及数据库 schema，相关测试无需 PostgreSQL。
- 未连接真实 LLM、Polymarket CLOB 或真实订单；LLM cache、live-price refresh、费用/edge 闸使用 stub/纯逻辑验证。
- F-3 score、F-4 relative/target/exit、F-5 recent-share/risk 新默认尚未做历史回测或 shadow A/B 标定，代码已明确标注需校准。
- `wallet_intelligence` 的 tier 分档未越界修改；medium 权重已按最新权威 spec 从策略配置删除，未来若要新增 medium 必须独立决策并做 shadow 对照。
- 本仓库这些文件是 seed template；合并/部署后必须通过 Strategy Editor/API reset-to-factory 同步 DB `strategies.source_code/config/schema`，否则运行库仍可能使用旧源码/配置。
