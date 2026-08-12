# HOMERUN 钱包共识 Alpha V1：收益机制与模块化设计草案

日期：2026-08-10
状态：待用户评审，未进入实现
审计对象：`homerun-pmr` 当前本地工作树。当前工作树包含未提交修改和未跟踪迁移，**尚不能称为固定、可复现的交易基线**；进入实现前必须先冻结 commit、迁移版本、镜像 digest 和数据库策略版本。

## 1. 结论先行

本轮不应通过简单放宽 `max_spread_bps`、降低置信度或取消严格报价门禁来“制造订单”。当前零订单由三类原因共同造成：

1. **正确的不可复制拦截**：钱包成交后，价格已经显著漂移，或者当前盘口价差/深度不足；此时跟随会把原本可能存在的优势交给被跟随者和更快的复制者。
2. **底层数据事实不完整**：部分信号没有严格 WebSocket 报价；共识事实没有稳定携带市场类别、规则、到期时间和可执行盘口；实体聚类仍是有限样本启发式结果。
3. **现有策略不是收益估计器**：当前 `traders_confluence` 在上游没有正 edge 时会合成一个启发式 edge。这个数可以用于排序，不能证明实盘期望收益；策略也没有以“我们在实际延迟后的可成交价”为中心做钱包可复制性校准。

推荐方案是：**保留现有 Wallet Consensus Baseline 作为控制组，复用 HOMERUN 已有 `DataEvent -> BaseStrategy -> Opportunity -> TradeSignal -> TraderDecision -> TraderOrder -> TraderPosition` 链路，新增独立的 `wallet_consensus_alpha_v1` DB 策略插件；底层只补齐现有钱包活动与市场缓存之间缺失的类别、结果 token 和时点字段，不修改新闻、天气、加密货币、体育的原策略代码。**

本方案没有找到一个可直接复制、且能以“当前 Polymarket V2 + 可审计逐笔实盘 + 扣除费用和滑点后的持续净收益”自证的钱包共识仓库。因此外部项目只按其源码中真实存在的组件分别采用；盈利结论不从 README、截图或作者回测继承。

首轮验收采用既定顺序：实时市场数据下的可执行 Shadow → 人工批准的 1--5 美元 Canary Live → 再决定是否放量。Shadow 只证明链路与候选收益，不冒充实盘收益。

### 1.1 证据标记与采用规则

本文后续架构决策按以下来源类型登记，避免把项目宣传、论文结论和本地实现混为一谈：

| 标记 | 来源类型 | 本文用途 | 边界 |
|---|---|---|---|
| `[L]` | HOMERUN 当前本地源码、数据库模型、运行日志和实测 | 确认真实接口、调用链、字段和当前问题 | 只代表当前固定源码与本机运行态 |
| `[O]` | 官方开源仓库、官方架构文档、官方交易所/API 文档 | 参考已在工业量化系统采用的分层、事件和执行模式 | 证明工程可落地，不证明某策略盈利 |
| `[E]` | 可追溯论文、链上数据集、可复现实证研究 | 建立和否定策略假设，设计样本外验证 | 研究结论不能直接外推为本账户收益 |
| `[C]` | 社区仓库、X/Discord/博客、产品案例 | 发现候选思路、实现细节和待核验钱包 | 未独立复算前不得进入收益证据 |

采用顺序是：先以 `[L]` 确认 HOMERUN 是否已有同类能力，再用 `[O]` 检查架构是否符合成熟实盘系统做法，最后用 `[E]` 约束策略假设；`[C]` 只进入待验证登记表。本文不创建仅为“看起来完整”而存在的新协议或接口。

## 2. 本次范围

### 2.1 允许修改

- 钱包原始成交/活动采集；
- 交易方向、结果 token、市场标识、类别、规则和时间字段标准化；
- 钱包活动汇总、point-in-time 统计、现有实体聚类证据与质量标记；
- 实时盘口快照、数据质量标记和事件分发；
- 新增独立的钱包共识策略和独立 Trader 配置，沿用现有账本中的 `strategy_type`、`strategy_key/version`、`trader_id` 做归因；
- 为新策略接入现有统一执行与风险控制。

### 2.2 本次禁止修改

- 新闻模块原有策略逻辑；
- 天气模块原有策略逻辑；
- 加密货币模块原有策略逻辑；
- 体育模块原有策略逻辑；
- 为了增加订单而关闭严格报价、全局风险、去重、自成交、持仓和资金上限；
- 让策略插件直接接触私钥或绕过 HOMERUN 执行层调用 CLOB。

新闻在本设计中是一个完整业务模块，不再拆分内部新闻类型。

## 3. 当前运行事实

### 3.1 运行快照

2026-08-10 18:26（Asia/Shanghai）只读查询结果：

| 指标 | 结果 |
|---|---:|
| 钱包交易器 | `Wallet Consensus Baseline 2026-08-10` |
| 模式 | `shadow` |
| 运行周期 | 60 秒 |
| 累计决策 | 1,352 |
| 独立信号 | 30 |
| approved | 0 |
| skipped | 1,293 |
| blocked | 59 |
| 订单 | 0 |

决策原因：

| 原因桶 | 次数 | 判断 |
|---|---:|---|
| 当前盘口价差超过 75 bps | 1,019 | 一部分是正确拦截；静态阈值不适合表达所有市场的可复制成本 |
| 实时 edge 不足 | 255 | 多数是正确拦截，说明钱包成交价不等于我们的成交价 |
| 严格 WS 报价缺失 | 59 | 底层行情覆盖/映射问题，应在数据层修复 |
| channel threshold | 14 | 策略评分/通道阈值问题，需要用收益证据重新校准 |
| edge + channel threshold | 5 | 同上 |

一个已核验样本中，钱包原始参考价约为 `0.3504`，本机决策时严格 WS 可买价已变为 `0.755`，实时 edge 为 `-34.53%`。这种订单不应通过。

### 3.2 当前代码已经具备的基础

