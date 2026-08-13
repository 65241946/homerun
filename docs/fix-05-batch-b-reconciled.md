# 修复 spec:fix-05 批次 B 加密高频(与已合并修复对账后的版本)

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`(含 fix-01/02/03,fix-04 进行中)
> 来源:`docs/repair/修复施工规格书_两批策略.md` 的 WO-B0~B4 + v1.1 增补。该规格书写于旧基点,部分项已由 fix-03 完成。
> **本文是对账后的权威版本**,实现方以本文为准。

---

## 零、对账表(先读)

| WO-B 原项 | 裁决 | 说明 |
|---|---|---|
| B0.1 费用统一到 kelly | ✅ **已完成** | fix-03 已把 `crypto_strategy_utils.taker_fee_pct`(:102)和 `:325` 委托给 `utils.kelly.polymarket_taker_fee_pct(..., category="crypto")`,重复公式体已删。**只验收不重做。** |
| B0.2 timeframe-aware 门槛统一 | ✅ 仍有效 | 见 G-0 |
| B0.3 clamp 下限 0.30 约定 | ✅ 仍有效 | 见 G-0 |
| B0.4 共享 `estimate_p_win` | ✅ 仍有效 | 见 G-0,B1/B2 依赖 |
| B1/B2/B3/B4 | ✅ 仍有效 | 见 G-1~G-4 |
| v1.1 第 2 条(定位注记) | ⚠️ 背景,非工单 | 修复后的 directional/convergence 应作 maker 引擎信号层;独立 taker 开仓必须过新费率的费后 EV 闸 |
| v1.1 第 3 条(GLFT 迭代) | ⏸ 不在本批 | 属新策略线,B3 只做"对称腿+skew"第一步 |

**已核实的前置事实(架构师,勿再假设)**:
- 费用曲线已是 2026 制度(`feeRate×p×(1−p)`,crypto 0.07)。**中间价位费用较旧曲线约 ×2.2、尾部更多** ⇒ 本批所有"费后 EV 闸"会显著比旧代码严。这是纠正,不是回归。**任何基于旧费率的阈值直觉作废**;新阈值先按 spec 值落地,真实标定靠回测(人跑)。
- **三块死配置属实**(我已 grep 核实):`convergence_*` 7 个 key **各仅出现 1 次**(只声明、零引用);`maker_quote_*` 同样;`directional_*` 的 `oracle_prob_min/max` 等亦仅 1 次。⇒ B1/B2/B3 的"接线"是真实工作,不是走过场。

---

## 通则(硬约束)

沿用 WO-B 施工总则:单一真值源(inline 回退必须等于 `default_config`)、零死配置(每 key ≥2 处)、不引入新依赖、`py_compile` 通过、每工单纯逻辑单测、默认值变更列表。另加:

- 所有费用计算走 `utils.kelly` canonical helper 并传 `category="crypto"`;**禁止**调用 `polymarket_taker_fee_legacy_quartic`;**禁止**新写平坦费用常数。
- **不得回退 fix-01/02/03 的任何改动**;与 fix-04 可能同时进行,若发现冲突(同文件同函数),停下来在产出文件记录,不要强改。
- **禁止虚构**:规格与代码现实冲突时停下来记录,不要自行发明替代方案。

---

## G-0 共享层(先做,G-1~G-4 依赖)—— WO-B0 剩余项

1. **timeframe-aware 门槛统一**(B0.2):所有策略的 oracle-age / market-data-age / min-seconds-left 默认走 `crypto_strategy_utils` 的 `default_max_oracle_age_ms` / `default_max_market_data_age_ms` / `default_min_seconds_left_for_entry`(约 :344-376);策略 config 只做覆盖,删除各处平坦常数。
2. **clamp 约定**(B0.3):任何 confidence clamp 下限统一 **0.30**,且必须低于该策略默认 `min_confidence`(写进共享注释)。
3. **共享概率 helper**(B0.4):新增
   ```
   estimate_p_win(diff_pct, elapsed_ratio, *, base_scale, min_scale, prob_min, prob_max)
     scale = max(min_scale, base_scale * (1 - elapsed_ratio))
     p     = clamp(bounded_sigmoid(diff_pct / scale), prob_min, prob_max)
   ```
   (`bounded_sigmoid` 已存在,约 :105)。供 G-1/G-2 使用。
4. **验收**:B0.1 的费用委托仍生效(测试断言 `crypto_strategy_utils.taker_fee_pct` 与 `kelly.polymarket_taker_fee_pct(category="crypto")` 对同一输入一致)。

**单测**:`estimate_p_win` 对 diff 单调、对 elapsed 的 scale 衰减(同 diff 晚期 p 更极端)、clamp 边界;per-timeframe 门槛表取值。

## G-1 `btc_eth_directional_edge.py`(核心工单)

按 WO-B1 的 1–9 项实施。要点:
1. **接线 `directional_*` 全块**(约 :615-631):用 G-0 的 `estimate_p_win`;`edge_percent = (p_win − entry_price) * 100`(**量纲=概率点**);phase 由 elapsed_ratio 切 early/mid/late,各自应用 `directional_*_min_edge` 闸与 `*_score_mult`;score 按规格公式,替换 `edge*0.7+conf*30`。
2. **费用闸**:`min_execution_adjusted_edge_percent` 默认 0→**0.5**;`net_edge = edge − fee_aware_min_edge_pct(entry_price, multiplier=2.0)`;`skip_fee_model=True` 移除或注明由本闸替代。
3. **阈值收敛与重定标**:`min_edge_percent` 3.0→**1.5**(量纲已变概率点);detect 的 `min_oracle_move_pct` 降为粗筛(0.15,per-tf 可覆盖,入 default_config);删 evaluate 里第二份 0.15 与 `quality_filter_overrides(min_roi=1.0)` 的量纲冲突。
4. **修活 guardrail**:detect 把 `model_prob_yes/no`、`up_price/down_price` 写入 `_crypto_context`,使方向 guardrail 真正生效。
5. **per-tf 门槛**(走 G-0)、**sizing**(删反向 edge hack 常数,改 `StrategySDK.fractional_kelly_size` quarter-Kelly,基于 p_win vs entry_price,受 max_size 与风控 cap 约束)、**熔断入 config**、**热路径**(decision_payload 懒构建、asset/timeframe 按 market_id 缓存、`get_book_imbalance` memo、`_edge_first_seen_ms` TTL 清理)、**死代码 `_direction_allowed` elapsed 机器**(接线或删除,产出文件说明)。

**验收**:`directional_*` 全 key grep ≥2;单测:p_win 单调性与衰减、费用闸拒/过边界、Kelly 尺寸数值表、`decision_payload` 在 skip 路径不构建(mock 计数)。

## G-2 `btc_eth_convergence.py`(实现文档宣称的收敛模式)

⚠️ **行为大改**:旧 latency 行为由 directional 承担。规格书自身警告"若线上依赖旧 convergence 行为,合并前先 shadow 对照"。**实现照做,但必须在产出文件顶部显著标注这是行为变更**,由操作员决定 shadow 时长。

按 WO-B2 的 1–7 项实施:接线 `convergence_*` 全块(入场窗口 `seconds_left ∈ [min,max]`、entry ∈ `[min_price, max_entry_price]`、`|oracle_diff_pct| ≥ min`;**方向 = oracle 有利侧**)、移除 cheap-side 逻辑、**oracle 新鲜度硬闸**(修 fail-open:age 缺失视为不新鲜→拒)、出场加 `exit_on_oracle_flip=True`、评分与费用(按曲线计入 edge,移除 `skip_fee_model`)、Kelly sizing、以及同 G-1 的熔断/懒 snapshot/缓存 + `_market_ml_probability_yes` 单侧先验改对称。

**验收**:场景单测 (a) 剩 30s、favored 0.90、fresh、diff≥min → emit 且方向=favored;(b) 剩 300s → 不 emit;(c) oracle age 缺/超限 → 不 emit;(d) 持仓中 diff 翻转 → flatten。`convergence_*` 全 key grep ≥2。

## G-3 `btc_eth_maker_quote.py`

按 WO-B3 的 1–5 项实施:接线 `maker_quote_*` 全块(min_spread 主闸、**skew** `s = clamp(oracle_diff_pct/skew_scale, -1, 1) * maker_quote_skew_max` 且新 key `skew_scale=1.0`、腿尺寸 clamp、`min_seconds_left` 与 `session_timeout = min(cfg, seconds_left − resolution_risk_seconds)`、评分公式)、**组合成本闸**新 key `maker_min_combined_edge=0.015`(替换 0.998 硬数,补腿跨价成本按费用曲线预估)、**库存一致性**(`max_unhedged_notional_usd` 同步派生、`hedge_timeout_by_timeframe`)、oracle_direction gate 对 maker 启用、`reentry_cooldown_seconds_per_market` 0→5、两处 `min_oracle_move_pct` 默认统一、熔断入 config、热路径。

💡 **maker 侧费用提醒**:fix-03 提供了显式 `polymarket_maker_fee`(=0)。maker 报价成本估算**不要**用 taker 费高估;补腿若需跨价吃单,那一腿才用 taker 费。

**验收**:skew 单测(cap、方向、加权和=1);组合成本闸边界;seconds_left 撤单窗口;`maker_quote_*` 全 key grep ≥2。

## G-4 五个小策略

按 WO-B4 的 1–5 项实施(`crypto_5m_midcycle`、`crypto_digital_sigma_edge`、`crypto_spike_reversion`、`crypto_entropy_maker`、`crypto_distance_edge`)。要点回顾:
- **midcycle**:真费用(删 `fee=0.0`/`skip_fee_model`)、`win_prob_estimate` 入 config 且优先用 `prob_above`、CycleTracker 300.0 常数 → `timeframe_seconds` 推导。
- **digital_sigma**:平坦 `fee_buffer=0.015` → 每侧真实 `taker_fee_pct`(`fee_buffer` 保留默认 **0.0** 作额外 margin,schema 说明语义变化)、oracle age 走 per-tf helper、`_realized_vol_per_sec` 缓存、`recent_move_zscore` 缺失的 fail-open 改为按配置拒或降 confidence。
- **spike_reversion**:补显式 `edge ≥ min_edge_percent` 闸、rejection 单遍化、confidence clamp 0.44→0.30、`liquidity_cap_fraction` 硬数改读 config。
- **entropy_maker**:`min_entropy` 0.82→**0.0**(注释数学互斥:H(0.80)=0.722 与 min_entry_price=0.80 不可同真)、clamp 0.40→0.30、rejection 单遍化、三个"软加分"参数 schema 注明"bonus 阈,非过滤器"。
- **distance_edge**:新 key `distance_cost_tiers_bps`(优先于 USD tiers)、**EV 闸**(有 oracle_history 用 `prob_above`,要求 `EV>0` 才 emit)、`min_seconds_to_resolution` 10→60(走 helper)。

**验收**:每策略 2–4 个单测;费用统一 grep(全部经 kelly 曲线);midcycle/digital 无 `skip_fee_model`/平坦 buffer 残留。

---

## 完成判据

- G-0 先行,G-1/G-2/G-3 可并行(不同文件),G-4 最后。
- 每工单单测绿;全 key grep ≥2(死配置清零,重点三块前缀);无新依赖;`py_compile` 通过。
- **默认值变更表**(旧→新→理由)写入产出文件 —— 本批变更多,这张表是 shadow A/B 的依据。
- **G-2 的行为变更**在产出文件顶部显著标注。
- 产出 `.piercode/fix-05-last-message.md`:逐条 `文件:行` + 改动 + 为什么 + 测试证据 + 默认值变更表 + 未验证项。
- **按工单分次提交**(G-0…G-4 各一组 commit,前缀工单号)。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交并推送;push 前 `git pull --rebase`。

## 交给人的后续(AI 侧无法自证)

规格书全局验收清单里这两条**不属于本 spec 的完成判据**,需操作员执行:
1. 合并后同步 DB `strategies.source_code`(策略文件是 seed template,不同步则线上不生效;只推代码、不覆盖用户已调参数)。
2. Shadow A/B ≥3 天,对照信号量、选择率、per-strategy PnL、拒单原因分布(修复后 `rejection_reason` 才可信)。
