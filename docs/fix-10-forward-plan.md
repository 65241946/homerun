# fix-10 ~ fix-12：正向修复计划

> 本文取代 fix-08 / fix-09（作废，不实施），并终止对 fix-02 ~ fix-07 的回滚式返工。
> 基座：`claude/batch-2-identity-finality-review-jvcsc5`（干净 fix-01 基座 + 已合并的 fix-02~07 + 4 个回归修复）。

## 为什么换打法

前面每一轮"为什么 X 是 0"都是靠读代码倒推的，因为**系统没有归因数据**。
525 条信号产出 0 个订单，日志里查不到是哪一道门把它们拦住的 —— 计数器
`deferred_by_reason` / `expired_by_reason` / `prefiltered_by_reason` 确实存在
（`trader_orchestrator_worker.py:6111-6113`），但只在
`decisions_written == 0 且 orders_written == 0` 的 idle 分支里，塞进一条
heartbeat message 的 payload（:8577, :8590），既不是指标、也无法查询、
正常出单的周期根本不记录。而**逐门拒绝计数完全没有**。

所以先把观测面建起来，后面所有判断才有依据。这是 fix-10 必须先落地、
且不与其他项混在一起的原因。

fix-11 / fix-12 与 fix-10 文件不相交，可并行。

---

## FIX-10：交易信号漏斗归因（P0，先做，独立提交）

### 现状（已核实）

| 事实 | 位置 |
|---|---|
| 三个 `*_by_reason` 计数器只在 idle 分支进 heartbeat payload | `workers/trader_orchestrator_worker.py:8565-8600` |
| 出单的周期不记录任何归因 | 同上，`if decisions_written == 0 and orders_written == 0` |
| 逐门拒绝计数不存在 | 全仓库无 |
| **但逐门数据本来就有** | `StrategyDecision.checks` 每项是 `DecisionCheck(key, label, passed, ...)` |

最后一行是关键：`[c.key for c in decision.checks if not c.passed]` 就是
逐门拒绝直方图，数据一直在，只是从来没有被聚合。

### 交付

1. **每周期一条漏斗记录，无条件写**（不再只在 idle 时写）。阶段：

   ```
   picked_up                 本周期取到的信号数
   prefiltered_by_reason     {reason: n}   已有
   deferred_by_reason        {reason: n}   已有
   expired_by_reason         {reason: n}   已有
   evaluated                 真正进入 strategy.evaluate 的数量
   gate_rejected_by_key      {check_key: n}  ← 新增，从 decision.checks 聚合
   selected                  decision == "selected" 的数量
   orders_submitted          
   orders_rejected_by_reason {reason: n}
   orders_filled             
   ```

   守恒关系必须成立：
   `picked_up = prefiltered + deferred + expired + evaluated`，
   `evaluated = selected + (被 gate 拒的 decision 数)`。
   写入前 assert 一次，不等式说明有分支漏计 —— 这正是要抓的东西。

2. **`gate_rejected_by_key` 的采集点**：orchestrator 拿到 `StrategyDecision`
   之后、写 decision 行之前，对 `decision.checks` 做一次
   `for c in decision.checks: if not c.passed: counter[c.key] += 1`。
   一个 decision 可能有多个门同时失败，**全部计入**（不要只取第一个）——
   我们要知道的是"哪些门在杀信号"，不是"谁先杀"。

3. **落库 + 暴露**：复用现有 `write_worker_snapshot` / trader cycle 快照通道，
   不要新建表。字段放在 stats 里。再加一个只读端点
   `GET /traders/{trader_id}/funnel?cycles=N` 返回最近 N 个周期的漏斗，
   以及一个跨 trader 的聚合 `GET /traders/funnel`。

4. **日志**：每周期一行 INFO，格式固定、可 grep：
   ```
   funnel trader=<id> picked=12 prefilt=3 defer=2 expire=0 eval=7 selected=1 orders=1 top_gate=min_edge:4,max_spread:2
   ```
   `top_gate` 取拒绝数前 3 的门。

### 验收

- 起一个 shadow trader 跑 10 分钟，`GET /traders/funnel` 能直接回答
  "这些信号死在哪一道门"，不需要读代码。
- 守恒 assert 不触发。
- 出单的周期同样有记录（不是只有 idle）。

### 不要做

- 不要新建指标系统 / Prometheus / 新表。
- 不要改任何 gate 的阈值或逻辑。**这一版只观测，不改行为。**

---

## FIX-11：体育源没有生产者（P1）

### 现状（已核实）

| 事实 | 位置 |
|---|---|
| 策略存在，声明 `source_key = "sports"` | `services/strategies/sports_overreaction_fader.py:222` |
| `detect(events, markets, prices)` 签名与 scanner 策略**完全一致** | 同上 :286 |
| 已在机会策略目录注册 | `services/opportunity_strategy_catalog.py:1641` |
| 交易面已经把 `"sports"` 列为消费源 | `workers/host.py:138, :418` |
| **scanner 只跑 `source_key == "scanner"` 的策略** | `services/scanner.py:1941, 1965, 2893` |
| **没有 `sports_worker.py`，没有任何 producer** | `workers/` 目录 |

所以体育永远是 0 不是配置问题，也不是某个 gate 拦的 —— **生产者从来没被建**。
策略是孤儿：注册了、能被加载、但没有任何代码路径会调用它的 `detect()`。

