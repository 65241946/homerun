# Wallet Consensus Alpha V1 研究发现

## Requirements

- 基座只承担公共数据获取、标准化、聚合、持久化和消息分发。
- 钱包共识策略在独立策略层实现，并拥有独立下单能力。
- 新闻、天气、加密、体育保留原策略与独立下单能力，本次不修改其内部逻辑。
- 新旧策略需可快速切换，模块间订单、持仓、风险预算和收益记录不得串账。
- 所有收益证据必须区分回测、shadow、canary live 和可核验实盘。
- 不能靠取消安全门禁制造订单；需要定位零订单发生在哪一层。

## Confirmed Local Facts

- HOMERUN 已有市场分类、钱包发现、钱包活动汇总、实体聚类、cluster-adjusted 共识信号、策略运行和 shadow 账本基础。
- 当前 Wallet Consensus Baseline 已运行但没有订单；历史检查显示大量决策被 spread、价格来源、edge、漂移等门禁拦截。
- 当前策略源码存在将缺失或非正 edge 合成启发式 edge 的路径；该数值不能当作已证明的可交易收益。
- 当前延迟基线记录在 `../wallet-consensus/2026-08-10-latency-baseline.md`。

## External Evidence Captured So Far

- `BallesJr/polymarket-copy-trader`：有 leader/copy price、延迟、盘口深度、skip 与 paper PnL，但没有排队、冲击和逆向选择模型，且固定提交无 LICENSE。
- `edulabrador/polymarket-smart-money`：有 category specialist、leave-one-out 和来源自动停用思路；本地 32 项测试通过，但提交回测样本与表现不足以证明收益，且其分类不覆盖天气。
- `pselamy/polymarket-insider-tracker`：MIT，资金链和 DBSCAN 聚类适合实体去重；不提供收益策略证明。
- `evan-kolberg/prediction-market-backtesting`：严格 L2 重放显示公开成交无法被跟随者完整复现，适合作为执行真实性门禁参考。
- `cbaezp/polycopier`：MIT，独立模拟账本、去重、过期和风险控制可作执行层参考；不证明钱包选择有效。
- 名称为 sports copy bot 的候选源码并无体育专长筛选，不能因仓库名称直接采用。

## Industrial Architecture Source Audit (2026-08-10)

### Mature open-source / official live-operation patterns

- **NautilusTrader（官方架构与实盘文档）**：真实组件边界为 `DataEngine`、`RiskEngine`、`ExecutionEngine`、`MessageBus`、venue adapters；Backtest/Sandbox/Live 复用同一 Strategy，实盘执行要求订单、成交和持仓 reconciliation。可支撑 HOMERUN 继续复用统一执行/风险/对账，而不是让钱包策略直接调用 CLOB。
- **Hummingbot Strategy V2（官方文档）**：Controller 读取 `MarketDataProvider` 并产生 `ExecutorAction`，Executor 自主管理下单、刷新、撤单和结束生命周期；多个 Controller 可在同一实例独立运行。可支撑“策略决策与执行生命周期分离”，但不要求 HOMERUN照抄其类名或新建 Executor。
- **QuantConnect LEAN Algorithm Framework（官方文档）**：Universe → Alpha/Insight → Portfolio Construction/Target → Risk → Execution 是正式可插拔边界；官方明确各模块应保持职责隔离。可支撑钱包策略只产生信号/评分，仓位和账户风险仍由既有统一层复核。
- **Freqtrade（官方文档）**：同一策略经过 backtest → dry-run/forward test → live；官方明确回测会假设成交并可能产生 lookahead，提供 `lookahead-analysis` 和 `recursive-analysis`。可支撑 HOMERUN 的 Shadow → Canary Live 门禁和偏差检查，但不能证明任何策略盈利。

### Real HOMERUN interface mapping

- 策略扩展点是 `backend/services/strategies/base.py::BaseStrategy`，不是拟议的 `prepare/evaluate/build_order_intent` 新接口。
- 钱包策略已有真实方法族：`prepare_firehose_signals`、`apply_firehose_filters`、`build_opportunities_from_firehose`、`custom_checks`、`compute_score`、`compute_size`、`should_exit`。
- 真实发布链为 `Opportunity` → `bridge_opportunities_to_signals()` → intent runtime / `TradeSignal` → `TraderDecision` → `TraderOrder` → `TraderPosition`。
- 策略/运行隔离已有 `Strategy`、`StrategyVersion`、`source_key`、`strategy_type`、`Trader.source_configs_json`、`trader_id` 和每 Trader 风险配置。
- 因此首版草案中的 `WalletTradeFactV1`、`EntitySnapshotV1`、`ExecutableQuoteV1`、`OrderIntentV1`、`module_key` 不能作为既有事实，也无必要建立平行体系；正式稿改为复用现有模型，并把确需新增的数据库字段单独标成“拟议迁移”。