- `Strategy`、`StrategyVersion` 和运行时 revision：支持策略源码、配置、版本和热加载；
- `source_key` 分桶：当前已有 `scanner/news/crypto/weather/traders/sports/manual` 等路由；
- discovery plane：钱包发现、活动汇总、共识计算；
- trading plane：统一信号消费、决策、订单、持仓、对账和风险门禁；
- `cluster_adjusted_wallet_count`：已有实体调整后的钱包数量；
- Shadow 决策、订单和持仓账本；
- 跨进程 `trade_signals` 桥：discovery 不需要和交易热路径共用事件循环。

因此应沿用 HOMERUN 的策略注册、交易编排、风险和账本，不另起一套交易系统。

### 3.3 当前源码可复现性门禁

2026-08-11 只读审计时，当前分支 `main` 位于 `4594fe4cead3947157c273598766d5c6a313238f`，相对 `origin/main` 超前 8 个提交，工作树另有大量未提交/未跟踪交易代码。结果身份迁移 `202608100001_wallet_rollup_outcome_identity.py`、`market_identity.py` 等关键文件仍未纳入 Git。

这意味着当前运行态可能包含三种不同事实：HEAD、工作树源码和数据库已应用迁移。进入 Alpha V1 实施前必须先生成只读清单并冻结三者；否则任何 Shadow PnL 都无法准确归因到一套可重放版本。本设计阶段不整理、提交或回退这些既有修改。

## 4. 当前零订单的根因矩阵

| 层级 | 已确认事实 | 性质 | 优化方向 | 修改边界 |
|---|---|---|---|---|
| 钱包事实 | 共识基于最近 15/60 分钟活动汇总 | 正常但信息不足 | 保存不可变成交事实、首次发现时间、增量方向和来源版本 | 基座数据层 |
| 结果身份 | HEAD 曾按 `BUY->YES / SELL->NO` 聚合；当前工作树虽有四象限修复，但迁移尚未跟踪 | P0，可能把 `BUY NO`、`SELL YES` 算反 | 方向由 action、token、outcome_index 和持仓变化共同确认；未确认记录隔离 | 基座数据层 |
| 活动幂等 | 当前 rollup 标识未把 token/outcome 纳入键 | P0，不同结果腿可能碰撞 | 去重身份包含 transaction/log/order 与 token/outcome；初步事件和链上确认必须归并 | 基座数据层 |
| 共识执行身份 | `MarketConfluenceSignal` 未持久化完整 token identity，cache miss 时无法可靠补齐 | P0，存在“有信号但下单层拒绝”链路 | 在现有共识交接对象中保存 condition、selected token、outcome index，不在下单瞬间猜测 | 基座数据层 |
| 市场类别 | `market_confluence_signals` 没有类别列，当前 trader 信号 payload 类别为空 | 数据缺口 | 从 Gamma/市场缓存补充规范类别、原始标签、事件和规则 | 基座数据层 |
| 实体聚类 | 当前主要依据共享市场、时间、ROI/胜率和策略相似度；不是链上共同资金簇 | 启发式，不是实体真相 | 版本化聚类快照，保存证据、置信度、算法版本；行为相似只降权，强合并需资金/控制证据 | 基座数据层 |
| 钱包质量 | 当前主要使用全局 PnL、胜率、ROI、活动等汇总 | 会混入类别差异和幸存者偏差 | 只使用 point-in-time、类别/价格桶/持有期分层、收缩后的 OOS 指标 | 基座数据层产事实，策略层使用 |
| 共识定义 | 同一窗口内按 `(market_id, outcome)` 聚合；同一状态会被重复评估 | 会制造重复决策，不能识别“新共识” | 发布共识状态版本和 delta，只对新成交、实体加入、方向翻转等状态变化触发 | 数据层发布事实 |
| 双向冲突 | 当前存在同一市场 YES/NO 同时活跃的信号 | 原始地址投票不可靠 | 计算 signed delta、冲突比、实体 HHI、买卖/对冲行为；冲突高时不交易 | 策略层 |
| 收益估计 | `traders_confluence.py` 在 edge 缺失时按阈值/置信度合成 edge | 策略缺陷 | Alpha V1 禁止合成 edge；使用延迟后可执行价格和 OOS 条件收益估计 | 独立策略层 |
| 行情覆盖 | 59 次严格 WS 来源未知 | 基础设施问题 | 在下单决策前建立 token 订阅、验证 token/outcome 映射和 quote age | 基座行情层 |
| 交易成本 | 1,019 次被固定 75 bps 拦截 | 固定阈值过于粗糙，但不能直接放宽 | 以实际下单规模计算 ask VWAP、手续费、滑点、退出成本和不确定性缓冲 | 策略判断 + 统一执行复核 |
| 延迟 | 当前钱包链路按 30/60/120 秒评估，不是毫秒系统 | 能做秒级/分钟级，不适合超短延迟 | 按类别和市场期限设 copyability profile；体育滚球、5 分钟币价等默认禁止 | 策略层 |
| RTDS 初步事件 | 未确认事件可能早于链上确认，但不具备最终身份和可审计完整性 | 只能作 preliminary；存在重复/误向风险 | 只作低置信早期提示，必须与 V2 链上/官方活动事实归并后才能进入正式共识 | 基座数据层 |
| 退出 | 现策略主要是固定止盈/止损，没有把 leader 卖出、共识反转作为第一类事实 | 收益链不完整 | Shadow 并行比较 leader-exit、consensus-reversal、hold-to-resolution | 独立策略层 |
| 版本/API | Polymarket 2026-04-28 切换 CLOB V2，V1 不向后兼容 | 实盘前硬阻断项 | 只使用/核验 V2 SDK 和签名结构；旧项目只借鉴算法 | 基座执行层，实盘前审计 |

## 5. 可获取高质量策略的渠道

### 5.1 证据优先级

