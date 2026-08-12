# Wallet Consensus Alpha V1 进度记录

## 2026-08-10

### Phase 1：约束与现状核对

- **Status:** in_progress
- 已确认新闻作为单一业务模块，不做内部类别拆分。
- 已确认公共底层数据逻辑可改基座，所有业务策略逻辑放独立策略层。
- 已确认钱包共识、新闻、天气、加密、体育必须保持独立策略、独立下单与互不串账。
- 已建立独立研究目录，未修改任何交易代码或运行配置。

### Verification

| Check | Result |
|---|---|
| HOMERUN code/config modified in this phase | No |
| Existing module strategy logic modified | No |
| Independent research records created | Yes |

### Next

- 核对钱包共识零订单的分层根因。
- 扩展 GitHub 之外的策略来源与天气研究来源。
- 确认 Alpha V1 首阶段验收终点。

### Design source audit

- 核对 NautilusTrader、Hummingbot Strategy V2、QuantConnect LEAN、Freqtrade 的官方架构与实盘运行文档。
- 确认这些来源支持的是组件边界、运行模式和验证纪律，而不是任何收益承诺。
- 对照 HOMERUN 真实模型与调用链，决定删除首稿中新造的平行接口名称，改用现有 `BaseStrategy`、`Opportunity`、`TradeSignal`、`TraderDecision`、`TraderOrder`、`TraderPosition`。
- 尚未修改交易代码、数据库结构或运行配置。
- 进一步确认 NautilusTrader 官方源码已有 Polymarket data/execution adapter 和 smoke tester，可作为接口/对账审计来源；不计划替换 HOMERUN 执行层。
- 源码确认 HOMERUN 已有 `traders_copy_trade` 单钱包分支和 `traders_confluence` 多钱包分支；推荐方案改为复用现有 `trader_activity` 订阅和统一执行链，不新建平行订单接口。
- 已将正式草案中的拟议 `WalletTradeFactV1`、`EntitySnapshotV1`、`ExecutableQuoteV1`、`OrderIntentV1`、`ExitIntentV1` 和 `module_key` 从实施架构中删除，改用 HOMERUN 真实模型和字段。
- 已增加逐项来源映射：NautilusTrader、Hummingbot、LEAN、Freqtrade 只作为官方工业开源架构/运行纪律来源；论文和社区项目分别标注实证与线索边界。
- 已确认最小基座补充点为现有 `WalletActivityRollup/MarketConfluenceSignal` 与 `CachedMarket` 的元数据衔接；尚未修改交易代码、配置或数据库。
- 设计文档进入用户评审，批准后才会形成实施计划。
- 文档自检结果：拟议平行接口关键词 0 处；14 个代码围栏成对；13 个本地代码证据路径和关键类/函数均存在；尾随空格 0 处。
# 2026-08-10

- Confirmed scope: news remains one whole module; news/weather/crypto/sports strategy internals are out of scope.
- Inspected the real HOMERUN strategy registry, discovery/trading process isolation, wallet confluence, entity clustering and trader execution path.
- Queried the running PostgreSQL ledger and froze a zero-order root-cause snapshot.
- Audited current official Polymarket API/CLOB V2 documentation, research papers, datasets and fixed GitHub commits.
- Drafted the full Alpha V1 architecture and strategy research record schema.
- No trading code or runtime configuration was changed in this design phase.

## 2026-08-11：真实开源来源复核

- 用户明确要求钱包共识架构优先参考 GitHub/社区真实量化项目和可核验实盘案例，禁止凭空构造。
- 已确认仓库存在上一版独立研究目录和设计稿；本轮不新建平行方案，先逐项审计其来源与 HOMERUN 当前源码映射。
- 已确认主工作树存在大量既有未提交交易代码；本轮保持交易代码、数据库和运行配置只读。
- 正在并行核验三类证据：HOMERUN 本地真实调用链、GitHub 固定源码能力、论文/公开订单/官方文档的证据边界。

### Phase 6 收口

- 已完成当前官方 Data/Activity/CLOB V2 契约、官方 SDK 归档/替代关系核验。
- 已完成 `py-sdk`、PMXT、prediction-market-analysis、prediction-market-backtesting、NautilusTrader、smart-money、insider-tracker 等项目的源码能力、版本、许可证、测试和收益证据分级。
- 已完成 HOMERUN 当前钱包发现、活动、聚合、实体、共识、DB 策略、信号桥、统一执行和账本链路映射。
- 已发现当前工作树非可复现基线，以及 outcome identity、rollup 幂等、共识 token identity、实体证据和合成 edge 等实施前门禁。
- 已更新设计草案的来源矩阵和可复现性说明；没有修改交易代码、数据库、配置或运行服务。
- 下一步仅在用户批准推荐架构后，才形成逐文件实施计划。

## 2026-08-11：按完整提示词重新思考

- 重新确认边界：基座只允许修改钱包数据获取/标准化/聚合/分发；钱包业务逻辑放独立策略层；其他四个模块原策略不改。
- 本轮新增关键审查口径：分别验证候选信号、策略批准、Shadow/模拟成交、真实 CLOB 成交，不能把它们统称为“订单”。
- 正在重新核对当前源码/运行模式与外部来源映射；本轮仍不修改交易代码、数据库或运行配置。
