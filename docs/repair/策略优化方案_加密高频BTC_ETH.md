# Homerun 交易策略优化方案 — 加密高频（BTC/ETH）

范围：`btc_eth_directional_edge`、`btc_eth_convergence`、`btc_eth_maker_quote`（三个大文件，各 ~4500 行，内部含 `SubStrategy` 枚举）+ 小型加密策略 `crypto_5m_midcycle`、`crypto_spike_reversion`、`crypto_digital_sigma_edge`、`crypto_distance_edge`、`crypto_entropy_maker` + 共享层 `crypto_strategy_utils.py` / `strategy_helpers/crypto_strategy_utils.py`。
目标：①参数/收益调优 ②逻辑/edge 增强 ③执行/延迟。**本轮为设计/方案，不改代码。**
标 ✅ 的论断已逐条对源码核验（含数值验证）。行号基于 seed 模板，线上 DB `source_code` 可能有漂移。

---

## 0. 前置（和上一份一致，但对加密这块尤其致命）

这些文件都是 **SEED TEMPLATE**，线上跑的是 DB `strategies.source_code`。加密这几个策略的一个共性问题是：**default_config 里暴露给运营者的很多旋钮，代码根本不读**（下面 §1 详列并已核验）。这意味着有人在 UI 里调参，很可能在调一堆无效开关，真正生效的阈值反而藏在代码里的 inline fallback（不在 UI 上）。所以对加密这块，「优化」的第一步不是调数值，而是**先让配置面板和代码行为对上**。

---

## 1. 头号问题：广告的模型没实现，核心旋钮是死的（全部 ✅ 核验）

这一节是整套加密策略里最高价值的发现。**不是参数不够好，而是策略实际在做的事和它宣称/暴露的旋钮对不上。**

### 1.1 directional_edge：宣传的概率模型不存在，edge 是原始 oracle 位移
- docstring（`btc_eth_directional_edge.py:11-19,522-530`）宣称「用 diff% 经衰减 sigmoid 转成 Up/Down 概率，按 EARLY/MID/LATE 阶段收紧 edge 门槛」。**代码里没有这个 sigmoid**：`model_prob_yes/no` 是从 payload 里读、默认 0.5（`:1745-1746`），`edge_percent = oracle_move_pct`（`:4263`），即一个 0.15–3% 的**资产价格位移**，不是概率 edge。
- 整个 `directional_*` 配置块（`:615-631`）在文件里**每个 key 只出现一次**（仅定义，从不读取）✅。运营者调 `directional_base_scale` / `directional_early_min_edge` / `directional_edge_score_scale` / 阶段 bonus，**零效果**。
- 连带 bug：`min_edge_percent` 默认 **3.0**（`:1135`）是拿来卡一个 0.15–3% 位移量的——尺度对不上，几乎会拒掉所有真实 oracle 位移，除非 DB 里把它压到很低。正确性完全依赖一个文件外的 DB override，很脆。

### 1.2 convergence：名不副实，实际是 oracle 延迟套利
- docstring（`btc_eth_convergence.py:6-14,522-532`）描述的是「临到期 5–45 秒、在 0.85–0.95 买赢家 side 吃收敛」。**代码做的正相反**：从当前 up/down 价定方向、买**便宜** side（`:4199-4204`），并拒绝已重定价到 ~0.80 以上的市场（`repricing_ceiling = 0.50 + max_repricing(0.30)`，`:4152-4171`）；且 evaluate 要求**大量剩余时间**（`min_seconds_left = {5m:45,15m:180,1h:360,4h:900}`，`:1360-1379`），和「最后 5–45 秒」完全相反。
- 七个 `convergence_*` 旋钮（`:601-607`）**每个只出现一次**、全是死配置 ✅。
- 实际 edge = oracle 与 strike 的背离（`edge_percent = oracle_move_pct`，`:4218-4231`），**没有任何均值回归/收敛半衰期模型**。名字叫 convergence，跑的是 latency-arb。

### 1.3 maker_quote：没有 skew（对做市商这是命门），且用了反向的门槛
- docstring 宣称「向预测赢家 skew、保持库存中性」（`btc_eth_maker_quote.py:12-13,515-517,524`）。**代码里 "skew" 只出现在注释/描述里**（`:12,516,524`），两腿报价完全对称（notional 0.5/0.5，各减 1 个 tick，`:789-790,850-869`）✅。对一个挂单做市商来说，「向赢家 skew」正是**防被逆向选择打穿**的核心保护——现在完全没有。
- `maker_quote_*` 整块（`:585-596`，含 `maker_quote_min_spread`/`maker_quote_skew_max`/`maker_quote_min_seconds_left` 等 12 个）**每个只出现一次**、全死 ✅。运营者以为在调做市点差/skew/临到期撤单，实际零效果。
- `min_edge_percent=3.0`（`:1101`）对做市商是**反的**：做市商应按**点差 + rebate**报价，而不是要求 2.5% 的方向性 edge——这个门槛只会让它在大 oracle 位移（对挂单最毒的时刻）才挂单。