| 等级 | 渠道 | 主要用途 | 能否直接证明收益 |
|---|---|---|---|
| A | 官方 API、链上成交、CLOB L2、结算规则、官方 changelog | 构造事实、成交和接口兼容性 | 不能单独证明策略，但是真实性基础 |
| A-/B+ | 有方法和样本说明的论文、可重复数据与 OOS 研究 | 形成可检验假设、避免常见偏差 | 只能支持机制，仍需本机实盘验证 |
| B | 有 LICENSE、固定 commit、测试、数据样例的开源项目 | 复用数据/回测/执行部件 | 不因开源或测试通过而证明盈利 |
| B-/C | Dune/链上查询、Hugging Face/Kaggle/PMXT 等数据集 | 独立复算和补历史盘口 | 数据质量需逐项审计 |
| C | Discord、X、Reddit、Telegram、博客、产品报告 | 发现新思路、钱包和接口变化 | 只作为线索，不作为收益证据 |

### 5.2 当前值得保留的来源

| 来源 | 可借鉴内容 | 采用方式 | 限制 |
|---|---|---|---|
| `[O]` Polymarket 官方 API 文档与 changelog | Gamma/Data/CLOB 职责、V2 订单和接口变化 | 作为接口契约和兼容性基准 | 不提供 alpha |
| `[O]` `Polymarket/py-sdk` `v0.5.0` @ `974d2e2` + CLOB V2 文档/合约 | 当前 Data/Activity/Position/订单、费用和链上核对契约 | 固定 tag+SHA 后通过 HOMERUN adapter 使用 | 仍是 0.x；旧 `py-clob-client` 已归档，不能作为 2026 新适配基线 |
| `[O]` NautilusTrader Polymarket adapter | V2 EIP-712、pUSD、FAK/FOK、订单状态和 reconciliation 的生产级参考 | 只审查 HOMERUN 执行/对账边界，不替换现有引擎 | LGPL，大型框架；不提供 wallet alpha |
| `[E]` `Jon-Becker/prediction-market-analysis` @ `2276382` | 大规模市场/链上成交数据、Parquet schema、断点续传 | 历史钱包、point-in-time 研究和链上审计 | 缺完整历史 L2 排队状态，不负责下单 |
| `[C]` `pmxt-dev/pmxt` @ `4a367d8` | 跨预测市场统一 trades/positions/activity/orderbook 与地址监听 | 可选的自托管只读适配/契约测试，不替换 HOMERUN 现有执行层 | 无 alpha；hosted 模式不承载本项目私钥 |
| `[E]` PMXT archive | 历史 L2/订单簿快照 | 延迟与深度重放 | 需验证缺口、时间同步和 V1/V2 边界 |
| `[C]` `evan-kolberg/prediction-market-backtesting` @ `c76e77a` | 队列、延迟、滑点、部分成交的回测框架 | 独立验证工具，不嵌入生产执行 | 它证明公开成交不可照搬，不证明某策略盈利 |
| `[C]` `pselamy/polymarket-insider-tracker` @ `b54dbd3` | DBSCAN、资金链、实体注册和异常特征 | 借鉴实体证据字段/算法，MIT | ingest 依赖已归档 `py-clob-client`，必须改用 V2；不提供收益证明 |
| `[C]` `runesleo/polymarket-toolkit` | 钱包现金流重建、被动成交、窗口 VWAP markout、V2 检查 | 借鉴统计口径并独立复算 | 未发现独立审计的策略净收益 |
| `[C]` `leolopez007/polymarket-trade-tracker` | maker/taker 与 split/merge/redeem 跟踪 | 借鉴现金流核对 | 不是钱包共识盈利策略 |
| `[C]` `cbaezp/polycopier` @ `2545bdf` | 去重、TTL、模拟账本、复制风控 | 借鉴执行边界，MIT | 无盈利证据；本机无 Rust 工具链未跑测试 |
| `[C]` `BallesJr/polymarket-copy-trader` @ `c8f0b24` | leader/copy 价、延迟、深度、负控和 paper 记录 | 只借鉴研究方法 | 无 LICENSE；263 笔关闭 paper 仅约 +0.56%，未建模排队/冲击 |
| `[C]` `edulabrador/polymarket-smart-money` @ `920eec0` | `(conditionId, outcomeIndex)` 多钱包同向共识、类别专长、price drift、来源 ROI 停用 | 只重新实现经验证的概念，不复制源码 | 无 LICENSE；自记共识样本仅 14 个且没有真实成交/费用/滑点，不能证明实盘收益 |
| `[E]` SSRN/arXiv/NBER 等论文 | 技能、执行质量、价格校准、复制收益 | 形成预注册假设与负控 | 多为工作论文，不能当生产承诺 |

### 5.3 论文给出的重要方向

- 《The Return to Imitation》报告：识别到的复制交易，相对同一跟随者在同一市场的非复制交易平均高 1.62 个百分点。它支持“复制可能有效”，但没有替代我们的延迟、成交和选人验证。
- 《Who Profits from Prediction Markets? Execution, not Information》强调预测方向和执行质量近乎独立；方向更准仍可能因买得太贵而亏损。这正是 Alpha V1 必须优先建模 `copyability` 的原因。
- 《Who Wins and Who Loses In Prediction Markets?》显示利润高度集中，成功交易者更多使用限价流动性。排行榜 PnL 不能直接等同于“适合用 taker 跟单”。
- 《The Wisdom of the Few》指出少数经过 skill screen 的交易者有永久价格影响；不能把所有高活跃 taker 或所谓 whale 混在一起。
- 域校准研究表明政治、体育、加密等类别的价格偏差轨迹不同，因此钱包能力必须按类别和期限分层，不能用一个全局胜率覆盖全部市场。

### 5.4 社区使用规则

从 X、Discord、Reddit、Telegram 或付费产品发现策略后，只做以下三件事：

1. 登记原始链接、时间、作者、声称数据和可核验钱包；
2. 从官方 API/链上/盘口独立复算；
3. 未通过 point-in-time 和可执行价格复验前，状态只能是 `idea`，不能进入 live。

截图、胜率、单个钱包 PnL、年化外推和“AI 自动赚钱”不进入收益证据栏。

