# 修复 spec:fix-06 shadow 运行时缺陷(实跑暴露)

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`(含 fix-01~fix-04)
> 来源:`docs/diagnosis-live-run-2026-08-12.md` —— 操作员实跑 shadow 模拟账户数小时、零订单,直查运行库与 worker 日志定位。
> 三项互相独立,可分次实施。每项必须带回归测试。

---

## 前置说明(实现方必读)

实跑的**主因是配置**(`.env` 缺 Polymarket 凭证 → 行情管道断 → 多道闸 fail-close),由操作员填凭证解决,**不在本 spec 范围**。

本 spec 处理的是那次实跑**暴露出的三个独立代码缺陷** —— 它们与凭证无关,填了凭证也依然存在。

## 通则

- 只改点名文件 + 对应测试;不做无关重构;不回退 fix-01~fix-04。
- 单一真值源:inline 回退值必须等于 `default_config`;新增 key 同时进 `default_config` + schema。
- 纯逻辑测试免 DB;需要 DB 的真起 Postgres,不得用 skip 冒充。
- 费用计算走 `utils.kelly` canonical helper 并传 category;禁止 legacy 四次曲线。
- 产出 `.piercode/fix-06-last-message.md`:逐条 `文件:行` + 改动 + 为什么 + 测试证据 + 未验证项。
- 分次提交(P-1/P-2/P-3 各一组,前缀工单号);push 前 `git pull --rebase`。

---

## P-1 手动买入不接模拟账本(shadow 下不扣款、订单永不成交)

**文件**:`backend/api/routes_traders.py`(`manual_buy`,约 :2245)

**根因**:shadow 分支只写一行 `TraderOrder`(status=`submitted`、`executed_at=None`),**完全不触碰 `SimulationAccount`**;`place_order` 只在 `if mode == "live"` 分支,shadow 无任何模拟成交。
实测:5 笔 manual 单卡在 `submitted`;`simulation_accounts.current_capital` 仍为初始 10000、`total_trades=0`。
**已核实非 fix-01 引入**(干净基座同样如此)。

**修复**:让 shadow 手动买入走**与自动 shadow 路径一致**的记账口径。

1. **先做对账调研**(必须,写入产出文件):当前 shadow **自动**路径成交后如何记账?fix-01 之后 `TraderOrder.actual_profit` 是编排器 shadow PnL 的唯一权威(见 `docs/fix-01-shadow-settlement.md`),`SimulationAccount` 不再被编排器路径触碰。
   ⇒ **手动买入必须与之一致**:**不要**重新引入"手动路径写 SimulationAccount"的第二套账本 —— 那会重蹈 fix-01 删掉的不对称镜像。
2. **本 spec 的裁决**:手动 shadow 买入应当
   - 通过 shadow 成交路径产生**真实的模拟成交**(复用自动 shadow 用的 fill simulator / 成交价逻辑,不要新写一套),把订单从 `submitted` 推进到 `executed`,写入 `effective_price`、`executed_at`;
   - PnL 与自动路径同源,平仓时经既有 shadow 结算路径写 `TraderOrder.actual_profit`;
   - **不**直接改写 `SimulationAccount.current_capital`。
   - 若 UI 的"模拟账户余额"需要反映手动单占用,那属于**读取侧聚合**(由订单推导),不是再建一套写入账本 —— 在产出文件里说明当前 UI 读的是哪个字段。
3. 若调研发现现有 shadow 成交路径无法被手动入口复用(例如强绑定 signal/decision),**停下来在产出文件记录**,不要自行发明第二套成交逻辑。

**回归测试**:
- shadow 手动买入后,订单状态**不再停留在 `submitted`**(推进到 executed 或明确的失败态),`executed_at` 非空。
- 手动买入**不改写** `SimulationAccount.current_capital`(与 fix-01 的单一权威源一致)。
- live 模式路径行为不变(回归保护)。

## P-2 shadow 模式不应依赖实盘下单凭证

**文件**:`backend/workers/host.py`(live execution 初始化)、`backend/services/trader_reconciliation_worker.py`(WalletStateCache reseeder)、以及消费"新鲜度/WS 定价"的闸门

**根因**:缺 Polymarket **下单**凭证 → `live_execution_service` 初始化失败 → `WalletStateCache` 无法 seed → 新鲜度闸持续拒单。日志自述:"the freshness gate will keep refusing trades. Last init error: missing_polymarket_credentials"。
⇒ **纯 shadow 运行(不下真单)也被实盘凭证卡死**。shadow 只需要行情数据,不需要下单能力。

**修复**:
1. **先调研并在产出文件回答**:`WalletStateCache` / 新鲜度闸对 shadow 模式**是否真的必要**?它守的是"钱包状态新鲜度"(实盘持仓/余额),shadow 并无真实钱包。
2. 按调研结论二选一,**并说明理由**:
   - **(a) 解耦**:shadow 模式下,钱包状态相关的新鲜度闸不适用 → 该闸对 shadow 不生效(或改用模拟持仓状态),行情数据仍走公开端点。
   - **(b) 若确有必要**:则在缺凭证时给出**明确可诊断的拒绝原因**(如 `shadow_blocked_missing_live_credentials`),而不是让下游看到 `source=unknown` 这种无从定位的信息。
3. 无论哪条,**不得**降低 live 模式的安全性:live 缺凭证必须继续拒单。
4. 日志噪音:`Missing Polymarket API credentials` 当前逐秒刷屏(每 ~2-5s 一条 ERROR)。改为**退避重试 + 首次 ERROR 后降级为周期性 WARNING**,避免淹没真实错误。

**回归测试**:
- shadow 模式 + 无实盘凭证 → 不因钱包新鲜度闸被拒(方案 a),或拒绝原因明确可定位(方案 b)。
- live 模式 + 无凭证 → 仍然拒单(安全回归)。
- 凭证缺失时的日志在 N 次后不再逐次 ERROR(可用计数 stub 断言)。

## P-3 `strategy_origin` 漏打标(约 4,226 次拒绝)

**文件**:crypto 系列策略的信号生产路径

**根因**:`btc_eth_directional_edge.py:4583-4607` 的 `source_origin_predicate` 要求 `source=="crypto"` 且(`payload.strategy_origin=="crypto_worker"` 或 `signal_type` 以 `crypto_worker` 开头)。
生产者**多处已打标**(`:4422/:4478`、`btc_eth_convergence.py:4389/4445`、`btc_eth_maker_quote.py:4387/4443`),`position_lifecycle.py:1581` 另有兜底补写。
但实测失败样本为 `source='crypto' signal_type='crypto_opportunity'` ⇒ **存在另一条生产路径未打标、且 signal_type 不匹配前缀**。

**修复**:
1. **先定位**那条产出 `signal_type='crypto_opportunity'` 却未带 `strategy_origin` 的生产路径,在产出文件列出 `文件:行`。
2. 在**该生产路径**补打 `strategy_origin="crypto_worker"`(与既有打标点一致)。
   - **优先修生产者**,不要放宽消费侧闸门 —— 闸门本身是正确的来源校验。
3. 若发现该路径**本就不该**被 crypto 闸门消费(即路由问题而非打标问题),停下来记录,不要强行打标掩盖。

**回归测试**:该生产路径产出的信号带 `strategy_origin="crypto_worker"`,能通过 `source_origin_predicate`;未打标的信号仍被拒(闸门未被放宽)。

---

## 完成判据

- 三项各带回归测试,`pytest` 相关用例绿。
- P-1/P-2 的**调研结论**写入产出文件(它们是"先查再改"的项,不是直接照做)。
- 不回退 fix-01~fix-04;不引入第二套 shadow 账本。
- `.piercode/fix-06-last-message.md` 写清每处改动 + 证据 + 未验证项。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支分次提交并推送。

## 不在本 spec 范围

- **填 `.env` 的 Polymarket 凭证** —— 操作员动作,是实跑零订单的主因。
- **体育策略无信号产出** —— `trade_signals` 中不存在 sports 源,需单独排查数据源层,本 spec 不涉及。
- orderflow / WS 行情缺失本身 —— 填凭证后应恢复;若填了仍缺,再单独立项。