### 1.4 三大文件其实是同一套引擎，只是 mode 被 pin 死
三个文件都含 `SubStrategy = {MAKER_QUOTE, DIRECTIONAL_EDGE, CONVERGENCE}`，但各自把 `active_mode` 硬写死（directional `:1215-1219`、convergence `:1184-1186`、maker `:1180-1185`）。所以每个文件里另外两个 mode 的整块代码（如 maker plan builder）都是死代码。**本质是一套 oracle 延迟套利引擎的三个 pin。** 这对优化是好消息：核心 edge 逻辑修一处、三处受益。

### 1.5 entropy_maker：两个默认门槛数学上互斥，默认几乎不出信号 ✅
- `min_entropy=0.82`（`crypto_entropy_maker.py:75`）要求 `prob ∈ [0.256, 0.744]`；`min_entry_price=0.80`（`:98`）要求成交 side 价格 ≥0.80（即 prob≈0.80，H(0.80)=0.722 < 0.82）。**两者不可能同时满足**（数值已验证）。默认参数下这个策略基本不产出机会。`min_entry_price` 的改动理由（`:86-97` 注释「只有 0.80+ 才盈利」）和 entropy 门槛直接打架——**得二选一**。

---

## 2. P1 — edge / 参数调优（收益导向，需接数据回测验证）

### 2.1 费用感知：普遍缺失或用了错误的平坦近似
预测市场 taker 费是**强价格相关**的：`polymarket_taker_fee_pct(p) = 0.25·(p(1-p))²`（`utils/kelly.py:81`）。在 0.92 价位只有 **0.135%**，在 0.50 价位约 **1.56%**。现状：
- `crypto_5m_midcycle`：**完全不算费**（`skip_fee_model=True`/`fee=0.0`，`:560/259`），却在 0.70 价位交易——EV 高估。
- `crypto_digital_sigma_edge`：用**平坦 `fee_buffer=0.015`**（`:110,476-477`）两边都减 1.5% ✅。它专做 favorites 到 0.92（`:497`），等于给 favorite 多扣 ~1.3pp，把真正 +EV 的单拒掉。
- directional/convergence/maker：`skip_fee_model=True` + `min_execution_adjusted_edge_percent=0.0`，`execution_edge` 门槛（`net_edge≥0`）恒过。
- 反例（做对了的）：`crypto_spike_reversion`、`crypto_entropy_maker`、`crypto_distance_edge` 都调了真实曲线。
- **方案**：所有加密策略统一用 `polymarket_taker_fee_pct(price)` 按 side 扣费，删掉平坦 `fee_buffer` 和 `skip_fee_model`；directional/convergence/maker 设一个真实的 fee 倍数门槛（≈2× fee）。这是本轮**性价比最高、且部分无需回测就能判定为纠错**的一类。

### 2.2 阈值没有按 波动率 / 时间框架 / 资产 缩放（一刀切）
- `min_oracle_move_pct=0.30%` 单标量跨 BTC/XRP、跨 5m/4h（directional `:4147`、convergence `:4114`）——BTC 波动 ≪ XRP，0.3% 在 4h 是噪声、在 5m 是决定性。
- `crypto_5m_midcycle` 的 `min_distance_bps=15`（`:84`）资产无关；`crypto_spike_reversion` 的 `min_abs_move_5m=1.8%`（`:70`）对 BTC 巨大、对小 alt 正常。
- `crypto_distance_edge` 用**美元**阈值（150/200/300 → 90/85/80¢，`:94-98`），绑死 BTC 且随 BTC 价格 regime 漂移（$120k vs $60k 时「$150 in the money」是完全不同的 bps）。
- **方案**：阈值改成**波动率归一化的 z-score** 或 **bps**（`distance_edge` 直接从美元改 bps，顺带解绑 BTC-only），并按 `sqrt(seconds_left)` 缩放（剩余时间越短、同样位移越难反转）。每个 `min_oracle_move_pct` 按 asset×timeframe 播种进 default_config。

