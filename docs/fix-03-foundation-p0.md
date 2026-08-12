# 修复 spec:fix-03 地基级 P0(费用曲线 + 交易员 source_flags)

> 基座:`65241946/homerun` 分支 `claude/batch-2-identity-finality-review-jvcsc5`(归一后主干,含 fix-01/fix-02 + 策略会话规格 + markout)
> 两项互不冲突(不同文件),可并行实现。都是**地基**:不修则下游所有 EV 判断与所有交易员回测都不可信。
> 每项必须带回归测试。改完在此分支提交并推送,由架构师从 GitHub 审查。

---

## 通则(硬约束)

- 只改下列点名的文件 + 对应测试;不做无关重构。
- 单一真值源:任何 `params.get(key, 魔法数)` 的 inline 回退必须与 `default_config` 一致(沿用 fix-02 的做法)。
- 删除即删干净:不留兼容 shim / tombstone;删常量前 grep 全仓确认无引用。**例外**:P0-a 明确要求保留 legacy 函数(见下),那是显式契约,不算 tombstone。
- 纯逻辑测试免 DB;需要 DB 的真起 Postgres,不得用 skip 冒充。
- 产出 `.piercode/fix-03-last-message.md`:逐条 `文件:行` + 改动 + 为什么 + 测试证据 + 未验证项。

---

## P0-a 费用曲线过时(WO-B0 的地基部分)

**文件**:`backend/utils/kelly.py`(+ `backend/services/strategies/crypto_strategy_utils.py` 的委托)

**根因(架构师已核实)**:`kelly.py:78` 的 `polymarket_taker_fee` 用 `p · 0.25 · (p(1−p))²` —— 2026 年前的四次曲线。现行制度是二次:`fee = C × feeRate × p × (1−p)`。
量级对照(p=0.50,crypto feeRate=0.07):旧 = 1.56% / 新 = 3.50%,**低估 ~2.2 倍**;p=0.90 处旧 0.20% / 新 0.63%,**低估 ~3 倍**。
调用面:`services/strategies/base.py`、`ctf_basic_arb`、`vpin_toxicity`、`btc_eth_directional_edge`、`btc_eth_maker_quote`、`services/backtest/matching_engine.py`、`intent_runtime.py`、`market_runtime.py` 等 → **全系统费后 EV 闸门系统性高估 edge**。

**修复**:
1. 新增制度可配的费用函数,费率**按类目**(v1.1 规格):
   ```
   FEE_RATE_BY_CATEGORY = {"crypto": 0.07, "sports": 0.05, "politics": 0.04, "geopolitics": 0.0}
   DEFAULT_FEE_RATE = 0.07          # 未知类目取最保守(最高)费率
   polymarket_taker_fee(p, *, category=None, fee_rate=None) -> C × feeRate × p × (1−p)   # C=1 share
   ```
   - `fee_rate` 显式传入时优先;否则按 `category` 查表;都没有 → `DEFAULT_FEE_RATE`。
   - **未知类目必须取最高费率**(保守),不得取 0。
   - maker 侧零费:新增 `polymarket_maker_fee(...) -> 0.0`(供做市路径显式调用,避免用 taker 费高估做市成本)。
2. **保留** legacy 四次曲线为 `polymarket_taker_fee_legacy_quartic(p)`,docstring 注明"2026-01 前制度,仅供历史回测对照,新代码不得调用"。这是显式契约,不是 tombstone。
3. `polymarket_taker_fee_pct` 同步走新曲线,签名透传 `category`/`fee_rate`。
4. `crypto_strategy_utils.taker_fee_pct`(约 :100-102)改为委托 `utils.kelly.polymarket_taker_fee_pct`,删除重复公式体(WO-B0 第 1 条)。
5. **调用点排查**:grep 全部调用者,凡能拿到市场类目的,把 `category` 传下去;拿不到的走 `DEFAULT_FEE_RATE` 并在该处留一行注释说明为何没有类目。**在产出文件里列出每个调用点的处理方式**。

**回归测试**(`backend/tests/test_kelly_fee_regime.py`):
- 数值表:crypto(0.07)在 p=0.10/0.50/0.90 的费用与手算一致;p=0/1 → 0。
- 类目差异:同 p 下 crypto > sports > politics > geopolitics(=0)。
- 未知类目 → 等于 DEFAULT_FEE_RATE(最保守),**不等于 0**。
- legacy 函数仍返回旧四次曲线值(证明只作对照、未被误用)。
- `crypto_strategy_utils.taker_fee_pct` 与 `kelly.polymarket_taker_fee_pct` 对同一输入结果一致(委托生效)。

**注意**:实施当日应对照 Polymarket/Kalshi 官方费率页核对数字(规格 v1.1 要求)。若官方值与上表不符,**以官方为准并在产出文件记录**,不要沿用本 spec 的数字硬写。

