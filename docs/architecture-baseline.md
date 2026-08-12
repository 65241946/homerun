# Homerun 交易系统 — 架构基线与跑通方案

> 分析对象:`65241946/homerun` @ `389d246`(上游原版,未经修改)
> 方法:6 个只读子代理并行测绘 6 大子系统 + 基建/文档人工通读 + 关键缺陷人工核实
> 口径:以源码实际实现为准。**CONFIRMED** = 已在源码逐行核实;**候选** = 子代理测绘发现、尚待二次核实。
> 面向:未来实盘交易。全篇围绕状态一致性、异常容错、日志埋点、错误回滚、仓位安全、参数校验。

---

## 一、整体架构 & 模块职责

### 1.1 进程拓扑(基座的脊柱)

系统**不是单体**,而是「1 个 API 进程 + N 个 worker plane 进程」,通过 Postgres + Redis 协同。

```
┌─────────────┐   HTTP/WS   ┌──────────────┐
│  前端 nginx  │ ──────────▶ │  API plane    │  main.py lifespan
│  (React)    │             │  :8000        │  只跑 API 侧循环,把编排状态写回 DB
└─────────────┘             └──────┬───────┘
                                   │ Postgres(状态权威) + Redis(跨进程总线/缓存)
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
  worker plane: trading      worker plane: news        worker plane: discovery
  (唯一能下单)                (ML 策略/新闻情报)          (市场/钱包发现)
  + recording / detection / reconciliation / jobs / services
```

- **plane 定义**:`workers/host.py:100` `_PLANE_CONFIGS` —— `trading / news / discovery / jobs / recording / detection / reconciliation / services / all(legacy 单进程)`。
- **启动**:`python -m workers.host <plane>`(`host.py:2304`),一个 plane 一个 OS 进程,`_acquire_plane_lock()`(`host.py:2196`)文件锁防同 plane 双开。
- **服务门控**:各服务读 `HOMERUN_WORKER_PLANE` 环境变量决定是否在本 plane 启动(`host.py:634` `_enabled`)。

**🔒 基座铁律(禁改区):trading plane 绝不能跑两次。** 会重复连 Polymarket WS 用户频道,两个会话双双被踢(docker-compose 注释 + `host.py:17-21`)。

### 1.2 资金路径单执行者(核心安全设计)

来源:`docs/plane_isolation_handoff.md` + 代码核实。

- **只有 trading plane 下单。** 主止盈止损引擎是 `exit_risk_loop.py:118` `ExitRiskLoop`:2 秒扫描 + WS 价格 tick 亚秒唤醒,独立 DB 连接池(2.5s 语句超时,永不排在重量级 reconcile 后面)。
- `reconcile_live_positions`(`position_lifecycle.py:6174`,~4.5k 行)95% 是冷对账 + 兜底,只在 `exit_risk_loop` 卡死 >10s 时触发平仓兜底。
- **安全不变量**:资金路径单执行者;冷对账的竞态可自愈(最坏一个 reconcile 字段标错,绝不会漏单或重复下单)。

### 1.3 领域模型

系统扫预测市场(Polymarket、Kalshi)套利,4 类错价:`WITHIN_MARKET`(YES+NO<$1)、`CROSS_MARKET`(跨平台同事件价差)、`SETTLEMENT_LAG`(结果已知未结算)、`NEWS_INFORMATION`(新闻隐含概率≠市价)。

- **核心对象 `Opportunity`**(`models/opportunity.py`,Pydantic 传输模型,非表):每个策略产出它,每个 UI 消费它。
- **交易模式**:仅 `shadow`(纸面,默认)/ `live`(实盘)两个 canonical 模式(`trader_orchestrator_state.py:180`);`backtest` 是独立离线链路;read-only live 是 reconciliation plane 的认证只读。

### 1.4 各层规模(源码实测)

| 层 | 文件 | 行数 | 职责 |
|---|---:|---:|---|
| `services/` | 346 | 259,808 | 全部业务逻辑 |
| ├ `strategies/` | 35 | 38,746 | 策略层(30 个策略) |
| ├ `trader_orchestrator/` | 25 | 27,458 | 编排/执行/退出核心 |
| `api/` | 40 | 36,539 | 40 个路由 + WebSocket |
| `workers/` | 24 | 23,209 | 后台 plane/worker |
| `models/` | 6 | 7,254 | 持久化(113 张表)+ Pydantic |
| `utils/` | 17 | 2,903 | 日志/限流/重试/时钟 |
| **backend 合计(不含测试)** | | **~347,837** | |
| tests | 251 | 84,114 | |

