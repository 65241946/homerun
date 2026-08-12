# 修复 spec:fix-04 批次 A(与已合并修复对账后的版本)

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`(含 fix-01/02/03)
> 来源:`docs/repair/修复施工规格书_两批策略.md` 的 WO-A1~A5。**该规格书写于旧基点(裸 389d246),部分工单项已被 fix-02/fix-03 完成或已被架构决策取代。**
> 本文是**对账后的权威版本**。实现方以本文为准,**不要**直接照 WO-A 原文施工,也**不要**应用 `docs/repair/traders_copy_trade_p0_draft.patch`(已作废,见下)。

---

## 零、对账表(先读,避免回退已审代码)

| WO-A 原项 | 裁决 | 说明 |
|---|---|---|
| A1.1 copy_delay/max_age 死锁 | ❌ **作废** | fix-02 已修,方案不同且更优:在 evaluate 算 `effective_max_age = min(hard_ceiling, copy_delay + max_age)`,**不改写用户配置**。A1 原方案会篡改用户设定的 `max_signal_age_seconds`,不采纳。 |
| A1.2 edge 公式 | ❌ **作废** | fix-02 已按操作员拍板的 **EV 口径**实现:`edge_ev = confidence − entry_price`,`expected_payout = clamp(confidence)`,且 `edge_midpoint/edge_multiplier` 已彻底删除。A1 的"结构上行×conviction"方案与之冲突,且要求保留 deprecated key,违反删除即删干净原则。 |
| A1.3 confidence 缺省统一 | ✅ **仍有效,纳入本 spec**(见 F-1) | fix-02 未覆盖。 |
| A2.2 回退值统一(min_confluence_strength / min_tier) | ✅ **已完成** | fix-03 已修,只需验收不重做。 |
| A5.1 词边界匹配 | ✅ **已完成** | fix-02 已修(`\b…\b` 正则)。只需验收。 |
| A5.2 近因约束 | ✅ **仍有效,纳入本 spec**(见 F-5) | fix-02 只把窗口收到 900s;A5 的"占比≥60%"更严谨,是真正的"漂移≠冲击"判据。 |
| 其余 A1/A2/A3/A4/A5 项 | ✅ **仍有效** | 见下。 |

**通则**:沿用 WO-A 施工总则(单一真值源、零死配置、每 key grep ≥2、纯逻辑单测、默认值变更列表、不引入新依赖、禁止虚构)。另加:本批次所有费用相关计算必须走 fix-03 的 `utils.kelly` canonical helper 并尽可能传 `category`,**不得**调用 `polymarket_taker_fee_legacy_quartic`。

---

## F-1 `traders_copy_trade.py`(A1 剩余项)

1. **confidence 缺省统一**(A1.3):`_build_copy_opportunity` 的 `to_confidence(copy_event.get("confidence"), 0.70)` 与 evaluate 侧缺省不一致 → 两处统一为 `min_confidence` 的默认值(0.45)。消除"detect 当高信心放行、evaluate 再拒"的浪费。
   - ⚠️ 注意:fix-02 的 EV 公式 `edge_ev = confidence − entry_price` **依赖 confidence**,改缺省会改变无 confidence 信号的 edge。这是预期内的一致化,但**必须在测试中固定新行为**。
2. **风控默认值收紧**(A1.4):`leader_allocation_cap_pct` 100→**25**;`max_copy_drawdown_pct` 100→**50**。三个 `1_000_000` 预算保留数值,但 schema description 加 **"上线实盘前必须按账户规模设置"**。
3. **resolution_date 造假**(A1.5):删除 `detected_at + 15min`;用市场真实 `end_date`,取不到就**不设该字段**。
4. **fail-open 可配**(A1.6):新 key `require_live_context: bool = False`;为 True 时,`live_market` 缺 `liquidity_usd`/`entry_price_delta_pct` 的信号直接拒(检查名不变,pass 逻辑按配置切换)。
5. **热路径**(A1.7):`validate_traders_copy_trade_config` 移入 `configure()` 缓存;`_to_utc(timestamp)` 每信号只解析一次。
6. **卖出镜像测试**(A1.8):补测试证明 `copy_sells=True` 时 leader 卖出 → sell opportunity 能过 inventory 检查并走减仓路径。

**验收**:全 key grep ≥2;单测:confidence 缺省两路径一致;风控新默认;`end_date` 缺失时不设 resolution_date;`require_live_context` 两态行为;卖出镜像。

## F-2 `traders_confluence.py`(A2 剩余项)

1. **方向不明丢弃**(A2.1,P0):`_resolve_trade_outcome` 无明确方向时返回 `None`,调用方跳过 —— **不再默认 "NO"**。这是真实资金风险(方向猜错=反向下单)。
2. **`firehose_max_age_minutes` 720→60**(A2.3,行为变更,记入默认值变更表)。
3. **tier 加权汇聚**(A2.4):新 key `tier_weights = {"low":1.0,"medium":1.5,"high":2.0,"extreme":3.0}`;有效钱包数 = Σ tier_weight(优先用 cluster_adjusted 名单);`min_wallet_count` 改为对加权和判定;tier 同时进入 score。
   - ⚠️ 关联已知问题:`docs/analysis-wallet-consensus-chain.md` 记录"**medium tier 永不产生**"(`_tier_for_count` 只出 EXTREME/HIGH/WATCH,watch→low)。实现 tier_weights 时**先核实该问题是否属实**;若属实,在产出文件里记录(修不修另行决策,本 spec 不要求改 wallet_intelligence)。
4. **重复计算去除**(A2.5):`build_opportunities_from_firehose` 不对已过滤行重跑 `evaluate_firehose_signal`;`normalize_trader_signal` 每行至多一次;`_effective_config()` 按 config 版本 memoize。
   - ⚠️ 与 fix-03 的交互:fix-03 在 pipeline 中**特意移除了提前 normalize**(避免把"缺键"变成"显式全 False")。去重时**不得**恢复那个提前 normalize。
5. **实例态竞态**(A2.6):`self._confluence_strength` 改为经 payload/params 传递到 compute_score/compute_size。

**验收**:方向不明→无信号;加权 wallet_count;单遍 evaluate(调用计数 stub);`_confluence_strength` 无实例态残留(并发两信号不串味)。

## F-3 `news_edge.py`(A3,注意 A3.7 已由 fix-02 完成)

按 WO-A3 原文实施 1–6、8、9 项;**A3.7(阈值单源)fix-02 已完成,只验收不重做**。
重点:
- **age=0 truthiness**(A3.2,P0):`_extract_signal_age_minutes` 改 `is not None` 判定链。
- **发出前刷新价**(A3.3)、**CI 闸门 + 收缩**(A3.4)、**费后闸前置**(A3.5)、**时间衰减**(A3.6)。
- **费后闸必须用 fix-03 的 canonical fee helper 并传 category**(news 类目按 estimator 已有字段映射;拿不到就走保守缺省)。
- LLM 缓存(A3.8)、score 重写(A3.9)。

## F-4 `news_momentum_breakout.py`(A4)

按 WO-A4 原文全部 7 项实施。注意 A4.4(min_liquidity 单源)**fix-02 已完成**,只验收。

## F-5 `certainty_shock.py`(A5 剩余项)

1. **近因约束**(A5.2):新 key `shock_recent_window_seconds=900`、`shock_recent_share_min=0.6` —— 要求总 move 的 **≥60% 发生在最近窗口内**。
   - 这是对 fix-02 的**加强**:fix-02 只把 lookback 收到 900s(避免慢漂移混入),本项才是真正的"冲击 vs 漂移"判据。两者叠加,不冲突。
2. **side 价格序列**(A5.3):history 存 `(ts, yes_price, no_price)`;NO 方向的 move/retrace/target 用 no 序列。
3. **死区对齐**(A5.4):config 校验强制 `shock_min_expected_move >= min_edge_percent/100`。
4. **风险闸生效**(A5.5):risk 公式加流动性与临期项使其可超 0.70,**或**将 `max_risk_score` 默认降至 0.60(二选一,产出文件说明选了哪个及理由)。
5. **热路径**(A5.6):deadline 按市场缓存;`import calendar` 移顶层;history 仅在通过 deadline 检查后累计;市场 key GC。

**验收**:6 小时匀速漂移(近因 share<0.6)→ 拒;末 10 分钟集中重定价 → 过;NO-side 序列正确性;死区校验。

---

## 完成判据

- 上述各项实现 + 每工单的验收单测,`pytest` 相关用例绿(纯逻辑免 DB)。
- **默认值变更表**(旧→新→理由)写入产出文件,供 shadow A/B 对照。
- 全 key grep ≥2(零死配置);无新依赖;`py_compile` 通过。
- **不得回退 fix-02/fix-03 的任何改动**;对账表标"已完成"的项只验收不重做,并在产出文件确认其仍然生效。
- 产出 `.piercode/fix-04-last-message.md`:逐条 `文件:行` + 改动 + 为什么 + 测试证据 + 默认值变更表 + 未验证项。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交并推送;push 前先 `git pull --rebase`(多方并发)。
- 建议**按工单分次提交**(F-1…F-5 各一组 commit,message 前缀工单号),便于审查方逐项核。