### 2.3 edge 不是概率，且在 directional/convergence 里是「反预测」的
- directional/convergence 的 `edge_percent = oracle_move_pct`（原始位移），不是 `P(win) - price`。
- 强证据：convergence 有一段**反向 sizing** hack——注释直说「reported edge 越高，结果越差」，于是 `edge_excess>15%` 时反而**缩小**仓位（`:2812-2822`）。这说明 `edge_percent` 字段在高位区**反预测**，是建模问题不是 sizing 问题。
- **范本**：`crypto_digital_sigma_edge` 是五个小策略里**唯一做对的**——它用 `prob_above(spot,strike,sigma,seconds_left)`（零漂移 GBM 的 `N(d2)`，`strategy_sdk.py:3424`）算真实数字期权公允价，再和可执行 book 比。
- **方案**：directional/convergence 也用 `bounded_sigmoid`（`strategies/crypto_strategy_utils.py:105`）或 `prob_above` 建 `P(win)`，edge 改成 `P(win) - entry_price`，scale 随窗口老化衰减（正是那套死掉的 `directional_*` 阶段配置本想编码的东西）。这样能把 §2.3 的反向 sizing hack 整个删掉。

### 2.4 sizing 与不确定性脱节
- directional/convergence/maker 的 `conf_boost = 0.8 + conf·0.8`（如 convergence `:2822`），而 confidence 本身近乎常数（`0.55 + 小项`，clamp `[0.55,0.92]`）→ 全区间 sizing 只有 ~1.4× 摆动，**几乎是平的**。
- **方案**：用**校准后**的 `P(win)` 对 `price` 做分数 Kelly（SDK 已有 `fractional_kelly_size`，quarter-Kelly），让仓位真正反映 edge/方差。`crypto_spike_reversion` 已经用 `kelly`（`:75-76`）可作参照。

### 2.5 一批「看起来是门槛、其实恒过」的 inert 闸门（✅ 核验）
- `min_confidence` 恒过：`spike` 置信度 clamp 到 `[0.44,0.90]`、门槛 ≥0.44（`:302,305-306`）；`entropy` clamp 到 `[0.40,0.92]`、门槛 ≥0.40（`:434/446,447`）——**默认下永不触发** ✅。方案：把默认门槛提到 clamp 下限之上，或降低 clamp 下限。
- `crypto_spike_reversion` 的 `min_edge_percent=2.8`（`:68`）在**检测路径 `_score_market` 里根本没被检查**（只在 `_rejection_reason:563` 和 `evaluate:731` 检查）——实际入场只有 fee-clearance 那道（`:356-357`），在 0.92 价位只 ~0.27%。方案：在 `_score_market` 补一道显式 `edge >= min_edge_percent`。
- directional 的 direction guardrail（要求 `model_prob_yes≥0.55`）因 detect 从不写 `model_prob_yes` 进 payload、默认 0.5，**恒不触发**（`:1141-1148,2071-2091`）；`execution_edge`（`net_edge≥0`）因门槛默认 0.0 恒过。
- **方案**：这些闸门要么接上真实字段让它生效，要么删掉——现在它们给人「有风控」的错觉。

### 2.6 exit / 临到期时机
- directional/convergence 对 5m/15m 默认切到 `binary_resolution_hold`，**关掉所有中途止损**、只等 85%/70% 止盈或强平（如 convergence `:3100-3140`）。对真收敛仓位说得通，但对 **latency 仓位**很危险——oracle 前提中途反转时，会抱着一个必输 binary 一路到 $0。
- **方案**：即使在 binary-hold 下，也保留一道 **oracle 反转出场**（当前 `sign(diff)` 翻转就平）。

---

## 3. P2 — 执行 / 延迟

### 3.1 decision snapshot 在 skip 时也全量序列化（三大文件共有，热路径最大浪费）
三大策略的 `evaluate()` 都在**分支判断之前**、对 `params/payload/live_market/oracle_status` 整体 `_json_safe` 深序列化构建 `decision_snapshot`（directional `:2643-2829`、convergence `:2779-2796`、maker `:2790-2794`），**skip 的信号（绝大多数）也照付这份开销**。对 HF 是实打实的每 tick 成本。**方案**：改成**懒构建**——只在 selected 或需要 firehose 详情时才序列化。

### 3.2 每 tick 阻塞 SDK 调用、无 memoize
`get_book_imbalance`（evaluate 每 tick）、`get_price_history(24)` + `get_buy_sell_imbalance`（exit 每 tick）在三大文件都无缓存（如 directional `:1616,3377-3400`）。**方案**：按 `(token_id, tick)` memoize；在 directional-only fork 里，orderflow/cancel 那几道本是给 maker mode 用的，可直接短路。

