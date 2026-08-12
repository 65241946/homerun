# 修复 spec:fix-07 WS 价格覆盖不足导致非加密信号全被拒

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`
> 来源:操作员填入 Polymarket 凭证后的第二轮实跑观测(见 `docs/diagnosis-live-run-2026-08-12.md` 的后续)。
> **前置事实**:凭证修复后 `orderflow imbalance unavailable`(原 ~37,000 条)已归零;剩余阻塞集中在本 spec。

---

## 观测事实(填凭证并重启后 10 分钟窗口)

| 决策 | 数量 |
|---|---|
| blocked | 143 |
| skipped | 100 |
| **selected** | **0** |

拒绝原因分布:

| 原因 | 次数 | 判定 |
|---|---|---|
| `Strict WS pricing required before execution: source=unknown age_ms=unknown max=10000 required=['ws_strict'...]` | **143(占 blocked 的 100%)** | 🔴 本 spec 目标 |
| `Book spread 800.0 / 2222.2 / 1052.6 bps exceeds max_spread_bps 75.0` | ~40 | ✅ **正确拒绝**,市场真实价差过宽,勿动 |
| `copy_trade_gate_failed:min_notional` | 25 | ✅ 正常风控,勿动 |

`orderflow imbalance unavailable` 已 **0 次** —— 凭证问题确认解决。

## 根因(已核实)

1. **闸门本身是对的**:`decision_gates.py:1170-1250` 的 `strict_ws_pricing` 要求 `market_data_source ∈ {"ws_strict","redis_strict"}`;`strict_ws_pricing_only` 默认 `True`(`trader_orchestrator_state.py:141`)。没有可靠实时价就不下单,这是正确的安全设计。
2. **`source=unknown` 意味着"根本没有 WS 价"而非"价太旧"**:`live_market_context.py:737/739` 写 `yes_source = "ws_strict" if yes_live is not None else None` —— 拿不到实时价时 source 为 `None`,`live_age_ms` 也是 `None`。观测到的正是 `source=unknown age_ms=unknown`。
3. **订阅覆盖只有加密市场**:`market_runtime._sync_crypto_subscriptions`(`market_runtime.py:1662`)只订阅 `self._crypto_markets` 的 token。日志 `Prewarmed CLOB market info cache: targets 15` 与之吻合。
4. ⇒ **traders_confluence / copy-trade 的信号指向钱包实际在交易的任意市场**(体育、政治、新闻等),这些市场**从不在订阅集内** → 永远拿不到 `ws_strict` 价 → 143/143 全部 blocked。

`intent_runtime.py:1190-1215` 存在"热订阅"动态路径(`polymarket_feed.subscribe(missing)`),但显然未覆盖交易员信号的市场 —— **本 spec 的核心就是查清它为何没生效,并补上覆盖**。

## Your task

### 第一步:先调研(必须,写入产出文件)

不要直接改代码。先回答:

1. `intent_runtime` 的热订阅(`_hot_subscription_tokens` / `subscribe(missing)`)**由谁触发、在什么条件下触发**?traders 信号进入编排器时,其 token 是否会走这条路径?若不会,是缺调用点还是被条件挡住?
2. traders_confluence / copy-trade 的信号 payload 里**是否带 token_id**?(`docs/analysis-wallet-consensus-chain.md` 记录钱包身份只在 `strategy_context`;token 是否同样只在 payload 深处)若拿不到 token,订阅无从谈起 —— 那是另一个问题,如实记录。
3. WS 订阅**是否有容量上限**?无限订阅是否会打爆连接?查 `ws_feeds.py` 的订阅实现与任何上限/分片逻辑。**这决定方案能否是"全量订阅"**。

### 第二步:按调研结论实施(二选一,并说明理由)

**方案 A(优先):按需订阅**
交易员/共识信号进入编排器前,对其 token 触发热订阅并等待首个 tick(有界超时)。首次拿不到就本轮跳过,下轮已订阅即可通过。
- 必须尊重第一步查到的容量上限;若有上限,实现 LRU/引用计数淘汰,并把上限做成 config key(进 `default_config` + schema)。
- 订阅失败/超时要有**明确可诊断的拒绝原因**(如 `ws_subscribe_timeout`),不要退回 `source=unknown`。

**方案 B(仅当 A 不可行):shadow 降级**
`strict_ws_pricing_only` 对 **shadow 模式**允许降级到 REST/缓存价,并在决策 payload 标注价格源与 age;**live 模式保持严格**。
- 新 key(如 `strict_ws_pricing_shadow_relaxed`,默认值在产出文件说明取值理由)。
- ⚠️ 降级会让 shadow 的成交价与 live 不可比,**必须在产出文件显著标注这一影响**。

**无论 A 还是 B,都不得放宽 live 模式的严格性。**

### 不要动的东西

- `Book spread ... exceeds max_spread_bps` —— 真实宽价差,拒得对。
- `copy_trade_gate_failed:min_notional` —— 正常风控。
- `strict_ws_pricing` 闸门本身的判定逻辑(它没错,错的是上游没喂数据)。

## 回归测试

- 有 WS 价(`source=ws_strict`,age 在限内)→ 闸门通过(回归保护)。
- 无 WS 价 → 按所选方案:A 触发订阅且拒绝原因为可定位的超时原因;B 在 shadow 下降级通过且标注价格源,在 live 下仍拒。
- **live 模式无 WS 价 → 必定拒绝**(安全回归,必测)。
- 订阅容量上限(若存在)生效:超限时淘汰策略可预期,不会无界增长。

## 完成判据

- 调研结论(三问)写入 `.piercode/fix-07-last-message.md`。
- 所选方案 + 理由 + 影响(尤其方案 B 的可比性影响)写入产出文件。
- 回归测试绿;不改动上面"不要动"的三项。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交推送;push 前 `git pull --rebase`。

## 与其它 spec 的关系

- 与 **fix-05**(crypto 策略)文件不重叠。
- 与 **fix-06** 有**潜在重叠**:fix-06 P-2 也涉及 shadow 与实盘能力的解耦。若 fix-06 已实施 P-2,本 spec 的方案 B 需与其保持一致口径,不要引入第二套 shadow 放宽开关 —— 实施前先 `git pull` 看 P-2 落地情况。
