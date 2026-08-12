# fix-02 P0 策略层修复结论

分支：`claude/batch-2-identity-finality-review-jvcsc5`

基座：从 `origin/claude/batch-2-identity-finality-review-jvcsc5` fast-forward 到 `b1695fb8` 后实施。本批只修改 spec 点名的策略文件、对应测试及本报告；未修改 `main`、`upstream`、编排或结算文件。

## P0-1 跟单 delay/max-age 双闸

- `backend/services/strategies/traders_copy_trade.py:194`：校验并保留 `max_signal_age_seconds_hard_ceiling`，使病态组合可在统一校验入口识别。
- `backend/services/strategies/traders_copy_trade.py:203`：当 `copy_delay_seconds >= hard_ceiling` 时记录 warning，避免空窗口静默失效。
- `backend/services/strategies/traders_copy_trade.py:617`：有效上闸改为 `min(hard_ceiling, copy_delay_seconds + requested_max_signal_age_seconds)`；`backend/services/strategies/traders_copy_trade.py:774` 的 `max_age` check 使用同一 effective 值，delay 下闸不变。原因是新鲜度应叠加在等待延迟之后，而不是与 delay 争用同一个绝对年龄窗口。
- 回归：`backend/tests/test_traders_copy_trade_strategy.py:160` 覆盖 delay=30/max-age=5 时 age=32 通过、age=40 拒绝；`:177` 覆盖 delay=0 的 5 秒旧行为；`:186` 覆盖病态配置 warning。

## P0-2 跟单 edge 改为 EV

- `backend/services/strategies/traders_copy_trade.py:354`：使用 `confidence - entry_price` 计算每份额 EV，再除以 entry price 得到非负 `edge_percent`；`expected_payout` 改为 clamp 后的 confidence。原 `roi_percent` 赋值位置不变。
- 已从同一文件的默认配置、参数 schema、校验白名单、coerce 和运行时公式中完整删除 `edge_midpoint` / `edge_multiplier`，没有保留兼容 shim 或 tombstone。
- 精确全仓 grep 只剩本 spec 的历史说明和回归测试的“不存在”断言；业务实现无这两个精确键。`threshold_edge_multiplier`、`oracle_*_edge_multiplier` 是其他策略的不同完整键名，未改动。
- 回归：`backend/tests/test_traders_copy_trade_strategy.py:199` 断言 price=0.99/conf=0.70 的 edge 为 0，price=0.01 的 edge 为 6900%，二者 payout 都为 0.70；`:218` 断言旧参数不再暴露。

## P0-3 certainty shock

- `backend/services/strategies/certainty_shock.py:228`：对规范化排除词预编译大小写不敏感的 `\b...\b` 正则；`:255` 用 pattern search 替代子串匹配。这样 `eth` 不再误伤 whether/weather，`sol` 不再误伤 solution/console；默认列表中的完整 `solana` 仍能排除 Solana 市场。
- `backend/services/strategies/certainty_shock.py:78` 与 `:229`：默认值和读取回退均从 21600 秒改为 900 秒。
- `backend/services/strategies/certainty_shock.py:280`：窗口不足时不再回捞窗口外的最后 N 个旧点，否则 6 小时慢漂移仍会重新进入 15 分钟计算。
- 回归：`backend/tests/test_certainty_shock_strategy.py:29` 覆盖词边界；`:69` 断言默认 900 秒；`:73` 断言 6 小时慢漂移不触发、14 分钟集中移动触发。

## P0-4 detect/evaluate 阈值单一来源枚举

最终按真实 detect/custom gate 调用链枚举为 7 个策略、9 个阈值。仅仅在 `default_config` 中声明但 detect 不读取同名 gate 的字段不计入，避免把策略语义扩大到 spec 外。

