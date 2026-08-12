# HANDOFF:策略优化项目上下文与实施计划

**日期**:2026-08-12
**面向**:接手本项目的新会话(需具备 GitHub 推送权限)
**前置**:阅读本文即可接手,不需要原会话的对话历史。

---

## 0. 三十秒摘要

Homerun 是一套预测市场(Polymarket + Kalshi)交易系统,已有 30+ 策略。本项目分两条线:

1. **修复线**:现有策略存在大量死配置、量纲错误、fail-open 与公式错误。已产出 10 张工单(WO-A1~A5、WO-B0~B4)。**尚未施工。**
2. **新策略线**:调研后锁定 7 个「公开竞争稀薄」的方向,已为前 3 个写出实现规格。**缺口 3 的第一块代码已完成并通过测试。**

**最要紧的一条**:仓库 `backend/utils/kelly.py:78` 的 Polymarket 费用公式是 2026 年之前的四次曲线 `p·0.25·(p(1-p))²`,**已经过时**。现行制度是二次曲线 `C·feeRate·p·(1-p)`。所有费用感知闸门都建立在错误曲线上,中间价位误差约一个量级。这个必须先修(WO-B0),否则下游所有 EV 计算都不可信。

---

## 1. 仓库状态(接手第一件事)

### 1.1 未推送的提交