---

## 二、数据流完整链路

### 2.1 检测 → 信号

```
策略 detect()/detect_async()/on_event()  (services/strategies/*，运行时在 DB)
  → QualityFilter → Dedup
  → strategy_signal_bridge.bridge_opportunities_to_signals  (opportunity→intent runtime 热路径)
  → trade_signals 表(UNLOGGED,只是审计投影)
```

### 2.2 信号 → 决策 → 订单(全量路径)

主循环 `workers/trader_orchestrator_worker.py`,单个候选顺序:

1. **prefilter**:live 下 reentry 冷却市场直接丢(`worker:6119`)。
2. **策略 `evaluate()`** → `decision ∈ {selected, blocked, skipped, failed}`,仅 `selected` 进门禁。
3. **pre-gate 短路**:`nofill_cooldown` / `reentry_cooldown` / `stacking_guard`(`worker:7212-7233`)。
4. **`apply_platform_decision_gates`**(`decision_gates.py:723`):顺序跑 ~16 个门禁,任一 block 即短路(每个门禁 `if final_decision=="selected"` 守卫)。
5. **`selected`** → `session_engine.execute_signal`(`session_engine.py:760`)。

### 2.3 订单构造 → 提交 → 回执(同步链)

```
execute_signal (session_engine.py:760)
 → _build_plan (:597)         取 execution_plan.legs / 合成单腿；allocate_leg_notionals 分名义额
 → _enforce_live_full_bundle_plan (:422)   担保捆绑强制 PAIR_LOCK/IOC
 → build_execution_session_rows            建 ExecutionSession + 腿行；提交前拒单闸(自成交/覆盖)
 → pre_db_venue_preflight (:1154)          venue 闸(buy_collateral / max_spread)；live 写占位单
 → submit_execution_wave (order_manager.py:1618)   各腿 asyncio.gather 并行 + 每腿超时
   → submit_execution_leg (:661)  ★主分叉点
       ├ shadow: fill_simulator 微结构估算，不触 venue (:1109)
       └ live:   execute_live_order → place_order (live_execution_service.py:4769) = 真实 CLOB 下单
 → 处理 wave 结果 (:1918)：executed→completed / open|submitted→open；failed 腿 reprice 重试
 → _persist_execution_projection (:1411)   写 TraderOrder（executed 被降级为 "open"）
```

### 2.4 成交确认 → 仓位 → 结算(异步链,独立循环)

```
reconcile_live_positions (position_lifecycle.py:6174)  ← trader_reconciliation_worker 30s（仅 live）
reconcile_shadow_positions (position_lifecycle.py:4591) ← 编排 worker 维护子周期 60s（仅 shadow）
  · 入场成交由钱包持仓回填确认：status="executed" + verification_status="wallet_position" (:7019)
  · 出场：exit_risk_loop 主 / reconcile 兜底 → execute_position_exit (:5768)
       → terminalize_filled_exit (:6080) → _status_for_close (:2114)
       → 写 actual_profit + payload.position_close
  · 链上核验覆盖 actual_profit，置 verification_status="wallet_activity"（polymarket_trade_verifier）
```

### 2.5 快车道(fast tier,风控面收窄)

`latency_class='fast'` 的 trader 走 `fast_submit.execute_fast_signal`(`fast_submit.py:290`):journal-first,**不跑 `apply_platform_decision_gates`**,只做 journal 去重 + `max_open_orders` 内存上限 → 直接 `submit_execution_leg`。venue 侧门禁仍生效,但平台层风控(日损/敞口/stacking/freshness/止损经济性)全部跳过。

---

## 三、依赖 / 配置 / 环境

### 3.1 运行时依赖(`backend/requirements.txt`)

- **核心**:fastapi≥0.115、uvicorn、sqlalchemy[asyncio]==2.0.25、asyncpg==0.30、alembic==1.13.2、pydantic≥2.7、redis 5.x、httpx、websockets==12.0、greenlet。
- **事件循环**:uvloop(Linux/macOS)/ winloop(Windows),`HOMERUN_FAST_LOOP=0` 可退化 stdlib。
- **ML/AI**:sentence-transformers、faiss-cpu、xgboost-cpu、lightgbm、onnxruntime、**lifelines**(Cox 成交概率模型)、numpy、scikit-learn。
- **其它**:pyarrow(回测 parquet)、weasyprint+jinja2(报告 PDF)、mcp(对外暴露回测工具)、cryptography。