| 策略 | 阈值 | 原 detect 值 | 原 evaluate/check 回退值 | 修复位置与方式 |
|---|---|---:|---:|---|
| `ctf_basic_arb` | `min_edge_percent` | 0.60 | 3.0 | `backend/services/strategies/ctf_basic_arb.py:85`，pipeline fallback 动态读取 detect 的 `self.config` 同名键 |
| `flash_crash_reversion` | `min_liquidity` | 2500.0 | 1500.0 | `backend/services/strategies/flash_crash_reversion.py:453`，custom check 回退读取 `self.config/default_config` |
| `news_edge` | `min_edge_percent` | 5.0 | 3.0 | `backend/services/strategies/news_edge.py:814`，evaluate 回退读取 `_config` |
| `news_edge` | `min_confidence` | 0.45 | 0.42 | `backend/services/strategies/news_edge.py:818`，evaluate 回退读取 `_config` |
| `news_momentum_breakout` | `min_liquidity` | 3000.0 | 1500.0 | `backend/services/strategies/news_momentum_breakout.py:563`，custom check 回退读取 `self.config/default_config` |
| `stat_arb` | `min_edge_percent` | 5.0 | 3.5 | `backend/services/strategies/stat_arb.py:223`，pipeline fallback 动态读取 detect 的 `self.config` 同名键 |
| `traders_confluence` | `min_confidence` | 0.45 | 0.42 | `backend/services/strategies/traders_confluence.py:116`，pipeline fallback 动态读取 firehose/detect 的 `_effective_config()` |
| `weather_distribution` | `min_edge_percent` | 5.0 | 3.0 | `backend/services/strategies/weather_distribution.py:102`，pipeline fallback 动态读取 detect 的 `_config` |
| `weather_distribution` | `min_confidence` | 0.50 | 0.42 | `backend/services/strategies/weather_distribution.py:102`，pipeline fallback 动态读取 detect 的 `_config` |

排除项说明：`certainty_shock`、`cross_platform`、`temporal_decay` 虽有类级 default/pipeline 声明值差异，但 detect 不读取这些同名 gate；`holding_reward_yield`、`settlement_lag`、`vpin_toxicity` 同理，因此未改。`crypto_entropy_maker`、`crypto_spike_reversion`、`tail_end_carry` 的真实 detect/evaluate 值一致。

回归：

- `backend/tests/test_strategy_threshold_single_source.py:36`：CTF 修改 config 后 detect 与 evaluate 的 edge 判定同步变化。
- `backend/tests/test_strategy_threshold_single_source.py:98`：news_edge 修改 edge/confidence config 后 detect 与 evaluate 同步变化。
- `backend/tests/test_strategy_threshold_single_source.py:130`：flash crash/news momentum 的 liquidity check 使用各自检测配置 2500/3000。
- `backend/tests/test_strategy_threshold_single_source.py:158`：stat arb、traders confluence、weather 的缺参 pipeline fallback 使用策略配置源。

## 测试与检查证据

- 修改前聚焦回归：16 项中 5 passed、11 failed，复现 5 类旧缺陷。
- 修改后聚焦回归：`python -m pytest backend/tests/test_traders_copy_trade_strategy.py backend/tests/test_certainty_shock_strategy.py backend/tests/test_strategy_threshold_single_source.py -q` → `16 passed in 0.85s`。
- 用户指定完整筛选：`python -m pytest backend/tests/ -k "copy_trade or certainty or strateg"` → `432 passed, 2126 deselected, 14 warnings in 267.55s`。
- 语法：`python -m compileall -q backend/services/strategies ...` 通过。
- 范围/格式：`git diff --check` 通过；精确键 grep 确认业务实现中没有 `edge_midpoint` / `edge_multiplier`。
- 本机最初缺少仓库要求的 `feedparser`，且 `pytest-asyncio 0.21.1` 不支持测试中的 `loop_scope`；按 `backend/requirements.txt` 约束补为 `feedparser 6.0.14`、`pytest-asyncio 0.26.0`，解析器同时升级到 `pytest 8.4.2` 后原命令通过。

## 未验证项

- 未运行未筛选的全部 2558 项测试；按任务要求运行了 432 项筛选测试和 16 项聚焦回归。
- 本批都是纯逻辑测试，没有测试需要 PostgreSQL，因此没有用跳过代替数据库验证。
- 没有为当前运行中的 Docker 栈重建镜像或重灌数据库策略源码；这些文件顶部注明它们是 seed template，正在运行的 `strategies.source_code` 是否刷新不属于本次代码批次验证范围。
- 完整筛选测试有 14 条既有 warning（datetime deprecation、asyncpg cleanup coroutine 等），但无失败。