### Existing wallet strategies and live routing (code verified)

- HOMERUN 已有单钱包跟随策略 `backend/services/strategies/traders_copy_trade.py::TradersCopyTradeStrategy`，数据服务 `traders_copy_trade_signal_service.py` 监听 `wallet_ws_monitor`、按 Trader 配置解析 tracked/pool/individual/group 钱包范围、去重并发布到现有信号桥。
- `traders_copy_trade` 不是共识收益策略：当前 `edge_percent` 由 `abs(0.5 - entry_price) * 200` 计算，这是价格离 0.5 的距离，不是 follower 在可执行价下的预期收益。正式方案不能把它当收益证据。
- 多钱包共识策略 `traders_confluence` 已订阅 `trader_activity`；`tracked_traders_worker` 将同一 `DataEvent` 交给 `event_dispatcher`，策略加载器按 DB 策略的 `subscriptions` 自动注册 handler，因此新增共识策略可以复用现有事件通道并与旧策略并行。
- `traders_firehose_pipeline.py` 的 API/展示过滤路径当前硬编码 `traders_confluence`，但 live worker 的事件分发不是该硬编码路径。正式规格必须区分“UI/API 展示查询”与“实时策略 dispatch”，避免误把展示层限制当成执行层限制。
- `intent_runtime` 已支持 `strategy_type` 过滤、`intended_trader_id` 隔离和 per-trader dedupe；`Trader.source_configs_json` 已能固定 `source_key/strategy_key/strategy_version/strategy_params`。不需要新增 `module_key`。

### Evidence boundary

- 上述框架的官方 Live 文档只能证明其架构经过真实交易运行场景设计，不能证明钱包共识策略有正收益。
- 钱包共识收益仍需由本机 point-in-time 数据、可执行盘口、Shadow 成交、Canary 实际订单与可核验 PnL 逐级证明。

### Additional verified source facts

- NautilusTrader 当前官方仓库不仅是通用框架，还已有 Polymarket CLOB data/execution adapter；官方集成文档列出 `PolymarketInstrumentProvider`、`PolymarketDataClient`、`PolymarketExecutionClient` 和维护中的 data/exec smoke tester。这个来源可用于审查 HOMERUN 的 Polymarket adapter 与 reconciliation 设计，但本轮不引入第二套执行引擎。
- NautilusTrader 官方 Live 文档要求策略启动前准备 instrument/execution state，并在启动和运行期将订单、成交、持仓与 venue reports 对账；这为 HOMERUN 实盘前“先对账、后启策略”的门禁提供直接来源。
- Hummingbot V2 官方文档明确 Controller 与 Executor 分工：Controller 是长期策略逻辑，Executor 管理有限订单生命周期；HOMERUN 对应复用现有 Strategy/TraderOrchestrator，不照搬类名。
- LEAN 官方文档明确 Alpha 只输出 Insight，组合构建、风险、执行依次消费前一层输出；这支持钱包策略不绕过账户风险层。
- Freqtrade 官方文档明确 dry-run 使用实时交易所数据但不在交易所开单；回测默认全部成交且计算延迟会改变价格。这个边界与 HOMERUN Shadow/Canary 的证据分级一致。

### Empirical source verification

- `The Return to Imitation: Evidence from Copy Trading on Polymarket` 已核验为 2026-04-28 发布的 SSRN working paper；其摘要报告同一 follower、同一 market 内，识别到的 copy trades 相比非 copy trades 高 1.62 个百分点。它支持研究假设，不证明 HOMERUN 当前策略可盈利。
- `Who Profits from Prediction Markets? Execution, not Information` 已核验为 2026 SSRN working paper；摘要基于 resolved Polymarket trades，将方向选择与成交价格分开，结论强调执行质量。它直接支持“必须用 follower 可执行价评估”，但仍是工作论文而非生产收益担保。
- Yale SOM 对 `Wisdom of the Few` 工作论文的官方研究介绍已核验：作者使用随机化 skill screen 区分技能与运气，并做样本拆分稳定性检查。它支持钱包筛选必须包含随机基准和 OOS 稳定性，而不是只按累计 PnL 排名。
- 上述均归类为“实证研究/工作论文”，不能替代 live fills、fees、slippage 和 realized PnL。