### 3.3 detect / reject 双重计算且已漂移（spike / entropy）
`_score_market` 之后，每个被拒的 row 又跑一遍 `_rejection_reason` 重算 oracle/edge/confidence/shape（spike `:113→497`、entropy `:134→587`）。且 reject 路径用**裸 `row.oracle_price`** 而非 `pick_oracle_source`，spike 还**硬编码 300s 周期**（`:545`）——非 5m 市场的 `elapsed_ratio` 算错，导致**拒单原因标签本身是错的**（既是热路径浪费、又是可观测性 bug，每个被拒 row 都跑、上限 24/event）。**方案**：拒单原因从单次 scoring pass 里导出，别重算。

### 3.4 其它热路径重算
- directional/convergence 每 tick 对每个市场跑 `_detect_timeframe`（正则 `_SLUG_REGEX.search`）/`_detect_asset`，而 asset/timeframe 是**每市场静态**的（directional `:4077-4082`）。方案：按 `market_id` 缓存。
- `crypto_digital_sigma_edge` 的 `_realized_vol_per_sec`（`:399`）每 tick 对每个窗口内市场重算 ~120 点 sigma，而 sigma 在一个周期内几乎不变。方案：按 `(market_id, history_len)` 缓存。
- 多处一次 evaluate 内重复 `utcnow()/int(...*1000)`——算一次即可。

---

## 4. 跨策略「修一次、多处受益」清单

1. **统一费用 helper**：`crypto_strategy_utils.taker_fee_pct`（`:100-102`）和 `utils/kelly.polymarket_taker_fee_pct`（`:81`）是**逐字节相同**的两份实现，distance 用前者、spike/entropy 用后者、midcycle/digital 都不用——收敛到一份，避免漂移。
2. **统一 timeframe-aware 安全 helper**：`default_max_oracle_age_ms`/`default_max_market_data_age_ms`/`default_min_seconds_left_for_entry`（helper `:344/359/375`）已存在，但 midcycle（平坦 5000）、distance（平坦 5000 + 10s 入场底线 vs helper 的 60s）、digital（平坦 4000，却同时跑 5m+15m）、三大文件（oracle-age 平坦 12s）**全都在用自己的一套 inline 值**，且和 helper 不一致（dual/triple source of truth）。全部路由到 helper。
3. **统一 confidence clamp/default 约定**：clamp 下限必须**低于**默认门槛，否则门槛恒过（§2.5）。作为一条编码约定一次性修掉。
4. **死配置总清算**：`directional_*` / `convergence_*` / `maker_quote_*` 三整块（已核验全死）——**要么接线、要么删**。留着就是误导性的控制面板。

---

## 5. 建议优先级 + 回测验证路径

**第一梯队（部分是纠错，不需回测就能判定；且是三大策略共用引擎，改一处多处受益）**
1. §1 对齐「配置面板 ↔ 代码行为」：先把三大块死配置和广告模型的落差处理掉（接线或删 + 文档纠正），否则后续任何调参都在盲调。
2. §2.1 全面费用感知 + §2.5 修 inert 闸门：这些是明确的建模/纠错，风险低。
3. §1.5 entropy_maker 的 `min_entropy` vs `min_entry_price` 二选一：否则该策略默认近乎不工作。

**第二梯队（收益假设，必须接数据 A/B）**
4. §2.3 把 directional/convergence 的 edge 换成真实 `P(win)-price`（以 digital_sigma 为范本），删掉反向 sizing hack。
5. §2.2 阈值波动率/bps/时间归一化；§2.4 校准 Kelly sizing；§2.6 oracle 反转出场。
   - 验证：对每个策略先用 `run_execution_backtest`（`settle_resolution_allow_network=False`）跑基线，再用 `run_parameter_sweep(param_grid, train_ratio)` 扫新阈值、`run_walk_forward(mode="anchored", n_folds=6)` 验稳健性。

**第三梯队（执行/延迟，逻辑稳定后再做）**
6. §3.1 懒构建 decision snapshot（三大文件热路径最大浪费）；§3.2–3.4 memoize + 消除 detect/reject 双算。

---

## 附：本方案与另一个会话/上一份文档的关系
- 上一份《信号/新闻/跟单类》已交付；跟单类的 P0（copy_delay 死锁、假 edge 等）在**另一个会话**修。
- 本文档为**加密高频**的设计方案，不含代码改动。三大策略「同一引擎、三处 pin」的结构意味着核心 edge/费用/热路径修复应做成**共享层**（`strategy_helpers/crypto_strategy_utils.py`）一次落地，再让三个 mode 复用。