## P0-b 交易员 `source_flags` 恒 False → 回测/UI 恒 0 机会

**文件**:`backend/services/strategies/traders_confluence.py`、`backend/services/trader_data_access.py`、`backend/services/traders_firehose_pipeline.py`

**根因(架构师已核实)**:
- `source_flags`(`from_pool`/`from_tracked_traders`/`qualified`)**只由** `trader_data_access.annotate_trader_signal_source_context` 附加,而它**只在 `include_source_context=True` 时运行**(`trader_data_access.py:246`)。原始 firehose 输出不含该字段。
- `evaluate_firehose_signal`(`traders_confluence.py:338`)有两道闸读它:
  - `unqualified_wallet_source`(:376-381,`qualified` 缺省 True 但 normalize 总会写键)
  - `source_scope_mismatch`(:383-395,scope="all" 需 `from_tracked ∨ from_pool`,**无缺省保护**)
- `StrategySDK.normalize_trader_source_flags(None)` → 三个键**全 False**(`strategy_sdk.py:1387` 的 derive 在全 None 输入下 `pool_flag=False, tracked_flag=False, qualified=False`)。
- 于是 `include_source_context=False` 的路径**每一行都被拒**。受害调用方:
  - `services/traders_firehose_pipeline.py:192`(→ `get_strategy_trader_opportunities`,UI/API)
  - `services/strategies/traders_confluence.py:726`(`detect_async`)
  - `services/strategy_backtester.py:255`(**回测器**)
  - `services/traders_copy_trade_signal_service.py:590`
- 净效果:**交易员策略在回测中恒零机会、UI/服务侧恒零**;只有 worker 主路径(True)能出信号。这使任何跟单/共识策略的回测结论都是空的。

**修复(默认口径:让 provenance 在所有路径都可得)**:
1. 把 provenance 附加从"调用方可选"改为**默认发生**:`trader_data_access.get_trader_firehose_signals` 的 `include_source_context` 默认保持 True,并把两个显式传 `False` 的调用点(`traders_firehose_pipeline.py:192`、`traders_confluence.py:726`)**改为不传/传 True**,让它们也拿到真实 provenance。
   - 若 `annotate_trader_signal_source_context` 有明显性能成本(它会查库),在产出文件里说明成本与实测耗时;**不要**为了省这点成本而保留一条恒零的死路径。
2. 兜底防御:`evaluate_firehose_signal` 的 `source_scope_mismatch` 闸,当 signal **完全没有** provenance 信息时(三个 flag 键都缺失,而非显式 False),不得静默判负 —— 记一条明确的 reason(如 `missing_source_provenance`)以便定位,而不是与"确实不合格"混为一谈。
3. 顺带修 fix-02 审查已记录的**死缺省**(`docs/fix-02-audit.md`):
   - `min_tier` inline 缺省 `"high"`(:425)与 `DEFAULT_CONFIG` 的 `"low"`(:81)不一致 → 统一读 `DEFAULT_CONFIG`。
   - `firehose_exclude_crypto_markets` inline 缺省 `True`(:408)与声明 `False`(:88)相反 → 统一读 `DEFAULT_CONFIG`。
   - `min_confluence_strength`:`custom_checks` 用 0.55、`DEFAULT_CONFIG` 是 0.50 → 统一。

**回归测试**(`backend/tests/test_traders_firehose_provenance.py`):
- 给一行**带真实 provenance**(from_pool=True)的 firehose 行 → `evaluate_firehose_signal` 通过(不再 `source_scope_mismatch`)。
- 给一行**无 provenance 键**的行 → 拒绝理由是 `missing_source_provenance`(可定位),而非与合格判负混淆。
- **端到端**:`get_strategy_filtered_trader_opportunities` 在有合格钱包数据时返回 **> 0** 行(旧代码恒 0)——这是本修复的核心判据。
- 三个死缺省:改 config 的 `min_tier`/`firehose_exclude_crypto_markets`/`min_confluence_strength` 后行为随之改变(证明 inline 缺省不再暗中抵消)。

---

## 完成判据

- 两项都改 + 各带回归测试,`pytest` 相关用例绿。
- P0-a:列出每个费用调用点的类目处理方式;官方费率核对结果写入产出文件。
- P0-b:端到端断言 `> 0` 机会成立(证明恒零路径已修复)。
- 全仓 grep 确认无残留误用(尤其没有新代码调用 legacy 四次曲线)。
- `.piercode/fix-03-last-message.md` 写清每处改动 + 测试证据 + 未验证项。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交并推送;**不要碰 main**;push 前先 `git pull --rebase`(该分支有多方并发推送)。