### Weather and venue-contract verification

- `Four Strategies, 562 Trades, Zero Edge` 已核验为 SSRN working paper；摘要明确报告 509 笔 live 天气交易总体亏损，而 paper 配置显示盈利。它是“回测/模拟不等于实盘”的反例来源，不应被表述成同行评审定论。
- `Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets` 已核验为 2026 arXiv 预印本；可支持按市场域/期限分层校准的研究假设，不支持具体交易阈值。
- `alteregoeth-ai/weatherbot` 仓库真实存在且为 MIT；可借鉴机场站点映射、多气象源、simulation 日志。其 README 的 mispricing/收益陈述仍是项目声明，不作为盈利证据；公开 issue 还出现余额/PnL 追踪问题，应保持审慎。
- Polymarket 官方 2026-08-10 当前 changelog 已核验：CLOB V2 于 2026-04-28 上线且不兼容 V1；当前还包含 keyset 分页、feeSchedule、异步成交回执等后续变更。接口审计必须以当前官方契约为准，旧开源项目只参考算法。

## Resources

- https://github.com/BallesJr/polymarket-copy-trader
- https://github.com/edulabrador/polymarket-smart-money
- https://github.com/pselamy/polymarket-insider-tracker
- https://github.com/evan-kolberg/prediction-market-backtesting
- https://github.com/cbaezp/polycopier
- https://github.com/Polymarket/polymarket-cli
- https://github.com/Polymarket/py-clob-client

## Open Questions

- 市场缓存中的规则与 `end_at` 对钱包候选的实际覆盖率，需要在数据 Smoke 阶段统计，不能预设完整。
- 钱包质量的首轮 OOS 校准模型和收缩强度，需要在 point-in-time 样本导出后预注册。
- 各类别达到多少独立实体和可执行 Shadow 样本才单独晋级，需要按实际信号密度评审。

已确定：首个验收终点是可执行 Shadow；达到数据、样本、成本和集中度门槛后才进入人工批准的 1--5 美元 Canary。退出方案先在 Shadow 中并行记录，不能混成一个 PnL。

## Current Runtime Evidence (2026-08-10 18:26 Asia/Shanghai)

- Wallet Consensus Baseline: shadow, enabled, 60-second interval.
- 1,352 decisions from 30 distinct signals; 0 approved and 0 orders.
- Block/skip buckets: spread 1,019; strategy edge 255; strict WS pricing missing 59; channel threshold 14; edge plus channel threshold 5.
- A verified negative-copy example moved from wallet reference price about 0.3504 to executable ask 0.755, producing live edge -34.53%; rejecting it was correct.
- Active confluence rows did not persist a category field, and emitted trader signal payloads currently have null category.
- Current entity clustering is heuristic and bounded: at most 200 wallets, at most 200 trades per wallet, with stored cluster confidence fixed at 0.6.
- The current strategy synthesizes edge when upstream edge is missing; Alpha V1 must not reuse that value as expected profit.

## Recommended Architecture

- Keep existing `traders_confluence` as control.
- Add a new DB-loaded strategy `wallet_consensus_alpha_v1` under the existing `traders` transport source.
- Reuse the existing `WalletTradeEvent/WalletActivityRollup/MarketConfluenceSignal` chain; only enrich firehose rows from the existing `CachedMarket` payload with sourced category and token/outcome metadata.
- Reuse `WalletCluster.confidence/detection_method/evidence` as uncertain entity evidence; do not introduce a new entity-snapshot contract in Alpha V1.
- Reuse `Opportunity -> TradeSignal -> TraderDecision -> TraderOrder -> TraderPosition`; do not add parallel fact, quote, intent or module interfaces.
- Keep signal/edge/filter/entry/exit/sizing inside the new strategy.
- Use a separate Shadow Trader and existing `strategy_type/strategy_key/version/trader_id` ledger attribution; retain account-level global safety arbitration.

## Source Type Policy

- `[L]` local HOMERUN code/runtime establishes what exists.
- `[O]` official open-source framework and exchange documentation establishes industrial implementation patterns and current contracts.
- `[E]` papers and reproducible datasets establish hypotheses and negative evidence.
- `[C]` community repositories and social posts are discovery leads only.
- External live-capable frameworks prove deployability patterns, not strategy profitability; only HOMERUN Shadow plus verifiable Canary fills can establish this system's PnL.

Full draft: `2026-08-10-wallet-consensus-alpha-v1-design.md`.