## 6. 架构方案比较

### 6.1 工业开源架构证据与 HOMERUN 映射

以下只采用项目官方仓库或官方文档描述的真实组件，不根据名称自行推导接口：

| 设计决策 | 参考来源类型 | 外部真实实现 | HOMERUN 当前真实对象 | 本轮结论 |
|---|---|---|---|---|
| 事件驱动的数据、风险、执行分层 | `[O]` 官方开源框架/架构文档 | NautilusTrader `MessageBus`、`DataEngine`、`RiskEngine`、`ExecutionEngine` | `event_dispatcher`、`intent_runtime`、`TraderOrchestrator`、订单/持仓/对账模型 | 复用现有链路，不另建事件总线或执行引擎 |
| 策略判断与订单生命周期分离 | `[O]` 官方开源框架/策略文档 | Hummingbot V2 Controller 产生 Executor actions，Executor 管订单生命周期 | `BaseStrategy` 产生 `Opportunity`，Trader 编排器形成决策和订单 | 策略不得直接签名或调用 CLOB |
| Alpha、风险、执行职责分离 | `[O]` 官方开源框架/算法框架文档 | QuantConnect LEAN Universe/Alpha/Portfolio/Risk/Execution | discovery/firehose、`BaseStrategy`、Trader 风险门、订单管理 | 钱包发现与盈利判断分开，账户风险仍统一 |
| 模拟到实盘使用同一策略路径 | `[O]` 官方开源框架/实盘文档 | NautilusTrader sandbox/live；Freqtrade dry-run/live | Trader `mode=shadow/live` 与同一策略版本、统一账本 | Shadow 与 Canary 不允许两套判断逻辑 |
| 区分事件发生时间和系统接收时间 | `[O]` 官方数据模型文档 | NautilusTrader `ts_event` / `ts_init` | `WalletActivityRollup.traded_at/created_at`；`WalletTradeEvent.timestamp/detected_at/latency_ms` | 复用已有时间字段，不新造时间协议 |
| 订单簿、延迟、部分成交和对账进入验证 | `[O]` 官方回测/执行文档 | NautilusTrader L2/L3、latency model、reconciliation | 现有 token 预热、实时价格刷新、TraderOrder/Position/settlement | 回测成交假设必须和 Shadow/Canary 分开报告 |
| 当前 Polymarket 接口兼容性 | `[O]` 交易所官方文档 + 官方开源适配器文档 | Polymarket CLOB V2；NautilusTrader Polymarket instrument/data/execution clients | HOMERUN `wallet_ws_monitor`、市场缓存和 CLOB 执行服务 | 只做 V2 契约审计，不替换 HOMERUN 引擎 |

这些来源证明的是工程模式已被真实量化框架采用。它们不证明钱包共识策略盈利；盈利证据仍只能来自 HOMERUN 自己的点时数据、可执行 Shadow 和小额实盘成交。

### 6.2 方案 A：直接改现有 `traders_confluence`

- `[L]` 当前 `traders_confluence` 已承载现有控制组，并在缺少真实 edge 时生成启发式 edge。
- 直接修改会让基线与新逻辑无法进行同时间、同市场对照，也会混淆旧订单归因。
- 结论：不采用。

### 6.3 方案 B：直接使用现有 `traders_copy_trade`

- `[L]` 该策略已经支持 tracked/pool/individual/group 范围、延迟、流动性、价格漂移、预算和库存门禁，适合作为“单钱包复制执行链”对照组。
- `[L]` 当前 `edge_percent = abs(0.5 - entry_price) * 200`，它表示价格偏离 0.5 的程度，不是预期收益，也不包含延迟后盘口和成本。
- 结论：保留作执行链和单钱包对照，不把它当作 Wallet Consensus Alpha。

### 6.4 方案 C：复用现有事件链 + 新增 DB 策略插件（推荐）

- 保留 `source_key=traders` 和现有 `TRADER_ACTIVITY` 事件；
- 新增 `wallet_consensus_alpha_v1`，继承真实 `BaseStrategy`，由 DB `Strategy/StrategyVersion` 管理；
- 策略只消费已发布的钱包共识事实并返回现有 `Opportunity`；
- 通过现有 `bridge_opportunities_to_signals()` 进入 `intent_runtime`，不创建新的意图协议；
- 新建独立 Shadow `Trader`，固定策略版本、参数和风险预算；
- 原 `traders_confluence`、`traders_copy_trade` 与新策略可在同一输入期并行，按 `strategy_type` 和 `trader_id` 独立归因。

结论：改动最小，控制组保留，符合 HOMERUN 现有插件和交易账本设计，也符合上述工业开源框架的职责分离原则。

### 6.5 方案 D：另建 sidecar/第二套交易引擎

NautilusTrader、Hummingbot 等确实支持完整独立运行，但当前引入会重复 HOMERUN 已有数据路由、风险、订单、持仓和对账能力，并新增跨进程一致性问题。Alpha V1 不采用；只有经过容量或故障隔离测量，证明现有进程模型成为瓶颈后再单独立项。

## 7. 推荐模块边界

### 7.1 当前真实执行链 `[L]`

```text
WalletTradeEvent / WalletMonitorEvent / WalletActivityRollup
-> MarketConfluenceSignal
-> tracked_traders_worker 构造 DataEvent(TRADER_ACTIVITY)
-> event_dispatcher 调用已订阅策略的 on_event()
-> wallet_consensus_alpha_v1 返回 Opportunity
-> bridge_opportunities_to_signals()
-> intent_runtime / TradeSignal
-> TraderDecision
-> TraderOrder
-> TraderPosition / settlement / reconciliation
```

该链路中的模型和方法都已存在。新策略不得绕过其中任一订单、风险或对账节点，也不新增平行的钱包事实、报价、订单意图或模块接口。

本地代码证据索引：