### 3.2 实盘专属依赖(`backend/requirements-trading.txt`,仅真实盘才装)

- `py-clob-client-v2`、`eth-account`、`web3`、**`coincurve`**(libsecp256k1 C 绑定,订单签名快 23×)。
- **含义**:不装这个文件,系统跑不了实盘 —— 天然纸面。

### 3.3 部署拓扑(`docker-compose.yml`)

- **Postgres 16**:为 130 万+记录/分钟写入量重度调优(wal_buffers=64MB、wal_compression=lz4、`synchronous_commit=off`、checkpoint_timeout=30min、`statement_timeout=60s`、`lock_timeout=5s`、`idle_in_transaction_session_timeout=120s`)。
- **Redis 7**:跨进程总线 + 缓存(no-persistence,maxmemory 1gb,volatile-lru)。
- **migrate**(一次性,跑 `init_database()`)→ **backend**(FastAPI :8000)→ **3 个 worker plane**(trading/news/discovery)→ **frontend**(nginx :3000)。

### 3.4 配置面(`config.py`)

- **凭证/链上**:`POLYMARKET_PRIVATE_KEY`(:346)、`POLYMARKET_API_KEY/SECRET/PASSPHRASE`(:347-349)、`CHAIN_ID=137`、`CLOB_API_URL`、`POLYGON_RPC_URL`。
- **交易安全限值**(`config.py:1026-1031`,live_execution 订单校验用):`MAX_TRADE_SIZE_USD=100`、`MAX_DAILY_TRADE_VOLUME=1000`、`MAX_SLIPPAGE_PERCENT=2.0`、`MIN_ACCOUNT_BALANCE_USD=0`。
- **基础设施**:`DATABASE_URL`(:236,带 `.runtime/database_url` 文件回退)、`REDIS_URL`、`REDIS_ENABLED`(master kill-switch)。
- **运行时覆盖优先级**:`db_non_null_override > env > code_default`(`config.py:758`)。每进程启动调 `apply_runtime_settings_overrides()`,**DB `app_settings` 表的非空值能压过环境变量**。180+ 配置项热重载,worker 每循环重读。

### 3.5 环境变量

`HOMERUN_WORKER_PLANE` / `HOMERUN_PROCESS_ROLE`(plane 门控)、`LOG_LEVEL`、`HOMERUN_FAST_LOOP` / `HOMERUN_USE_WINLOOP`、`HF_TOKEN`、`APP_SECRETS_KEY`、`TELEGRAM_*`。

---

## 四、缺陷 / 实盘风险 / 逻辑漏洞清单

> 优先级:🔴 实盘资金安全 / 状态一致性(必须先解决) · 🟠 应尽快修 · 🟡 记录待评估

### 🔴 CONFIRMED-1:模拟结算入账是死代码 —— "收益结算不出来"根因

**已逐行核实。** 编排器影子交易的平仓入账 `close_orchestrator_shadow_fill`(`simulation.py:479`)被 `enable_simulation_ledger` 门控(`position_lifecycle.py:5062`),该参数默认 `False`(`:4601`),**全库无任何调用方传 True**(grep 仅命中定义与门禁两处)。而开仓扣款 `record_orchestrator_shadow_fill`(`simulation.py:456`)照常经 backfill 执行。

- **净效果**:`SimulationAccount.current_capital` **只扣不还**,`total_pnl` 永不更新。
- **放大**:资金单调下降 → 最终每次 backfill `required_capital > current_capital` 抛 `insufficient shadow capital`(`simulation.py:376`)→ 卡死。
- **注意**:realized pnl 其实写进了 `TraderOrder.actual_profit`(`:5136`),只是没镜像进 SimulationAccount 账本。**修复方向不是简单打开开关**,要先确定 UI/账本口径以哪张表为权威,避免双重记账。

### 🔴 CONFIRMED-2:全库 0 个 CHECK 约束 + 枚举字段实为自由字符串

**已核实。** 除 3 个 simulation 列(`TradeStatus`/`PositionSide` 真枚举)外,**所有** status/side/**direction**/mode/verification_status 都是自由 `String`,合法取值只写在行内注释里。DB 层无法保证:价格∈[0,1]、size/notional≥0、filled≤size、legs_completed≤legs_total、枚举合法性。

