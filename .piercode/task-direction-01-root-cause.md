# Task: direction-01 — 非规范 direction 写入点根因定位(只调查,不修复)

## Context

你在 `D:\Desktop\量化交易\homerun-pmr`。你没有我们的对话历史,以下是全部必需背景。

### 用户实测的两个故障

1. 钱包共识信号没法下单
2. 模拟交易收益没法正常结算

### 已确证的证据链(每条都已核对过原文)

消费端只认 6 个合法值,见 `backend/services/simulation.py:86-90`:

```text
buy_yes / sell_yes / buy_no / sell_no      → 直接映射
buy / sell                                 → 靠 payload 的 token_ids 索引反查
其余                                        → simulation.py:124 raise ValueError
```

数据库里实际存在的非法值(来自 `homerun-pmr/.piercode/audit-01-baseline.md` Claim 5 与 Claim 7):

```text
a34cebe40064412dafb954c1da69b319   direction = buy_elina svitolina
78e02caa5f484dfda341f477db29c397   direction = buy_ekaterina alexandrova
(另有第三个样本)                     direction = buy_dn soopers
```

后果:9 条订单卡在 `status=submitted`、`token_id=''`、无 ledger;`shadow_ledger_backfill_failed` 事件 2309 条,首条根因 `record_orchestrator_shadow_fill_failed`,错误文本 `Unsupported direction 'buy_dn soopers'`。

这些值的共同特征:`buy_` + **体育赛事的 outcome 名称**(网球选手名、球队名)。二元 Yes/No 市场不会触发,体育类多结果市场必然触发。

### 已排除的候选(不要重复查)

| 候选 | 位置 | 为什么排除 |
|---|---|---|
| `_direction_from_outcome` | `backend/services/signal_bus.py:2196-2202` | 非 yes/no 返回 `None`,fail-safe |
| `_resolve_leg_direction` | `backend/services/trader_orchestrator/session_engine.py:161-167` | 拼接前校验 `outcome in {yes,no}`,非规范时退回裸 `side` |

注意 `session_engine.py:157-159`:leg 若已带 `direction` 则**原样返回、零校验**。所以污染源在更上游。

## Your task

1. 找出所有能把非规范值写入 `trader_orders.direction` 的代码路径。重点是「`buy_` 前缀 + outcome 名称」这种形态的产生点。至少覆盖:signal 生成、leg 构造、copy-trade 路径、manual buy 路径、以及任何从 market outcome label 推导 direction 的地方(已知嫌疑:`backend/tests/test_trader_order_market_links.py:114` 提到 `uses_cached_market_outcome_labels_for_direction`)。
2. 对每个找到的写入点,用 `git log -S` 或 `git log -L` 确认:该逻辑是最近引入的,还是长期存在但只在体育类市场才触发。给出引入的 commit SHA 和日期。
3. 说明为什么现有测试没有拦住。已知 `backend/tests/test_simulation_orchestrator_ledger.py:253` 覆盖了裸 `buy`,但没有任何测试覆盖 `buy_<非规范名>`。确认是否还有其他覆盖缺口。
4. 把结论写入 `.piercode/direction-01-findings.md`。

## Hard constraints

- **只调查,不修复**。不得修改任何 `backend/` 下的源码或测试。
- 只允许创建这一个文件:`.piercode/direction-01-findings.md`
- 不得修改数据库中的任何行
- 不得读取工作目录以外的内容
- 不得调用任何外部 API 或产生费用
- 只读查询数据库是允许的,但必须使用 `transaction(readonly=True)`

## Output format(必须遵守)

### 结论

根因是什么,一句话。是新引入还是长期存在,明确说。

### 写入点清单

每个写入点固定格式:

> `文件路径:行号` — 引用产生 direction 的那几行原文

**产生条件**:什么输入会导致非规范输出
**引入时间**:commit SHA + 日期,或「长期存在,可追溯至 <SHA>」
**是否已触发**:能否对应上数据库里的实际坏值

### 测试覆盖缺口

> `文件路径:行号` — 现有测试覆盖了什么

**缺什么**:哪种输入形态没有被任何测试覆盖

### 保持不动

哪些实现是对的(例如已确认 fail-safe 的两个函数),后续修复不要破坏。

### 未能验证

不确定的、没查证的、超出权限的。**不许留空来显得完成度高**。真没有就写「无」并说明核验了什么。

## Verification(提交前必做)

- 抽查至少 3 条事实性主张,对照真实代码确认
- 确认 `git status --short` 只有 `.piercode/direction-01-findings.md` 一个新文件,把输出贴进产出文件
- 对每个声称的写入点,给出一个具体的输入样例,能推出数据库里已观测到的某个坏值

## 明确不要做的事

- 不要修复。这一轮只要根因。
- 不要为了让结论好看而跳过「未能验证」
- 不要修改测试来适配现状