原会话所在的沙箱被 git proxy 挡住了写操作(`access denied by the git proxy: ... not in this session's authorized repository set`,已确认是上游开放 bug [anthropics/claude-code#76248](https://github.com/anthropics/claude-code/issues/76248),Cowork 会话里没有添加仓库的 UI)。因此代码以 patch 形式交付。

本地分支 `claude/strategy-repair-specs-jvcsc5`,基于远端已存在的 `dbf0c5c3`(= `origin/claude/batch-2-identity-finality-review-jvcsc5` 的 tip),有 3 个提交:

```
103b50c2  feat(markout): pure markout/adverse-selection core (Gap 3, step 1)
b3d1eca5  docs(repair): spec addendum v1.1 (2026 fee regime) + phase-2 strategy gap analysis
0e4daa6b  docs(repair): strategy repair specs, optimization plans & data-recording guide
```

新增 8 个文件,**不修改任何既有代码**,因此在任何 base 上打都不冲突:

```
backend/services/markout/__init__.py
backend/services/markout/curves.py
backend/tests/test_markout_curves.py
docs/repair/HANDOFF_上下文与实施计划.md          <- 本文
docs/repair/traders_copy_trade_p0_draft.patch
docs/repair/修复施工规格书_两批策略.md
docs/repair/数据录制链路指南.md
docs/repair/新策略选型与缺口分析报告.md
docs/repair/策略优化方案_信号新闻跟单类.md
docs/repair/策略优化方案_加密高频BTC_ETH.md
```

### 1.2 落地方式

补丁文件已发给用户。在有推送权限的环境里:

```bash
cd ~/homerun
git fetch origin
git checkout -b docs/strategy-specs origin/main    # 纯新增文件,落 main 无冲突
git am ~/Downloads/*.patch
git push -u origin docs/strategy-specs
```

`git am` 保留原始 commit message 与作者信息。若补丁丢失,所有文档内容同时存在于 claude.ai 项目「策略」中,可重新导出。

### 1.3 部署提醒(容易忘,忘了就白干)

`backend/services/strategies/*.py` 是 **SEED TEMPLATE**。合并到 main **不等于线上生效**——运行时读的是数据库 `strategies.source_code`。每次策略代码变更后必须同步 DB(UI 策略编辑器或 API),否则改了个寂寞。同步时注意**只推代码,不要覆盖用户已调好的参数**。

---

## 2. 关键约束:谁能做什么

原会话的计划里写了一张「W1-W8 由 AI 会话连续实施」的甘特图,**那是错的,已作废**。真实约束:

| 能力 | 云端 AI 会话 | 需要人/本机环境 |
|---|---|---|
| 写纯逻辑代码 + 单测 | ✅ 可离线自证 | 审查合并 |
| 写规格 / 调研 / 设计 | ✅ | 定方向 |
| 接 Polygon RPC、连 Postgres | ❌ 够不着 | ✅ |
| 跑真实数据回测 / walk-forward | ❌ | ✅ |
| shadow / 实盘部署 | ❌ | ✅ |
| 跨会话状态保存 | ❌ 容器闲置即回收 | 靠 git + 项目文档 |

**推论**:AI 侧的产出必须是「不依赖运行环境就能验证正确性」的东西——纯函数、单测、规格。凡是需要真实数据才能验证的部分(摄取、回填、回测),AI 写代码,人跑验证。节奏由施工进度决定,不由日历决定。

---

## 3. 修复线(WO-A1~B4)

**完整工单见** `docs/repair/修复施工规格书_两批策略.md`。以下是接手要点。

### 3.1 施工总则(必读)

1. 每批一个分支:`fix/batch-a-signal-copytrade`、`fix/batch-b-crypto-hf`;每工单一组 commit,message 前缀工单号。
2. **单一真值源**:任何 `params.get(key, 魔法数)` 的 inline 回退值必须与 `default_config` 一致。新增 key 必须同时进 `default_config` + schema。
3. **零死配置**:每个 `default_config` key 在文件中出现 ≥2 次(定义一次 + 至少一处读取)。重点检查 `directional_*`、`convergence_*`、`maker_quote_*` 三块——目前**整块是死的**。
4. 不引入新依赖;改完跑 `python3 -m py_compile`。
5. 每工单验收点写进 `backend/tests/test_<strategy>_repair.py`,纯逻辑不依赖 DB/网络。
6. 默认值变更列成表(旧→新→理由)写进 PR,供 shadow A/B 对照。
7. **禁止虚构**:规格与代码现实冲突时停下来记录在 PR,不要自行发明替代方案。

### 3.2 施工顺序

```
WO-B0(共享层,先做)
   ├── WO-B1 / B2 / B3 可并行(不同文件)
   └── WO-B4(五个小策略)
批次 A 五个工单相互独立,可全并行
```

### 3.3 WO-B0 是最高优先级

费用统一 + timeframe-aware 门槛统一 + 共享 `estimate_p_win` helper。**特别是费用曲线**:

- 现行 Polymarket:`taker fee = C × feeRate × p × (1−p)`,feeRate 按类目——加密 0.07、体育 0.05、政治/金融/科技 0.04、地缘 0;**maker 零费**,且 taker 费的 15–25% 每日返给 maker。
- 现行 Kalshi:taker 0.07 曲线 + **maker 0.0175 曲线**(2026-07-07 起开始收 maker 费)。
- 实现:做成 `fee_rate_by_venue_category` 可配置,`kelly.py` 旧四次曲线保留为 legacy fallback 并标注 deprecated。
- **施工当天对照两家官方 fee 页再核一次**——这个制度一年内已经变过两次。

注意 `backend/utils/kalshi_taker_fee` 已经是正确的二次形状,只有 Polymarket 那支是错的。

### 3.4 一个需要你拍板的行为大改

**WO-B2(`btc_eth_convergence.py`)会改变线上行为。** 该文件的 docstring 宣称做「收敛模式」,实际代码做的是延迟套利(latency arb),名实不符。规格的决定是:把延迟套利交给 `btc_eth_directional_edge.py`,把 convergence 改回文档语义(最后 5–45 秒、0.85–0.95 价区、买 oracle 有利侧)。

**如果线上依赖旧 convergence 行为,合并前必须先 shadow 对照确认。**

---

## 4. 新策略线(Phase 2a)

**完整调研见** `docs/repair/新策略选型与缺口分析报告.md`(含 30+ 条已核实 URL 与 [VERIFIED]/[INFERRED] 标注)。

### 4.1 先看反面清单——别在这些方向浪费预算

| 已死/垂死 | 证据 | 对现有策略的含义 |
|---|---|---|
| YES+NO 捆绑套利、组合套利 | 2024-04→2025-04 全年提取约 $40M;到 2026 年 NBA 市场一个月仅 7 次可执行机会、中位存活 3.6 秒、可执行深度约 15 股 | `basic`/`ctf_basic_arb`/`negrisk`/`combinatorial` 降级为零边际成本的拾荒后备 |
| Poly↔Kalshi 跨平台套利 | 数十个公开 repo + 零售仪表盘直接卖机会流 | `cross_platform` 同上降级 |
| 加密 5m/15m 延迟抢跑(taker) | **制度性死亡**——Polymarket 动态 taker 费就是为杀它而设;7.27 亿行研究:报价对 Binance 中位滞后 347ms,但朴素延迟策略**费后每次尝试净 −0.116** 标准化收益单位 | directional/convergence/maker 三大引擎正在做这件事。修复后也**别指望 taker 模式盈利**,产出应作为 maker 引擎的信号层 |
| 朴素跟单 | 顶层 1% 交易者拿走 76.5% 利润且**以 maker 限价单方式赢**;鲸鱼已反侦察(冰山、拆仓) | `traders_copy_trade` 修完 P0 后定位为辅助信号 |
| 头部市场奖励 farming | 从 ~200-300 USDC/天/万U 衰减到 ~10%/年 | 只作为 maker 策略的副收入项建模 |

衰减速度背书:已发表因子样本外收益平均降 26%、发表后降 58%;预测市场套利 18 个月内从 $40M/年衰减到 $210/月。**任何边的半衰期都是月级**——这是为什么验证纪律不能省。

### 4.2 缺口 3:Markout 仪表化(进行中)

**地位**:所有 maker 策略的前置。纯数据工程,无市场风险。

**已完成** (commit `103b50c2`):`backend/services/markout/curves.py` — 纯计算核心,43 个单测全绿。包含 markout 计算、cohort 分类、TTL 分桶、cell 聚合。零 DB/网络/时钟依赖。

其中有两个设计决定,**接手后如果不同意,现在改比接上 RPC 之后改便宜得多**:

1. **默认分母是 $1 结算面值,不是 mid。** 二元合约永远结算 0 或 1,所以 1 分钱的逆向移动对 maker 就是每股 1 分钱损失,不管合约在 2c 还是 90c 交易。用股票惯例除以 mid,会把这两个同样的损失算成 −5000bp 和 −111bp,让低价合约看起来毒性奇高——会直接毒化缺口 1 的撤单阈值。`PER_MID` 保留但仅供对照文献。
2. **空输入抛异常,不返回零。** 零填充的 `MarkoutStats` 读起来像「中性流」,而这恰恰是这套仪表要抓的静默失败。同理 `classify_cohort` 数据缺失返回 `UNKNOWN` 而不猜。

**下一步(需要运行环境,AI 写不了验证)**:

| 步骤 | 内容 | 谁 |
|---|---|---|
| 3a | Polygon RPC 接入,摄取 `OrderFilled` 事件 → parquet + Postgres | 人(需 RPC key) |
| 3b | 每小时离线聚合 → `markout_curves` 表 | AI 写 / 人跑 |
| 3c | 毒性实时评分模块,latency <50ms | AI 写 / 人验 |
| 3d | LP 竞争密度(HHI) | AI 写 / 人验 |
| 3e | 回测 replay 接口 | AI 写 / 人验 |

**为什么必须接链上**:Polymarket 公开 WS 流做买卖方向推断**只有 ~59% 准确率**(对 3.03 亿条链上 `OrderFilled` 验证)。不接链上,任何有效点差/毒性度量都是沙上建塔。

**验收**:OrderFilled 覆盖 >99%;markout 曲线覆盖政治长尾 ≥50 市场、加密日线 ≥100 市场;毒性评分对链上成交方向预测准度 >55%;应用 markout 后回测 Sharpe **下降** 15–25%(这是对的——以前忽略了被吃掉的成本)。

### 4.3 缺口 1:GLFT/pm-AMM 做市引擎(旗舰)

**依赖**:缺口 3 的 markout 表 + WO-B3 修复完成。**在这两件事完成前不要开工。**

**经济学**:Polymarket 中位报价点差 ~400bp(中间价位)、低价区 1300–1800bp;中位每市场仅 ~32 个有效 LP,长尾常常只有 1 家;maker 零费 + 15–25% 返佣。官方开源做市 bot(poly-market-maker)只有 Bands/AMM 两个策略、无库存风险模型、无波动缩放、无逆向选择逻辑,且 2023 年后冻结。**做市理论在加密圈已工业化,但没有任何公开实现迁移到预测市场。**

**核心数学**:保留价 `p* = Φ(S/(σ√τ))`,其中 `S = γ·I/σ`(库存信号)。关键性质:τ→0 时不再报两侧;库存为 0 时 p*≈0.5;库存偏移 ∝ `I/(σ√τ)`。局部方差 ∝ `φ(Φ⁻¹(p))²/(T−t)`——在 p=0.5 峰值、临到期爆炸。

**必须实现的四件事**:库存硬界(±3σ)、临期挂单缩减日程、毒性触发撤单(接缺口 3)、返佣建模。

**回测特别注意**:1¢ tick 的 [0,1] 价格是**极端 large-tick 市场**,排队位置一阶重要。必须做排队仿真,否则回测结果没有意义。

### 4.4 缺口 2:校准偏差收割(独立,可立即开工)

**零依赖**——不需要缺口 3,不需要新数据。修复线在跑的时候这条可以并行。

**证据(全清单最硬)**:2.92 亿笔交易、32.7 万合约的研究显示 FLB 随剩余期限增长,>1 个月政治市场校准斜率 1.32;**离结算 1 周的 70c 政治合约真实概率 ~83%**。Kalshi 30 万+ 合约:5c 合约只赢 ~2%,taker 平均亏 32%、maker 亏 10%。

**反例(必须做类目过滤)**:宏观/CPI 类**无此偏差**。黑名单:macro、sports、resolved、combo。

**执行要点**:政治/地缘类、剩余期限 7–30 天、价位带 [0.60,0.90] 买有利侧;**只用限价单**(maker 零费,且低价区 1300+bp 点差决定了 taker 执行会吃掉全部理论边);分数 Kelly(0.25)+ 单市场硬上限防肥尾;结算前 12h 平仓避开 UMA 争议窗。

**风险**:肥尾(favorite 翻车)、资金效率(押 95 赚 5)、FLB 被机构学习后衰减。监控月度 Sharpe 趋势,跌破 0.5 关闭。

### 4.5 缺口 4~7(未展开,按需)

4. **同资产期限结构一致性**(5m/15m/1h/4h 阶梯)——公开工作为零的白地,Homerun 是唯一原生具备该数据面的系统。**先监控模式攒分布,不要直接交易。**
5. **洞察流雷达**——把 whale 基建从「跟单」重定向为「内幕签名检测」。双刃剑:做 maker 时这同时是撤单信号。
6. **新场地先发**(Polymarket US / Kalshi perps)——是接入工程不是策略。**准入 KYC 是硬约束,需自查资格。**
7. **结算窗口交易**(UMA 提案窗)——「抢提案」已被制度关闭,但「为争议概率定价」没有公开工具。排最后。

---

## 5. 推荐执行顺序

```
立即(并行三条):
  ① WO-B0 费用曲线修正        <- 最高优先级,下游一切依赖它
  ② 缺口 2 校准收割           <- 零依赖,可独立推进
  ③ 缺口 3a Polygon RPC 接入   <- 需要 RPC key,起步最慢先启动

WO-B0 完成后:
  ④ WO-B1/B2/B3 并行 + 批次 A 五工单并行
  ⑤ 缺口 3b~3e(markout 表 → 毒性 → 回测接口)

修复合并 + 缺口 3 完成后:
  ⑥ 缺口 1 GLFT 引擎          <- 旗舰,但严格排在依赖之后

数据攒够 30 天后:
  ⑦ 缺口 4 期限结构(监控模式)、缺口 5 洞察流雷达

机会型:
  ⑧ 缺口 6 新场地、缺口 7 结算窗口
```

---

## 6. 验证纪律(全部适用,不可省)

1. **修复前后同窗对照**:每策略跑 `run_execution_backtest`,修复前 vs 修复后同一时间窗。
2. **walk-forward**:`n_folds=6, train_ratio=0.5` 默认,**至少需要 30 天数据**。
3. **回测必须对链上成交验证**(59% 方向准确率问题)。
4. **shadow ≥1 周**再小额实盘。宁可 shadow 3 周也不要急上线。
5. **预注册止损标准**:每策略上线前先写死「shadow 夏普 < X 或 markout 持续为负则砍」,防止沉没成本驱动决策。

**数据前提**:book parquet 默认只留 7 天(`book_retention_days=7`),crypto 事件总线 topic 同样 7 天。**walk-forward 需要 30 天——必须先把留存拉到 ≥30 天再攒数据,被裁掉的窗口不可恢复。** 详见 `docs/repair/数据录制链路指南.md`。

---

## 7. 静默毁数据的坑(逐条确认过)

- `backend/data/cache/parquet_retention.json` 是**死配置**,全仓库无代码读它,改它无效。真实留存在 `recorder_config_json` + `topic_catalog`。
- **磁盘守卫**:空闲 <10GB 时录制**静默丢弃** flush 并紧急裁窗口。给数据盘留足空间。
- **docker-compose 部署 = 不录制**:compose 只定义了 trading/news/discovery 三个 worker,没有 recording/detection/jobs plane,且容器内 parquet 路径不在挂载卷里(容器重建即丢)。GUI 启动器跑则没问题。
- **`oracle_history` 只存在于自己录的窗口**:外部导入的 parquet 走 gap-fill 投影,投影行没有该字段。加密策略只能在自录窗口上高保真回测。
- **`trade_signals` 表是 UNLOGGED**(非正常关机直接清空)且终态信号 24 小时被清理。回测的持久基底是 parquet + 事件总线,不要依赖旧信号;跑 `discover_from_history=True` 让策略在录制数据上重新检测。
- **`market_settlements` 没有任何常驻进程写入**。加 cron 每天跑 `python -m scripts.backfill_market_settlements --days 30`,否则严格离线回测没有结算真值。

---

## 8. 诚实声明

- 「缺口」= 截至 2026-08-12 公开证据下竞争稀薄的方向,**不等于保证盈利**。这些判断本身会随他人进入而衰减,文献量化的半衰期是月级。
- 所有容量/收益数字是文献值或粗估,**必须**用自己录制的数据重新测量。
- 实盘涉及真实亏损风险,以及(内幕流跟随、多场地准入方面的)合规风险。资金管理决策在用户。

---

## 9. 接手后的前三个动作

1. `git am` 打上补丁,推到远端,确认 8 个文件都在。
2. 读 `docs/repair/修复施工规格书_两批策略.md` 的施工总则 + WO-B0,**先把费用曲线修对**。
3. 跑一次数据自检(`curl -s localhost:8000/api/dataset/recorder/recording`),确认 `actively_recording=true`,然后**立刻把 `book_retention_days` 拉到 ≥30**——这一步越早做越好,留存不改后面 walk-forward 无米下锅。