- **`trader_orders.direction`**(`database.py:4331`)= nullable String,无枚举无 CHECK → 数据库接受任意字符串。这是你之前在 pmr 版遇到的 `buy_<人名>` 能落库的 schema 层根源。
- **干净基座现状**:执行路径的 direction 构造 `_resolve_leg_direction`(`session_engine.py:149`)是 fail-safe 的(拼接前校验 `outcome∈{yes,no}`,否则退回裸 `buy`/`sell`)。**即缺陷的"使能条件"在基座就存在,但主执行路径当前不产生垃圾值** —— 其它写入路径(manual-buy、钱包共识 traders_confluence)是否安全需针对干净基座单独核实(见"待核实清单")。

### 🔴 CONFIRMED-3:实盘无独立现金分录账本

**已核实。** 实盘余额/PnL 是 `live_trading_runtime_state` 上的**可覆盖聚合**(`database.py:2980-2982` `total_pnl/daily_pnl/total_volume`),无 append-only 分录,无法独立重建/对账。金源仅靠 verifier 事后覆盖 `trader_orders.actual_profit`。面向实盘,这违反"资金变动必须可审计追溯"。

### 🟠 候选-4:实盘订单缺去重唯一约束

`live_trading_orders.clob_order_id`(`database.py:3006`)、`trader_orders.provider_order_id`(:4340)仅索引非唯一;订单去重完全靠 `trader_signal_consumption` 的 `uq(trader_id,signal_id)`(:4848)在**信号消费**粒度,非**订单**粒度。同一经纪商订单理论可被记录多次。

### 🟠 候选-5:快车道绕过全部平台门禁

`execute_fast_signal`(`fast_submit.py:290`)不跑日损/敞口/stacking/strict-WS/止损经济性检查。实盘启用快车道时风控面显著收窄,需与产品意图核对是否有意为之。

### 🟠 候选-6:编排器影子仓对 position_monitor 不可见

`record_orchestrator_shadow_fill` 建仓不写 TP/SL(`simulation.py:438-453`),而经典 `position_monitor` 只监控设了 TP/SL 的仓(`position_monitor.py:91`)。→ 除已关闭的 CONFIRMED-1 外,影子仓没有别的结算出口。

### 🟠 候选-7:全局 pause 恢复的 all() 语义

`should_pause = all(control.is_paused ...)`(`main.py:528-541`)跨 9 子系统。只有全部暂停才恢复为 paused;操作员若只暂停部分,重启后恢复运行。实盘场景可能与意图相悖。

### 🟠 候选-8:非法/裸 direction 静默卡死仓位

`_direction_outcome_index` 对裸 buy/sell 且缺 token_id 或非二元市场返回 None(`position_lifecycle.py:1296`),该行被记 `invalid_entry` 跳过、**永不平仓**(`:4693-4755`);开仓侧对应 `_direction_to_position_side` raise(`simulation.py:116`)→ 连账本都不建。

### 🟡 候选-9:提交歧义 → 本地 FAILED 不回查 venue

`place_order` 通用异常标 FAILED + 释放抵押(`live_execution_service.py:5522`),**不回查 venue**;若 venue 实际已接单 → "记为失败的真实持仓",只能靠对账扫描按 metadata/provider ID 匹配恢复。主链 `place_order` 不 enqueue pending reconciliation。

### 🟡 候选-10:即时成交也先记 OPEN,FILLED 靠异步

`place_order` 即时成交返回 OPEN(`:5271`),FILLED 依赖后台 `_bg_fill_fetch`(失败仅 debug,`:5313`)/ reconcile。同步读 `order.status` 会看到陈旧态,收敛全靠 reconcile 按时运行。

### 🟡 候选-11:reprice 重试双成交窗口

重试前撤单为 best-effort(`session_engine.py:1958` `except: pass`);撤单失败但原单随后成交 → 原单与 reprice 单双成交。

### 🟡 候选-12:trader_positions 唯一约束 NULL 空洞

`uq(trader_id,mode,market_id,direction)`(`database.py:4806`)中 `direction` 可空;Postgres 视 NULL 互异 → 同 `(trader,mode,market)` 多行 `direction=NULL` 共存,持仓聚合可能被拆分。

### 🟡 候选-13:策略层 exec() DB 源码 = RCE 面

`StrategyLoader.load` 用 `exec()` 执行 DB `strategies.source_code`(`strategy_loader.py:749`),仅靠 AST 白名单防护。谁能写该列即可执行任意 Python。这是 `agents.md` 原则4"信任内部代码"风险最集中处。

### 🟡 候选-14:镜像双写静默吞异常