| 事实 | 当前源码位置 |
|---|---|
| `Opportunity` 与 `stable_id/strategy_context/execution_plan` | `backend/models/opportunity.py` |
| `WalletCluster/MarketConfluenceSignal/WalletActivityRollup` | `backend/models/database.py` |
| `TradeSignal/Trader/TraderDecision/TraderOrder/TraderPosition` | `backend/models/database.py` |
| 策略基类及真实钩子 | `backend/services/strategies/base.py::BaseStrategy` |
| DB 策略装载和订阅注册 | `backend/services/strategy_loader.py` |
| 事件分发 | `backend/services/event_dispatcher.py` |
| Opportunity 到信号桥 | `backend/services/strategy_signal_bridge.py::bridge_opportunities_to_signals` |
| 信号标准化、去重和 Trader 路由 | `backend/services/intent_runtime.py` |
| 多钱包事件生产与市场元数据附着 | `backend/workers/tracked_traders_worker.py` |
| 现有多钱包/单钱包对照策略 | `backend/services/strategies/traders_confluence.py`、`backend/services/strategies/traders_copy_trade.py` |
| 单钱包实时监听与发布 | `backend/services/traders_copy_trade_signal_service.py`、`backend/services/wallet_ws_monitor.py` |
| 当前市场类别与扩展元数据 | `backend/services/market_cache.py::CachedMarket/market_cache_service` |

### 7.2 基座数据层：只补真实缺口

| 现有对象/方法 | 已有事实 | 本轮允许的最小补充 | 明确不做 |
|---|---|---|---|
| `WalletTradeEvent` / `WalletMonitorEvent` | 钱包、token、BUY/SELL、价量、交易/订单/区块标识、发生/发现时间、检测延迟 | 校验幂等键和字段完整率，未知结果身份隔离 | 不从 BUY/SELL 猜 YES/NO |
| `WalletActivityRollup` | `market_id/token_id/outcome/outcome_index`、价量、来源、cluster、`traded_at/created_at` | 按 point-in-time 口径输出增量流和质量标记；缺失字段进入日志 | 不新建重复事实表 |
| `WalletCluster` | confidence、detection_method、evidence、成员聚合数据 | 聚类结果只作为有置信度的证据；低置信结果降权；算法版本若确有需要另做迁移评审 | 不把 cluster 当作确定实体真相 |
| `MarketConfluenceSignal` | 钱包数、cluster 调整数、净/冲突名义、首次/最后发现时间 | 发布状态变化所需的稳定输入，避免相同状态周期性重复 | 不在数据层生成盈利概率 |
| `CachedMarket` / `market_cache_service.get_market()` | condition、category、token_ids、outcomes 及 `extra_data` | 在现有 `_attach_cached_market_execution_metadata()` 中复制已存在的 `category` 与结果 token 元数据；规则/到期字段只在缓存确有来源时透传 | 不虚构 category、规则或到期时间 |
| 现有实时价格/执行刷新 | token 预热、最新价格刷新、Trader 风险和订单状态 | 记录决策时实际买卖侧价格、quote age、深度、费用版本和拒绝原因 | 不建立第二套行情或下单服务 |

基座只负责采集、标准化、聚合、持久化、质量标记和事件分发；不得输出“应该买哪个结果”、期望 edge 或仓位。

### 7.3 独立策略层：使用现有插件接口

新增 DB 策略的可实施边界是：

```python
class WalletConsensusAlphaV1(BaseStrategy):
    strategy_type = "wallet_consensus_alpha_v1"
    source_key = "traders"
    subscriptions = [EventType.TRADER_ACTIVITY]

    async def on_event(self, event: DataEvent) -> list[Opportunity]:
        ...
```

这段只表示 HOMERUN 已有 `BaseStrategy` 的真实接口形态，不是要复制粘贴的最终源码。具体实现继续复用基类已有的 `configure`、`evaluate`、`custom_checks`、`compute_score`、`compute_size` 和 `should_exit` 钩子；只有需要时才覆盖。

策略输出使用现有 `Opportunity` 字段：`strategy`、`edge_percent`、`confidence`、`markets`、`positions_to_take`、`execution_plan` 和 `strategy_context`。证据钱包、实体调整、延迟档、成本分解、算法/参数版本放进已有 `strategy_context`；稳定状态使用现有 `Opportunity.stable_id` 和后续 `TradeSignal` 去重链，不新增意图表。

策略不得读取私钥、签名或直接调用 CLOB。它只表达候选方向、规模上限和执行约束，最终仍由 Trader 编排器形成 `TraderDecision/TraderOrder`。

### 7.4 Trader 隔离、统一执行与全局风险

每个模块分别拥有：

- 独立 `Strategy/StrategyVersion` 与 `Trader` 配置；
- 通过现有 `strategy_type`、`strategy_key/version`、`trader_id` 独立信号、订单、持仓和收益归因；
- 独立日亏损、单笔、单市场和并发仓位预算；
- 独立暂停、回滚和版本切换。

所有模块只共享以下账户级安全门：

- 总资金和总敞口；
- 同 token 重复买入/自成交；
- 同市场矛盾方向和组合敞口；
- 账户权限、签名和全局 kill switch；
- 交易所健康、数据新鲜度和对账完整性。

“模块互不干扰”不能解释为绕过账户总风险。业务信号与账本独立，账户安全仍统一仲裁。

新闻、天气、加密货币、体育仍各自使用原有 `source_key`、策略和 Trader；本轮既不订阅它们的内部事件，也不改它们的策略源码。钱包策略可以交易这些类别的市场，但订单归因始终是 `source=traders + strategy_type=wallet_consensus_alpha_v1`。

## 8. Wallet Consensus Alpha V1 收益机制

本节是基于 `[L]` 当前可用字段、`[E]` 模仿交易/执行质量研究和 `[O]` 实盘框架纪律形成的待验证策略规格，不是外部项目中现成可复制的盈利算法。所有阈值必须由本机 point-in-time 数据预注册并经 Shadow/Canary 验证，不能从社区帖子抄数。

### 8.1 机制定义

Alpha V1 不跟“钱包数量”，而跟**独立实体、类别专长、真实增量订单和延迟后仍可复制的共识**。

