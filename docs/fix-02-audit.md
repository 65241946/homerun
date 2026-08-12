# fix-02 审查 + P0-4 全策略补扫

> 审查对象:commit `f455129`(策略层 P0 修复批次),分支 `claude/batch-2-identity-finality-review-jvcsc5`。
> 审查方独立在干净容器核对真实 diff + 亲跑测试,非照抄实现方自述。

## 结论

**fix-02 通过,可作为策略 P1 优化的基础。** 5 类 P0 全部改对;P0-4 经全策略补扫**无漏网**。另发现一个独立于 P0-4 的隐患(见末节),留给 P1。

## fix-02 逐项核对

| P0 | 结果 |
|---|---|
| 1 跟单 delay/max-age 互斥 | `effective_max_age = min(hard_ceiling, copy_delay + max_age)`;delay 下闸保留;病态配置有 warning。✓ |
| 2 跟单 edge→EV | `edge_ev = confidence − entry_price`;`expected_payout = clamp(confidence)`;`edge_midpoint/multiplier` 全删无残留。✓ |
| 3a certainty 子串排除 | `\b…\b` 正则 + `any(pattern.search)`;`import re` 在位。✓ |
| 3b certainty 6h→900s | 默认+回退均 900s;额外删掉"窗口不足回捞窗外旧点"(超 spec 但正确)。✓ |
| 4 detect/evaluate 阈值单一源 | 7 策略 9 阈值,evaluate 回退改读各自 config,硬编码第二值删净。✓ |

### 测试有效性(最硬证据)

- 修复后:`test_traders_copy_trade_strategy` + `test_certainty_shock_strategy` + `test_strategy_threshold_single_source` = **16 passed**(审查方容器独立复现)。
- 把策略码退回修复前、保留新测试再跑 = **11 failed, 5 passed**,精确复现全部 5 类 bug → 测试是真守门。

## P0-4 全策略补扫(独立于实现方枚举)

方法:AST 静态提取全部 34 个策略的 `pipeline_defaults` 与 `default_config`,机器比对同名阈值;对每个不一致处再查 detect 是否真读该阈值;追基类 `base.py`。

- 基类 `evaluate`(base.py:1271)默认委托 `_pipeline_evaluate`(:1397),对 `min_edge_percent/min_confidence/max_risk_score` 用 `params.get(X, pipeline_defaults[X] 缺则硬编码 3.0/0.42/0.68)` 设闸。
- 同闸分歧的**充要条件** = detect 与 evaluate 都对同一阈值设闸、取值不同。
- 逐个核实值不一致的策略,其 **detect() 对这些阈值零引用**:`certainty_shock`、`cross_platform`、`temporal_decay`、`holding_reward_yield`、`settlement_lag`、`vpin_toxicity` → 均**非** P0-4,排除正确。`crypto_entropy_maker`、`crypto_spike_reversion` 有自洽的 evaluate override。

**→ 实现方修的 9 阈值 / 7 策略,就是真 P0-4 的完整集合。无漏网。**

## 独立隐患(非 P0-4,留给 P1)

`_pipeline_evaluate` 缺 `pipeline_defaults` 时兜底到硬编码 `3.0/0.42/0.68`。以下策略在 `default_config` 声明了这些旋钮、但 detect 不用:

| 策略 | default_config 声明 | evaluate 兜底 |
|---|---|---|
| `settlement_lag` | min_edge=4.0, min_conf=0.45, max_risk=0.78 | 基类 3.0/0.42/0.68 |
| `vpin_toxicity` | min_edge=2.5, min_conf=0.5, max_risk=0.75 | 基类 3.0/0.42/0.68 |
| `holding_reward_yield` | min_edge=1.0, min_conf=0.3, max_risk=0.75 | 基类 3.0/0.42/0.68 |
| `certainty_shock`/`cross_platform`/`temporal_decay` | 见各自 default_config | pipeline_defaults(≠ default_config) |

**性质取决于实盘 params 来源:**
- 回测/模拟:`params = strategy.config`(`strategy_backtester.py:1891/2052`、`execution_simulator.py:425`),声明值**生效**,硬编码兜底是死代码 → 无害。
- 实盘 runtime:是否也把 `strategy.config` 塞进 `context["params"]` **尚未追到底**。若没塞,这些声明旋钮在实盘 evaluate 会被硬编码 `3.0/0.42/0.68` 悄悄取代。

**P1 待办**:追一处实盘 evaluate 调用点(orchestrator 侧 `context["params"]` 构造),确认是否传入 `strategy.config`。
- 若传入 → 上述为死防御代码,可选择清理(让声明值成唯一源)。
- 若没传入 → 是真 bug:声明的默认阈值实盘不生效,需按 fix-02 P0-4 同法把兜底改读 `self.config`。
