# 分析:钱包共识 → 交易员信号 → 机会 → 机器人执行

> 架构分析,供其他会话复用(不必重跑)。news/体育/加密/天气策略内部另行分析。
> 标注区分:**[已核实]** = 架构师在代码中逐行验证过;**[待核实]** = 代码走查提出、尚未逐一验证,勿当结论用。

## 链路全貌 [已核实]

```
钱包成交
  → WalletActivityRollup                       (smart_wallet_pool 采集/持久化)
  → ConfluenceDetector.scan_for_confluence      (wallet_intelligence:94)
        合格钱包闸 → 按(market_id, side)聚类 → 簇调整钱包数 → conviction 0-100 → tier
  → MarketConfluenceSignal                      (键: market_id + outcome)
  → firehose 读出                                (smart_wallet_pool.get_tracked_trader_firehose_signals)
        + top_wallets[]、source_flags*(*仅 include_source_context=True 时)
  → Opportunity.strategy_context.{firehose.wallets, top_wallets, copy_event, source_trade, wallets, wallet_addresses}
  → TradeSignal(source="traders")               (bridge_opportunities_to_signals / intent_runtime)
  → Trader 编排器: detect → evaluate(scope ∩ 机会内钱包) → risk/gates → TraderOrder
```

## 两个"交易员"的关系 [已核实]

| | 含义 | 载体 | 说明 |
|---|---|---|---|
| **A. 交易员(机器人)** | 系统里配置的自动交易器 | `models/database.py::Trader` | 执行方。**无钱包身份**,靠 `source_configs_json` 订阅信号源 |
| **B. 机会里的交易员** | 被跟踪/被复制的外部钱包(leader) | `DiscoveredWallet`/`TrackedWallet`/`TraderGroupMember` | 只活在**信号 payload 内部**,从不作为 Trader 行存在 |

**唯一连接点**:`Trader.source_configs_json[].traders_scope`(modes: tracked/pool/individual/group)与机会内嵌钱包列表求交集。
- 扁平化读取:`StrategySDK.extract_trader_signal_wallets`(strategy_sdk:1482)—— 汇总 `wallets` / `wallet_addresses` / `top_wallets[].address` / `source_trade.wallet_address` / `strategy_context.{copy_event,source_trade,firehose.*}`
- 求交:`StrategySDK.match_trader_signal_scope`(:1623)

**关键结构事实**:钱包身份**只存在于 `strategy_context` / `payload_json`**,市场腿(markets/positions)里没有。任何消费方想知道"这机会背后是谁",必须走上面两个函数。

## 已确认问题

### 🔴 P0:`include_source_context=False` 的路径恒返回 0 个交易员机会 [已核实]

- `source_flags` 只由 `trader_data_access.annotate_trader_signal_source_context` 附加,且**仅在 `include_source_context=True` 时运行**(trader_data_access:246);原始 firehose 不带该字段。
- `evaluate_firehose_signal`(traders_confluence:338)的 `source_scope_mismatch` 闸(:383-395)要求 `from_tracked ∨ from_pool`,**无缺省保护**;`normalize_trader_source_flags(None)` 返回三键全 False(strategy_sdk:1387)。
- ⇒ False 路径**每行必拒**。受害方:`traders_firehose_pipeline.py:192`(UI/API)、`traders_confluence.py:726`(detect_async)、**`strategy_backtester.py:255`(回测器)**、`traders_copy_trade_signal_service.py:590`。
- **影响**:交易员策略回测恒空、UI/服务侧恒零;只有 worker 主路径(True)出信号。**任何跟单/共识策略的回测结论在修复前都不可信。**
- 修复方案见 `fix-03-foundation-p0.md` P0-b。

### 🟡 已证伪/夸大:copy 5s 新鲜度窗口"结构性为空"

走查称 worker 节奏(默认 30s)必然超过 copy `max_signal_age_seconds`(默认 5s)→ 跟单恒失效。**证伪**:仓库存在快车道(`services/wallet_ws_monitor.py`、`services/trader_orchestrator/fast_submit.py`),copy 的 `detected_at` 取叶子交易时间。若走 WS 快车道,5s 窗口是合理设计。**仅当退回慢桥时才失效** —— 未确认,不作为 bug。若要定性,需追 copy 信号实际走哪条道。

## 待核实清单(代码走查提出,架构师尚未逐一验证)[待核实]

1. **"medium" tier 永不产生**:`_tier_for_count`(wallet_intelligence:478)只输出 EXTREME/HIGH/WATCH,而 canonical 是 low/medium/high/extreme,`watch → low`。⇒ 2–3 个钱包的簇与最弱信号无法区分。
2. **`is_tradeable` 在源头是假值**:原始 firehose 里 `is_tradeable = bool(is_active)`(smart_wallet_pool:846),非真实可交易性;只有 worker 事后用 `get_market_tradability_map` 覆盖。读原始 firehose 的消费方信任了未经检查的值。
3. **`avg_wallet_rank` 算了但丢弃**:持久化在信号上,firehose 读出未透传,到不了策略/机会。
4. **`min_confluence_strength` 在机会构建阶段不设闸**:只在 orchestrator `custom_checks` 检查,且那里缺省 0.55 ≠ `DEFAULT_CONFIG` 的 0.50。
5. **死缺省**(与 `fix-02-audit.md` 记录同类):`min_tier` inline "high" vs 声明 "low";`firehose_exclude_crypto_markets` inline True vs 声明 False。→ 已纳入 fix-03 P0-b 第 3 条一并修。

## 关键阈值速查

`MIN_WALLETS_WATCH/HIGH/EXTREME = 2/4/6`、`MIN_WALLET_RANK_SCORE = 0.20`、`SIGNAL_DECAY_MINUTES = 90`(wallet_intelligence:71-75);conviction 权重:数量 30 / 钱包质量 25 / 时序 15 / 名义 10 / 方向共识 10 / 市场上下文 10,罚项:集中度 ≤10、异常 ≤8。