完整漏斗：

```text
钱包成交事实
-> 方向和结果身份确认
-> 实体去重
-> point-in-time 钱包/实体质量
-> 市场类别与期限适配
-> 新共识状态变化
-> 延迟后可执行盘口
-> 净优势下界
-> Opportunity
-> TradeSignal
-> TraderDecision 风险复核
-> TraderOrder / TraderPosition
-> Shadow/Canary 退出与结算
```

### 8.2 钱包/实体资格

不得使用今天的排行榜回填过去。每个信号只读取当时已经可见的数据快照。

建议特征：

- 类别内已结算样本数、净 PnL、Brier/校准和收益因子；
- 价格桶和持有期分层表现；
- maker/taker/switcher 行为及执行质量；
- 交易频率、市场覆盖和集中度；
- leave-one-market-out 与前后半段稳定性；
- 资金来源、实体归属、疑似自成交/对冲/做市标签；
- 复制后的历史收益，而不只是 leader 自己的收益。

小样本必须向市场基准收缩；全局盈利但某类别没有样本的钱包不能被当作该类别专家。

### 8.3 共识信号

推荐使用有符号增量而非窗口内累计持仓：

```text
entity_flow = Σ entity_weight * signed_new_notional
conflict_ratio = opposite_flow / gross_flow
entity_hhi = Σ (entity_notional / gross_notional)^2
```

触发必须来自状态变化，例如：

- 第二/第三个独立实体在时间窗内加入同侧；
- 加权净流首次越过阈值；
- 原有共识显著增强；
- 共识方向翻转。

同一共识状态不应每 60 秒重复生成新决策。策略应使用已有 `Opportunity.stable_id` 表达“市场 + 结果 token + 共识状态”的确定性指纹，再由已有 `TradeSignal.dedupe_key` 和数据库唯一约束阻止重复；只有贡献实体、方向或增量流等真实状态发生变化才形成新的指纹。具体指纹输入必须来自现有 rollup/confluence 字段并写入 `strategy_context`，不能依赖当前时间随机变化。

### 8.4 类别和延迟适配

钱包共识策略可以交易新闻、天气、加密货币、体育对应的市场，但这是钱包策略自己的类别适配，不调用也不修改四个原模块的内部策略。

| 市场形态 | 当前 30/60/120 秒延迟下的默认处理 |
|---|---|
| 中长期事件、政治/宏观/一般新闻 | 可进入 Shadow copyability 评估 |
| 天气日高温等小时/天级市场 | 可评估，但必须按结算站点和规则识别 |
| 体育赛前市场 | 可评估 |
| 体育滚球/进球后重定价 | 默认阻断，除非未来实测达到事件级低延迟 |
| 长期限加密事件 | 可评估 |
| 5m/15m 加密方向盘 | 默认阻断；当前链路不具备稳定的亚秒竞争力 |

### 8.5 可复制优势

禁止使用钱包原成交价或 Gamma 展示价作为我们的成本。对大小为 `q` 的订单：

```text
entry_cost(q) = ask_vwap_after_latency(q)
copy_edge_resolution(q)
  = calibrated_p_outcome
  - entry_cost(q)
  - fee(q)
  - slippage_buffer(q)
  - uncertainty_buffer

copy_edge_exit(H, q)
  = E[executable_bid_at_H(q)]
  - entry_cost(q)
  - entry_fee
  - exit_fee
  - partial_fill_and_exit_buffer
```

`calibrated_p_outcome` 由历史上同类、同期限、同价格桶、相似实体质量和共识状态的 OOS 结果估计；样本不足时不制造概率，只进入观察或极小 Shadow。

下单条件应是净优势下界大于零，而不是固定 spread 小于 75 bps。固定最大价差仍可保留为灾难性上限，但不是主要收益判断。

### 8.6 下单和退出

- Alpha V1 默认 `shadow`；
- 订单价格上限由 `min(max_copy_price, fair_value - cost_buffer)` 决定；
- 不因 leader 继续买入而无限追价；超过价格漂移或 TTL 自动取消；
- Shadow 同时记录 IOC/taker-limit 和 passive-limit 两套反事实，但只允许一个正式执行策略；
- 退出并行记录三种策略：leader sell、consensus reversal、hold-to-resolution；
- Canary 初期固定 1--5 美元上限，不直接启用 Kelly 放大；
- 有足够实盘填单和滑点样本后，才允许保守 fractional Kelly，并继续受硬上限限制。

### 8.7 反向收益研究支线

不在 Alpha V1 实盘启用，但保留研究：

- 共识出现后价格已过度跳涨，复制 edge 变负时，观察 5m/30m/2h 回撤；
- 大量公开复制者拥挤、盘口变薄和 leader 退出时，研究 crowding fade；
- 被系统长期判定为不可复制的热门钱包，研究“leader 盈利、follower 亏损”的逆向标签。

这些支线必须独立策略 slug 和独立账本，不能与正向共识混合计算 PnL。

## 9. 快速但严谨的验证顺序

### 阶段 0：冻结控制组

- 保留当前 `traders_confluence` 及其配置，不覆盖；
- 记录源码 commit、镜像 digest、数据库策略版本和运行快照；
- 新策略默认 disabled，使用新 Trader ID 和独立风险预算。

### 阶段 1：数据链路 Smoke

目标不是盈利，而是证明每个漏斗节点都真实工作：

- `WalletTradeEvent/WalletActivityRollup` 有稳定幂等依据并能追溯来源；
- outcome identity、category、end_at、token 映射逐项统计；有来源的必须一致，缺失或冲突的候选不得交易；
- strict WS quote 能覆盖候选 token；
- 相同 `Opportunity.stable_id` 不重复形成订单决策；
- 日志能沿 `TradeSignal -> TraderDecision -> TraderOrder` 追溯到原始共识证据。

### 阶段 2：可执行 Shadow

每个候选同时记录 30/60/120/300 秒延迟档的：