### 交付

不要新建 `sports_worker.py`。scanner 已经在抓并且分类体育市场
（`scanner.py:3423` 按 category 分 sports/soccer/baseball/...），它就是天然的生产者。
缺的只是两件事：

1. **scanner 收策略时不再硬编码 `"scanner"`**：把
   `scanner.py:1941 / 1965 / 2893` 三处的
   `source_key == "scanner"` 改成"在本 scanner 负责的 source 集合内"，
   集合默认 `{"scanner", "sports"}`，由一个模块常量给出（不要读环境变量，
   不要加新配置项 —— 这是代码层的路由表，不是用户旋钮）。

2. **bridge 按 source_key 分组**：`workers/scanner_worker.py:265` 现在把所有机会
   用 `source="scanner"` 一次性 bridge。改成按产出机会的策略 `source_key`
   分组，每组用自己的 source bridge 一次。
   机会到策略的映射：`strategy_loader.get_instance(opportunity.strategy).source_key`
   （:260 附近已经在用这个 pattern 取 `quality_filter_overrides`，照抄）。

3. 分组后 `source="sports"` 的那一组会走 `sports` 源，交易面 `strategy_source_keys`
   已经包含它，下游无需改动。

### 验收

- scanner 跑一轮后，`trade_signals` 表里出现 `source = 'sports'` 的行。
- 前端体育页不再恒为 0（有没有机会取决于行情，但**必须有信号被产出并落库**；
  若确实没有满足条件的市场，fix-10 的漏斗要能显示体育源 picked_up > 0
  或明确显示 detect 产出 0 与原因）。
- scanner 原有的 `source="scanner"` 行为不变，数量不下降。

### 注意

`strategy_loader` 从不 `setattr` `source_key`，所以**这个问题无法靠改数据库配置解决**，
必须改代码。别在 `strategies.config` 里找旋钮。

---

## FIX-12：news 抓取静默失败（P1）

### 现状（已核实）

`services/news/workflow_orchestrator.py:352-357`：

```python
fetched = await asyncio.wait_for(news_feed_service.fetch_all(), timeout=60)
...
except Exception as exc:
    logger.warning("News fetch sync failed (continuing with cache): %s", exc)
```

`asyncio.TimeoutError` 的 `str()` 是**空字符串**。超时的时候这行日志打出来是：

```
News fetch sync failed (continuing with cache): 
```

后面什么都没有。运维看到的是一条没有内容的 warning，等于失败被吞掉了。
而且 60s 是**整个 `fetch_all()` 的总预算** —— 任何一个慢 feed 就能把全部
feed 一起拖超时，结果是"一个都没抓到"而不是"少抓了一个"。

### 交付

1. **超时单独成支，并且打得出来**：
   ```python
   except asyncio.TimeoutError:
       logger.warning("News fetch sync timed out after %ss (continuing with cache)", 60)
   except Exception as exc:
       logger.warning("News fetch sync failed (continuing with cache): %r", exc)
   ```
   注意通用分支用 `%r` 不用 `%s` —— 空 `str()` 的异常类型不止 `TimeoutError` 一个。

2. **改成逐 feed 有界 + 部分成功**：`fetch_all()` 内部对每个 feed 单独
   `wait_for`（每个 feed 一个较小的预算），一个 feed 超时只丢它自己，
   其余照常返回。总预算保留作为兜底外层。
   返回值要能区分"抓到 0 条"与"全部超时"。

3. **把结果写进 worker 快照 stats**：`feeds_total` / `feeds_ok` / `feeds_timeout` /
   `articles_fetched`，这样 fix-10 的漏斗之外，新闻源的健康度也是可查的。

### 验收

- 人为把一个 feed URL 指向不可达地址，日志必须打出**哪个 feed** 超时，
  且其余 feed 的文章正常入库。
- 全部 feed 超时的情况下，日志里能看到明确的超时信息（不是空字符串）。

---

## 不在本批次内（明确说明去向）

| 项 | 去向 |
|---|---|
| copy-trade `confidence` 硬编码 0.70（`traders_copy_trade_signal_service.py:554`） | **策略会话**。这是"钱包 skill 先验怎么定"的问题，不是代码 bug。当前 EV 公式两个版本都是拿常量当概率，改哪边都是编造。 |
| fix-05 G-2 收敛语义、几个未经请求的默认值翻转 | **暂不回滚**。等 fix-10 的漏斗数据出来，用数据判断它们是否真的在杀信号，再决定。 |
| ~70 个"读了但没声明"的配置键 | 排在 fix-13，等 fix-10 落地后做，届时可以用漏斗确认哪些键真的在生效路径上。 |
| fix-08 / fix-09 | **作废**，不实施。 |

## 需要操作者做的（不是代码）

1. **GitHub Actions 是关闭的** —— `list_workflows` 返回 0，`.github/workflows/ci.yml`
   在 `main` 上但从未执行过。所以 worker-plane 守卫目前不提供任何保护，
   8169 个 ruff 报错和一个从不被收集的测试文件都是这么漏过去的。
   仓库 Settings → Actions 里启用。
2. `max_spread_bps` 当前 75（= 0.75% 价差上限），对预测市场偏紧。
   建议 150–250。这条确认在生效（所有 trader 都走 `create_trader`，会完整归一化 22 个键）。