## 2026-08-11 source revalidation

- Polymarket 当前官方 Data API `/trades` 明确返回 `proxyWallet`、`side`、`asset`、`conditionId`、`outcome`、`outcomeIndex`、`timestamp` 和 `transactionHash`。因此钱包底层事实必须保存 token/outcome 身份，不能只保存 BUY/SELL。
- 当前官方 WebSocket 文档区分 `price_change`（挂单新增/撤销导致的价位变化）与 `last_trade_price`（真实成交）。钱包成交方向不得由 `price_change.side` 推断。
- `Polymarket/py-clob-client` 已于 2026-05-25 归档；当前官方 Python 参考应转向 `Polymarket/py-sdk`/当前官方 SDK 文档。旧客户端只能作为历史实现参考，不能作为新适配层基线。
- `Jon-Becker/prediction-market-analysis` 当前为 MIT，真实包含 Polymarket/Kalshi 市场、API/链上成交采集、Parquet 存储和测试；适合数据研究/离线钱包画像，不含 L2 可执行价格或收益策略。
- `evan-kolberg/prediction-market-backtesting` 当前仍明确为 active development，真实包含 L2 book replay、trade-tick fill evidence、费用/fill 模型和账户重放；适合作为执行真实性基准，不等于策略盈利证明。
- `pmxt-dev/pmxt` 当前为 MIT，真实提供跨预测市场统一数据/交易 SDK 和自托管路径；对 HOMERUN 首版更适合做适配契约和 recorder 参考，不应替换已经存在的执行内核。
- `BallesJr/polymarket-copy-trader` 真实实现了 point-in-time leader/copy price、盘口深度步进、延迟、skip log、paper PnL 和 leader unwind；但固定源码无 LICENSE，且 paper 结果不能晋级为可核验实盘证据。
- `edulabrador/polymarket-smart-money` 真实实现多钱包同向聚合、category specialist、价格漂移和来源 ROI 停用思路；其阈值与收益仍是项目自身研究口径，不能直接写入 HOMERUN 生产默认值。
- `pselamy/polymarket-insider-tracker` 为 MIT，真实包含实时成交、链上资金来源、异常规模和 DBSCAN/实体线索；适合生成“独立实体证据”，不提供可复制 PnL 证明。

### Evidence rule retained

社区项目最多分别贡献数据采集、实体证据、共识计算、可复制性回放或执行对账之一。只有同一 HOMERUN 策略版本下的 point-in-time Shadow/Canary 订单、实际可执行价格、费用、滑点和结算账本，才能证明本系统的收益。

## 2026-08-11 HOMERUN 与 GitHub 最终交叉审计

- 当前 HOMERUN 工作树不是可复现基线：HEAD 为 `4594fe4c`，分支比远端超前 8 个提交，且结果身份迁移、市场身份、模拟现金账本等关键文件尚未纳入 Git。实施前必须冻结 commit、数据库 migration/version、DB 策略版本和镜像 digest。
- HEAD 的旧共识方向曾把 BUY/SELL 直接映射为 YES/NO；当前工作树虽有四象限修复，但 rollup 去重仍未纳入 token/outcome，`MarketConfluenceSignal` 仍缺可执行 token identity。它们属于数据事实 P0，不是“放宽策略阈值”能解决的问题。
- 当前实体聚类主要依据共享市场、时间、ROI/胜率和策略相似度；没有证据证明它等价于共同资金/控制实体。行为聚类只能作为降权证据，不能硬合并。
- 当前共识策略在上游无 edge 时合成 edge；该值只能解释为共识强度，不能作为期望收益或 PnL 证明。
- 当前官方 Python 基线是 `Polymarket/py-sdk v0.5.0 @ 974d2e2`；旧 `py-clob-client` 已归档。所有仍依赖旧客户端或旧 RTDS 钱包活动流的社区项目，执行接入均判定为不可直接采用。
- 最终采用组合：官方 SDK/合约负责当前契约；`prediction-market-analysis` 负责历史/链上事实；`insider-tracker` 只贡献实体证据思路；`smart-money` 只贡献同 token 多钱包共识假设；`prediction-market-backtesting` 独立验证 L2/延迟/费用/部分成交；NautilusTrader 只作 V2 执行和对账模式参考。
- 未发现任何一个候选仓库同时具备当前 V2 兼容、LICENSE、固定版本、逐笔可核验 live fills、费用滑点和独立审计净收益。公开项目的收益结论均不得继承到 HOMERUN。