`_mirror_trader_order_to_verification`(`database.py:4529`)吞掉所有异常(`:4566-4570`),侧表可能停留陈旧 PnL(注释称下次写入自愈,取决于 verifier 频率)。

### 架构级观察:`agents.md` 原则4 与实盘严谨性冲突

原则4 明确"不对内部服务调用做输入校验,只在真正边界校验"。CONFIRMED-2 证明这条哲学在金融字段上已产生实际风险(direction 无约束一路穿透)。**面向实盘,建议把关键金融字段(direction/side/status/mode)列为"真正边界",强制枚举 + DB 约束** —— 见产出⑦。

---

## 五、系统跑通验证方案(仅方案,无执行命令)

> 目标:验证干净基座能完整流转,基座本身无问题。全程 **shadow 模式**,不接实盘凭证。

### 前置条件(关键)

- **P0. 用全新数据库。** 策略运行时真身在 DB `strategies.source_code`,catalog 播种绝不覆盖已存在行(`opportunity_strategy_catalog.py:1568`)。复用旧库 = 仍跑旧的被改源码。干净代码 ≠ 干净运行时。
- **P1. 不配 Polymarket 凭证。** 实盘下单需"凭证 + mode=live + 非 read-only + arm token"四者合取,缺凭证天然停纸面。
- **P2. 起齐依赖**:Postgres 16 + Redis 7 + migrate 完成。

### 验证用例(每例:步骤 → 校验点 → 预期)

| # | 业务用例 | 校验点 | 预期 |
|---|---|---|---|
| V1 | **迁移与建表** | `init_database()` 完成后,113 张表存在;alembic head 单一 | 无异常;表齐全 |
| V2 | **plane 启动隔离** | 分别起 trading/news/discovery 三 plane | 各自进程锁生效;trading plane 不被二次启动 |
| V3 | **策略播种** | 新库启动后 `strategies` 表 | 30 个系统策略被 seed;source_code 为出厂版 |
| V4 | **检测出机会** | scanner 跑一轮,`opportunity_state` 表 | 有 Opportunity 产出;bridge 写入 trade_signals |
| V5 | **信号过门禁** | 构造一个 shadow 信号走编排 | decision_gates 逐层记录;`trader_decisions` 有审计行 |
| V6 | **影子开仓扣款** | 触发一笔 shadow 下单 + backfill | `SimulationAccount.current_capital` 减少;`payload.simulation_ledger` 写入 |
| **V7** | **影子平仓入账** ⚠️ | TP/SL/max_hold 触发平仓后查 `total_pnl` | **当前会失败(CONFIRMED-1)** —— 这正是要先修的基座缺陷,V7 是回归判据 |
| V8 | **退出引擎** | exit_risk_loop 对一个 shadow 仓评估 | 输出 hold/reduce/close;陈旧 mark 走告警不盲平 |
| V9 | **风控门禁** | 制造超 `max_open_positions`/`max_daily_loss` 场景 | evaluate_risk 拦截;返回对应 reason |
| V10 | **对账幂等** | 同一信号重复消费 | `trader_signal_consumption` 唯一约束挡住重复 |
| V11 | **状态一致性** | 全流程后校验 trader_orders.status 取值 | 应只出现文档化取值(**当前无 DB 约束保证 —— 见 CONFIRMED-2**) |
| V12 | **崩溃恢复** | 杀 worker 进程重启 | 持仓从 live_trading_positions/DB 恢复;pause 状态对齐(注意候选-7) |

**基座"跑通"判据**:V1-V6、V8-V10、V12 全绿,且 **V7 修复后转绿**、V11 加约束后可强校验。V7/V11 是基座必须先补的两个洞。

---

## 六、基座 / 策略层边界(扩展点 vs 禁改区)

### 6.1 基座底层(改这里 → 影响所有策略,**谨慎/禁改**)

| 模块 | 位置 | 性质 |
|---|---|---|
| **plane 隔离** | `workers/host.py:100` | 🔒 禁改:trading plane 单例是安全铁律 |
| **资金单执行者** | `exit_risk_loop.py` + `position_lifecycle.reconcile_*` | 🔒 禁改:money path 单写者不变量 |
| **BaseStrategy** | `strategies/base.py:466` | ⚠️ 谨慎:时钟/事件路由/默认 evaluate/默认 exit 共享 |
| **StrategySDK** | `strategy_sdk.py:150`(~4000 行,~90 静态方法) | ⚠️ 谨慎:sizing/退出构造/行情/费用共享能力 |
| **StrategyLoader** | `strategy_loader.py` | ⚠️ 谨慎:AST 门禁 + exec + per-trader clone |
| **门禁引擎** | `decision_gates.py` / `platform_gates.py` / `venue_gates.py` / `gate_pipeline.py` | ⚠️ 谨慎:所有交易共用的风控层 |
| **风险管理器** | `risk_manager.py:38` | ⚠️ 谨慎:日损/仓位/敞口/熔断 |
| **持久化 schema** | `models/database.py` | ⚠️ 谨慎:113 表,改动需 alembic 迁移 |
| **执行链** | `session_engine.py` / `order_manager.py` / `live_execution_service.py` | 🔒 禁改:下单/回滚/裸腿处理核心 |