- 可买 ask VWAP 和可卖 bid VWAP；
- quote age、spread、深度、部分成交；
- 5m/30m/2h/24h 可执行 markout；
- 最终结算 PnL；
- 没下单时的精确原因。

快速排障看漏斗，收益判断看样本，不用等待所有长期市场结算才发现数据问题。

### 阶段 3：Canary Live

推荐晋级条件，不是收益保证：

- 数据身份和幂等错误为 0；
- 严格报价覆盖率达到预设目标，未知报价不进入订单；
- 至少 100 个可执行 Shadow 填单样本，并有足够的短期 markout；
- 至少 30 个已结算或等价可核验退出样本；
- 60/120 秒延迟档扣除全部成本后没有显著负收益，且主要收益不由单一市场/钱包贡献；
- 最大回撤、单钱包/实体贡献、类别集中度和尾部亏损均在预算内；
- CLOB V2 鉴权、签名、下单、撤单、查询和对账已用最小探针验证。

Canary 采用人工开关、1--5 美元单笔、每日亏损硬上限和自动停机。不能用 Shadow 的成交率和延迟替代 Live。

## 10. 天气策略调研（本轮不改天气代码）

### 10.1 值得参考的方向

1. **规则/站点一致的概率桶模型**：解析每个市场的 resolution source、机场站点、当地日期、单位和舍入规则；用 NBM/ECMWF/GEFS/HRRR 等集合预报，经站点和 lead-time 校准后转换为完整互斥温度桶概率。
2. **模式更新 surprise**：记录每次模型 run 的发布时间和新旧分布变化，研究市场在新 run 后的重定价速度；只在可执行盘口仍有净 edge 时交易。
3. **临近结算 nowcast**：利用规则指定站点的 METAR/ASOS 已实现最高温与剩余时段分布，避免把市中心天气或非结算站点当真值。
4. **完整桶面约束**：同一事件所有互斥桶的概率和、可买/可卖深度和组合成本可能产生结构性机会；必须用整篮子真实成交成本。
5. **城市 × lead time × 天气形态校准**：以 Brier、CRPS、可靠性曲线和 OOS PnL共同评估，不以单一 MAE 或命中率判断。

### 10.2 可参考项目与资料

- `[O]` NOAA NBM 当前文档和历史/再预报资料：概率校准基础；
- `[O]` ECMWF Open Data、ENS 和 hindcast：全球集合预报与校准；
- `[O]` NOAA ASOS/METAR 文档：结算站点和观测精度；
- `[C]` `alteregoeth-ai/weatherbot`：MIT，参考机场坐标、ECMWF/HRRR/METAR、模拟记录和自校准的工程结构；不采用其未核验收益声明；
- `[C]` `yangyuan-zhen/PolyWeather`：可作为数据报告和站点映射线索，采用前需重新审计当前 commit、license、结算规则和实际数据；
- `[E]` 《Four Strategies, 562 Trades, Zero Edge》：509 笔 live 天气交易整体亏损，而 paper 显示盈利，是重要反例；提示当模型 MAE 接近 2°F 桶宽时，表面概率优势可能不可交易。

### 10.3 明确拒绝的做法

- 单个天气 App 点预测直接映射到一个桶；
- 多模型简单多数票当概率；
- 用错机场、当地日期、单位、整度舍入和规则修订窗口；
- 模型概率减 UI 展示价直接叫 edge；
- 用 paper 胜率替代可执行盘口和 live 成交；
- 本轮顺手修改现有天气策略代码。

## 11. 策略搜集登记模板

建议每个候选策略一条 YAML/数据库记录，字段如下：

```yaml
record_id:
status: idea|rejected|research|shadow|canary|live|retired
discovered_at:
reviewed_at:

source:
  evidence_class: L|O|E|C
  type: local_code|runtime_log|official_docs|official_repo|community_repo|paper|dataset|social|product_report
  url:
  author:
  published_at:
  commit_hash:
  license:
  last_activity_at:
  independently_verified_at:
  local_audit_path:
  claim_level: architecture|implementation|backtest|paper|shadow|canary|live

strategy:
  name:
  module: wallet_consensus|news|weather|crypto|sports
  hypothesis:
  signal_logic:
  signal_sources: []
  market_universe:
  category_scope: []
  entry_rule:
  exit_rule:
  sizing_rule:
  capacity_assumption:

timing:
  source_event_timestamp_field:
  source_to_ingest_p50_ms:
  source_to_ingest_p95_ms:
  ingest_to_decision_p50_ms:
  ingest_to_decision_p95_ms:
  decision_to_ack_p50_ms:
  decision_to_ack_p95_ms:
  decision_to_fill_p50_ms:
  decision_to_fill_p95_ms:
  backtest_delay_scenarios: []

data_quality:
  point_in_time: true|false|unknown
  outcome_source:
  orderbook_depth_available: true|false
  fee_schedule_version:
  missing_rate:
  duplicate_rule:
  entity_dedup_method:
  survivorship_bias_risk:
  lookahead_bias_risk:

backtest:
  period:
  in_sample_period:
  out_of_sample_period:
  candidate_signals:
  attempted_orders:
  submitted_orders:
  filled_orders:
  partial_fills:
  resolved_orders:
  wins:
  losses:
  gross_pnl:
  fees:
  slippage:
  net_pnl:
  roi:
  profit_factor:
  max_drawdown:
  concentration_top1_pct:
  negative_controls: []
  bootstrap_or_ci:

live_evidence:
  mode: none|paper|shadow|canary|live
  account_or_wallet_reference:
  verifiable_order_ids: []
  verifiable_tx_hashes: []
  attempted_orders:
  acknowledged_orders:
  fills:
  cancels:
  rejects:
  reverted_or_ghost_fills:
  realized_pnl:
  unrealized_pnl:
  fees:
  actual_slippage:
  max_drawdown:
  evidence_boundary:

risks:
  market_risks: []
  execution_risks: []
  data_risks: []
  security_risks: []
  legal_or_access_risks: []
  license_risks: []

decision:
  verdict:
  reason:
  reusable_components: []
  forbidden_components: []
  next_test:
  rollback_condition:
```

