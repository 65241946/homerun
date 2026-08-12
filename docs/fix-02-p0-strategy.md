# 修复 spec:fix-02 策略层 P0 正确性批次

> 基座:`65241946/homerun` 分支 `claude/batch-2-identity-finality-review-jvcsc5`(干净基座 389d246 + fix-01)
> 本 spec 是给实现方(codex)的方案。5 个 P0 都是**明确正确性 bug**,不需回测即可判定。
> 每个修复**必须带回归测试**;改完在此分支提交并推送,由审查方从 GitHub review。

---

## 通则(硬约束)

- 只改下列点名的文件 + 对应测试。不做无关重构、不动 fix-01 已改的编排/结算文件。
- 删除即删干净(agents.md 原则):不留兼容 shim、不留被替换的旧常量当 tombstone;删一个常量要 grep 全仓确认无其它引用。
- 每个修复配**回归测试**;策略逻辑多为纯函数,能不起 DB 就不起。跑 `python -m pytest backend/tests/ -k "<相关>"` 绿。
- 产出 `.piercode/fix-02-last-message.md`:逐条列改动 `文件:行` + 为什么 + 测试证据 + 未验证项。

---

## P0-1 跟单 `copy_delay` 与 `max_age` 互斥

**文件**:`backend/services/strategies/traders_copy_trade.py`

**根因**:两道闸是 `copy_delay_seconds ≤ age_seconds ≤ max_signal_age_seconds`(闸见 ~行 789-803,`max_signal_age_seconds` 算于行 636 = `min(hard_ceiling, requested)`,默认 requested=5s;`copy_delay_seconds` 行 682,UI 可设到 300s)。一旦 `copy_delay > max_signal_age`,窗口为空 → 100% 静默拒单,策略失效且无提示。

**修复(语义:延迟平移窗口,新鲜度叠加在延迟之后)**:
- 新鲜度上限改为 `effective_max_age = min(hard_ceiling, copy_delay_seconds + requested_max_signal_age_seconds)`。
- "max_age" 闸用 `age_seconds ≤ effective_max_age`(替换行 ~791 对 `max_signal_age_seconds` 的比较),detail 文本同步显示 effective 值。
- "copy_delay" 闸保持 `age_seconds ≥ copy_delay_seconds` 不变。
- 有效窗口变为 `[copy_delay, copy_delay + max_signal_age]` ∩ hard_ceiling。
- 参数校验处(~行 218):若 `copy_delay_seconds ≥ hard_ceiling`(窗口恒空的病态配置),记一条显式 warning/event(不要静默)。

**回归测试**:构造 `copy_delay=30, max_signal_age=5, age=32` → 通过(旧代码会拒);`age=40`(> 30+5)→ 拒;`copy_delay=0` 时行为与旧一致。

## P0-2 跟单 edge 公式 = 离 0.5 距离 → 改期望值(EV)

**文件**:`backend/services/strategies/traders_copy_trade.py`(行 369-376 附近)

**根因**:`edge_percent = abs(edge_midpoint − entry_price) * edge_multiplier`(midpoint=0.5, mult=200),对 0.5 对称:price=0.99 与 0.01 同为 98,虚高 ROI 污染 `roi_percent`(行 462)排序与 sizing。

**修复(EV 口径,已获操作员确认)**:用已有的 leader 置信度 `confidence`(行 360,默认 0.70)算期望值:
```
edge_ev = confidence - entry_price                       # 每 $1 份额的期望收益
edge_percent = max(0.0, edge_ev / entry_price * 100.0) if entry_price > 0 else 0.0
expected_payout = min(1.0, max(0.0, confidence))         # 期望结算赔付
```
- `roi_percent = edge_percent` 赋值点不变,值改为上式。
- **删除**现已无用的 `edge_midpoint` / `edge_multiplier`:config 默认(行 59-60)、SDK 参数定义(行 107-122)、白名单(行 197-198)、coerce(行 243-244)。先 grep 全仓确认这两个键无其它引用再删。
- 效果:price=0.99 conf=0.70 → 负 EV → edge=0(不再高排名);price=0.01 conf=0.70 → 反映真实赔率。

**回归测试**:断言 0.99 与 0.01 的 edge_percent **不再相等**;0.99/conf0.70 → edge_percent==0;低价高赔率样本 edge_percent 明显更高;expected_payout==confidence(clamp)。

## P0-3a certainty_shock 子串排除加密市场

**文件**:`backend/services/strategies/certainty_shock.py`(行 257)

**根因**:`if kw in text:` 纯子串匹配,`"eth"` 命中 "whether"/"weather"、`"sol"` 命中 "solution"/"console",误排非加密市场。

**修复**:改**词边界匹配**。预编译:对每个排除词 `re.compile(r"\b" + re.escape(kw) + r"\b")`,匹配 `pattern.search(text)`。保持大小写不敏感(text 已 `.lower()`,行 254 区)。

**回归测试**:`exclude=["eth","sol"]`;"Will it rain whether..." **不**被排除;"ETH above $4000?" 被排除;"Solana price..." 被排除、"solution summit..." 不被排除。

## P0-3b/3c certainty_shock 检测 6h 漂移(非冲击)+ 自述 0 胜 —— 修

**文件**:`backend/services/strategies/certainty_shock.py`(行 78 默认;行 228 读取)

**根因**:`shock_lookback_seconds` 默认 21600(6 小时),使一段**慢漂移** 0.22 也算"冲击";该策略历史 0 胜。

**修复(操作员选"修")**:把 shock 检测窗口从 6h 收到分钟级真·冲击:
- 默认 `shock_lookback_seconds` 21600 → **900**(15 分钟)。这是纠正"漂移≠冲击"的起点默认;精确值后续 P1 回测调。
- 其余 shock 参数(min_abs_move=0.22 等)不动。
- 与 3a 一起:排除词边界化后,加密剔除更准。

**回归测试**:同一条 0.22 的价格移动,若发生在 6h 内(慢漂移)→ **不**触发;若集中在 ≤15min 内 → 触发。断言默认 lookback==900。

## P0-4 检测阈值 vs evaluate 回退阈值两套数

**文件**:多个策略(`backend/services/strategies/*.py`)—— **实现方需先枚举**

**根因**:某些策略 `detect()` 用 `config.get("X")` 的阈值,但 `evaluate()` 的回退用**另一个硬编码常量**,两者不同源 → 用户调 config 的 X 被 evaluate 的硬编码值暗中抵消。

**修复**:
1. 先枚举:grep 每个策略里 detect 与 evaluate 都用到的数值阈值,找出"detect 读 config、evaluate 用硬编码不同值"的每一处。
2. 统一为**单一真相源**:evaluate 的回退改读 detect 用的同一 `config.get("X", <同一默认>)`,删掉第二套硬编码常量。
3. **在产出文件里逐一列出**改了哪些 `文件:行`、哪个阈值、原来两个值分别是多少 —— 审查方要逐条核。

**回归测试**:对至少 2 个改动到的策略,断言"改 config 的阈值后,detect 与 evaluate 的判定同步变化"(不再有隐藏第二值)。

---

## 完成判据

- 5 个 P0 全改 + 各带回归测试,`pytest` 绿(起 Postgres 的测试需真起库,纯逻辑测试免)。
- 全仓 grep 确认删除的常量/参数无残留引用。
- `.piercode/fix-02-last-message.md` 写清:每处 `文件:行` + 改动 + 测试证据 + P0-4 的枚举清单 + 未验证项。
- 不 commit/push 到 main;在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交并推送。