### 6.2 策略业务层(改这里 → 只影响该策略,**扩展点**)

- 单个 `services/strategies/*.py` 的 `detect/detect_async/on_event` 主体;
- 其 `evaluate/custom_checks/compute_score/compute_size` 覆写;
- 其 `should_exit` 覆写;
- 其类属性声明(`scoring_weights`/`sizing_config`/`default_config`/`subscriptions`/`quality_filter_overrides`/`risk_budget`/`market_filters`)。

### 6.3 新增策略的标准姿势(零侵入基座)

1. 新建 `services/strategies/my_strat.py`,`class(BaseStrategy)`,实现 detect/on_event 之一;
2. `opportunity_strategy_catalog.py` 的 `SYSTEM_OPPORTUNITY_STRATEGY_SEEDS` 加一条 seed;
3. 重启触发播种(或经策略编辑器 UI 写 DB)。
4. **无需改任何中央注册表/编排器/风控** —— 这是声明式设计的目的。

### 6.4 现有 30 策略分类(按 source_key)

scanner(16)、crypto(8)、traders(2)、news(1)、weather(1)、sports(1)、manual(1)。

---

## 七、后续迭代开发规范(防止再次改乱基座)

### 7.1 分层纪律

1. **基座核心少侵入,业务在策略层扩展。** 新功能优先做成新策略(6.3 姿势)或新 SDK 静态方法,不改 BaseStrategy/门禁/执行链。
2. **禁改区(6.1 的 🔒)改动必须单独立项 + 操作员评审。** plane 隔离、money-path 单写者、执行/回滚链,任何改动附回归证明。

### 7.2 金融字段硬化(直接回应实盘诉求)

3. **关键金融字段上枚举 + DB 约束。** direction/side/status/mode/verification_status 从自由 String 改为受约束枚举,并加 Postgres CHECK/枚举类型。这是把 `agents.md` 原则4 在金融字段上的例外制度化 —— 修 CONFIRMED-2 的根。
4. **资金变动必须可审计。** 补 append-only 现金分录表(修 CONFIRMED-3),余额由分录求和得出,不可覆盖式更新。
5. **账本口径单一权威。** 明确 shadow/live 各以哪张表为 PnL 权威,消除 TraderOrder.actual_profit 与 SimulationAccount 的双账本歧义(修 CONFIRMED-1 的前提)。

### 7.3 验证纪律

6. **测试必须起 Postgres。** 集成测试覆盖 repository/migration/持仓恢复;缺库跑测试 = 跳过了系统最该验的持久化路径(已实测:缺库 39 红,起库 629 全绿)。
7. **策略测试禁绑具体 slug。** 策略是 DB 动态的,测共享基座行为,不测某个策略类(`agents.md` 明令)。
8. **回归判据显式化。** V7(影子结算)、V11(状态约束)作为基座健康的常驻回归。

### 7.4 变更留痕

9. **工作树保持干净、有远端备份。** 三代项目此前均无异地副本;基座必须有 GitHub 备份,改动走分支 + PR,不在脏工作树上叠改。
10. **给 codex 的 spec 遵循"可检验判据"**:每步带 `文件:行号` 指针,明确允许改的文件白名单,沙箱档位与文件写入要求自洽。

---

## 待核实清单(下一轮针对干净基座)

- [ ] 干净基座的钱包共识(`traders_confluence`)/ manual-buy 路径是否产生非规范 direction(你在 pmr 版遇到的"信号没法下单"是否在基座复现)。
- [ ] 候选-4/5/7/9 等标🟠🟡项逐条核实,升级为 CONFIRMED 或排除。
- [ ] `close_orchestrator_shadow_fill` 修复方案:开开关 vs 改口径,需先定 PnL 权威表。
- [ ] live_risk_clamps 各 `*_cap` 在 worker/session_engine 的确切落地点。