## 12. 日志与收益归因

每个候选必须能沿同一 `trace_id` 查到：

```text
WalletTradeEvent / WalletMonitorEvent
-> WalletActivityRollup
-> MarketConfluenceSignal
-> DataEvent(TRADER_ACTIVITY)
-> Opportunity(stable_id, strategy_context)
-> TradeSignal(id, dedupe_key)
-> TraderDecision + TraderDecisionCheck
-> TraderOrder / provider ack-fill-cancel-verification
-> TraderPosition / settlement
-> realized PnL
```

最低日志字段使用现有字段命名：`source`、`strategy_type/strategy_key/strategy_version`、`trader_id`、`signal_id`、`decision_id`、`order_id`、`trace_id`、`market_id/condition_id/token_id/outcome_index`、钱包/聚类证据、`traded_at/created_at/detected_at`、原始价、`entry_price/effective_price`、深度、费用、滑点、净 edge、`TraderDecisionCheck` 门禁结果和拒绝原因。尚不存在或本阶段没有来源的字段必须记为缺失，不能用默认值伪装完整。

收益必须同时按模块、策略版本、类别、钱包/实体、延迟档和退出策略拆分。未结算浮盈、Shadow PnL 和实盘已实现 PnL不得合并显示。

## 13. 实施顺序和回滚边界

1. 冻结并导出当前控制组配置和指标；
2. 只在现有 `WalletActivityRollup`、`MarketConfluenceSignal` 与市场缓存衔接处补类别、结果身份和行情覆盖，运行只读验证；
3. 新增 `wallet_consensus_alpha_v1`，默认 disabled；
4. 新建独立 Shadow Trader，不覆盖现有 Trader；
5. 先跑信号漏斗与可执行 Shadow，核对日志和账本；
6. 达到晋级条件后，另行评审 Canary Live；
7. 任一阶段发现身份错误、串账、重复订单、对账不平或 V2 兼容问题，立即禁用新策略并回退到控制组；四个原模块不受影响。

## 14. 参考链接

### 14.1 工业开源架构与实盘运行参考 `[O]`

- NautilusTrader 官方仓库：<https://github.com/nautechsystems/nautilus_trader>
- NautilusTrader Architecture：<https://nautilustrader.io/docs/latest/concepts/architecture/>
- NautilusTrader Live Trading：<https://nautilustrader.io/docs/latest/concepts/live/>
- NautilusTrader Data（`ts_event/ts_init`）：<https://nautilustrader.io/docs/latest/concepts/data/>
- NautilusTrader Backtesting：<https://nautilustrader.io/docs/latest/concepts/backtesting/>
- NautilusTrader Execution/Reconciliation：<https://nautilustrader.io/docs/latest/concepts/execution/>
- NautilusTrader Polymarket integration：<https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/polymarket.md>
- Hummingbot 官方仓库：<https://github.com/hummingbot/hummingbot>
- Hummingbot V2 Strategies/Controllers/Executors：<https://hummingbot.org/strategies/v2-strategies/>
- QuantConnect LEAN 官方仓库：<https://github.com/QuantConnect/Lean>
- LEAN Algorithm Framework：<https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview>
- Freqtrade 官方仓库：<https://github.com/freqtrade/freqtrade>
- Freqtrade Strategy 101（backtest/dry-run/live）：<https://www.freqtrade.io/en/stable/strategy-101/>
- Freqtrade Lookahead Analysis：<https://www.freqtrade.io/en/stable/lookahead-analysis/>

上述项目用于证明架构和运行纪律可落地，不列作 Wallet Consensus 盈利案例。外部项目的 README 收益截图、模拟回报和未审计账户均不进入晋级证据。

### 14.2 Polymarket、数据、策略与天气研究来源

- Polymarket API：<https://docs.polymarket.com/api-reference/introduction>
- Polymarket Data API 交易字段：<https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets>
- Polymarket 用户活动现金流：<https://docs.polymarket.com/api-reference/core/get-user-activity>
- Polymarket 市场数据：<https://docs.polymarket.com/market-data/overview>
- Polymarket CLOB V2 迁移：<https://docs.polymarket.com/v2-migration>
- Polymarket changelog：<https://docs.polymarket.com/changelog>
- Polymarket resolution：<https://docs.polymarket.com/concepts/resolution>
- Polymarket 当前 Python SDK：<https://github.com/Polymarket/py-sdk>
- Polymarket CTF Exchange V2：<https://github.com/Polymarket/ctf-exchange-v2>
- Prediction Market Analysis：<https://github.com/Jon-Becker/prediction-market-analysis>
- Prediction Market Backtesting：<https://github.com/evan-kolberg/prediction-market-backtesting>
- PMXT Archive：<https://archive.pmxt.dev/Polymarket/v2>
- Polymarket Insider Tracker：<https://github.com/pselamy/polymarket-insider-tracker>
- Polymarket Toolkit：<https://github.com/runesleo/polymarket-toolkit>
- Polymarket Trade Tracker：<https://github.com/leolopez007/polymarket-trade-tracker>
- Polycopier：<https://github.com/cbaezp/polycopier>
- Copy Trader Research：<https://github.com/BallesJr/polymarket-copy-trader>
- Smart Money Research：<https://github.com/edulabrador/polymarket-smart-money>
- The Return to Imitation：<https://ssrn.com/abstract=6670318>
- Who Wins and Who Loses：<https://ssrn.com/abstract=6443103>
- Execution, not Information：<https://ssrn.com/abstract=6191618>
- Domain-Specific Calibration：<https://arxiv.org/abs/2602.19520>
- NOAA NBM：<https://vlab.noaa.gov/web/mdl/nbm-documentation>
- ECMWF Open Data：<https://www.ecmwf.int/en/forecasts/datasets/open-data>
- NOAA ASOS：<https://www.weather.gov/asos/>
- WeatherBot reference：<https://github.com/alteregoeth-ai/weatherbot>
