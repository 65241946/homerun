# 修复 spec:fix-09 价差闸应按持有意图分档,而非全局一刀切

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`(含 fix-01~fix-07)
> 来源:操作员实跑发现 —— 跟单机会(traders_confluence,信念 86/100、7 个钱包共识、净名义 $9.5K)在手动买入时被 `Book spread 689.7 bps exceeds max_spread_bps 75.0` 拒绝。

---

## 问题(操作员指出,架构师核实并认同)

`max_spread_bps = 75.0`(`services/strategy_sdk.py:670`,coerce 于 `:2198`,worker 回退于 `trader_orchestrator_worker.py:3906`)作为**全局 `risk_limits` 默认值**,由 `order_manager._check_max_spread_bps`(`:492`)在下单前闸(`:1117-1135`)和 live 闸(`:1387-1402`)统一施加,**不分策略类型、不分持有意图**。

**75bps 这个数只对"往返型"策略成立** —— 做市、日内、套利要进出两次,买卖价差是真实的往返成本。

**但对"持有到期型"策略,价差不是往返成本**:
- 跟单 / 共识 / 事件押注在卖一价买入后**持有到结算**,拿 $1 或 $0,**出场不经过盘口**;
- 价差只产生**单边影响**:以 ask 买入相对 mid 多付**半个价差**。
- 实例:Lynn Vision 盘口 38¢/62¢,ask 43¢,mid ≈ 39.5¢ ⇒ 实际多付约 3.5%,而非闸门隐含的 6.9% 往返损失。

⇒ 当前实现**把所有宽价差市场的持有型策略全部封死**。体育、电竞、小众政治盘天然宽价差,而这些正是钱包共识信号最常出现的地方。实跑中 40+ 条 `Book spread ... exceeds` 拒绝全部源于此。

**佐证**:`sports_overreaction_fader` 自己的 `max_spread_bps` 默认是 **200**(`:83`)—— 该策略作者已经意识到体育盘价差大。但那是策略内部闸,**挡不住 order_manager 的全局 75**,策略的意图被上层闸悄悄否决。这本身也是一个"两套阈值互相抵消"的实例(与 fix-02 P0-4 同类)。

## Your task

### 第一步:调研(必须,写入产出文件)

1. 枚举 `max_spread_bps` 的**全部施加点**(`order_manager` 下单前闸、live 闸、`venue_gates.max_spread_bps_gate`、`session_engine:1129`、各策略自有闸),说明每处的作用域与优先级 —— **哪一道实际决定了最终拒绝**。
2. 逐个策略判定其**持有意图**:往返型(做市/日内/套利)还是持有到期型(跟单/共识/事件)。列成表。crypto 短周期属往返型;`traders_copy_trade`、`traders_confluence`、`sports_overreaction_fader`、`certainty_shock` 等属持有到期型 —— **但以你的代码核实为准,不要照抄本 spec 的分类**。
3. 确认 `_compute_book_spread_bps` 的口径(相对什么计算 bps:mid?ask?),这决定"单边溢价"该怎么表达。

### 第二步:实施

1. **闸门按持有意图分档**,而不是简单调大全局值:
   - 往返型:维持严格阈值(75bps 量级)。
   - 持有到期型:改用**单边溢价**判据 —— 即 `(ask − mid) / mid`,阈值单独配置(新 key,例如 `max_entry_premium_pct`),默认值在产出文件说明取值理由。
   - 分档依据**优先从策略自身声明读取**(如策略 `default_config` 里的 `hold_intent` 或已有的 `max_spread_bps`),而不是在 order_manager 里硬编码策略名单。**硬编码策略名单是反模式,只在无法从策略读取时作为兜底,并在产出文件说明。**
2. **消除"两套阈值互相抵消"**:策略自身声明的 `max_spread_bps`(如 sports 的 200)必须能**真正生效**,不被全局默认覆盖。这与 fix-02 P0-4 的单一真值源原则一致。
3. **手动买入路径**:操作员是知情决策者。允许其**显式覆盖**价差闸(请求参数如 `acknowledge_wide_spread=true`),但:
   - 默认仍然拒绝并**返回可读的原因 + 实际价差 + 阈值**(当前已有,保留);
   - 覆盖时必须在 `TraderOrder.payload_json` 记录 `spread_gate_overridden: true` 与当时的 `spread_bps`,以便事后归因;
   - **live 模式是否允许覆盖,由操作员决定** —— 在产出文件给出建议但不要擅自放开 live。
4. 新增 key 一律进 `default_config` + schema(带 description),inline 回退等于声明值。

### 不要做的事

- **不要**简单把全局 75 调大 —— 那会让做市/日内策略失去保护,是掩盖而非修复。
- **不要**移除价差闸 —— 它对往返型策略是必要的。
- 不改动 fix-01~fix-07 的任何行为。

## 回归测试

- 往返型策略 + 689bps 价差 → **仍然拒绝**(保护未削弱)。
- 持有到期型策略 + 689bps 价差但单边溢价在阈值内 → **通过**(核心判据,对应操作员实际遇到的场景)。
- 持有到期型 + 单边溢价过大(如 ask 远高于 mid)→ 仍拒绝。
- 策略自身声明的 `max_spread_bps`(如 sports 200)生效,不被全局默认覆盖。
- 手动买入:默认拒绝且原因可读;带确认标志时通过并在 payload 记录 `spread_gate_overridden`。
- live 模式行为按第 3 条的决定,并有测试固定该行为。

## 完成判据

- 第一步三项调研结论写入 `.piercode/fix-09-last-message.md`(含施加点表、策略持有意图表、bps 口径)。
- 新增 key 的默认值 + 取值理由写入产出文件。
- 回归测试绿,尤其"往返型仍拒 / 持有型放行"这对对照。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交推送;push 前 `git pull --rebase`。

## 背景纠正(供实现方理解意图)

架构师最初把该拒绝判定为"正确行为",理由是往返价差成本。**该判断有误并已收回** —— 对持有到期的方向性押注,出场不经过盘口,价差不构成往返成本。本 spec 以修正后的理解为准。
