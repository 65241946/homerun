# HOMERUN 在线终局结算与可审计收益账本设计

## 1. 目标与边界

本设计在 HOMERUN 现有交易基座内建立一条面向实盘的统一终局链：订单在成交时固化市场身份，冷平面获取并保存官方终局事实，结算协调器以唯一幂等凭证推进 Shadow 或 Live 状态，模拟账户用不可变现金分录证明余额，Live 账户则把“市场终局、可领取、链上兑换、资金到账、收益核验”分层记录。

本次修复覆盖 Polymarket 上由 HOMERUN trader orchestrator 管理的 Shadow 与 Live 订单。它不修改策略阈值、仓位算法、跟单筛选、下单权限或实盘密钥，也不把 legacy 通用模拟器的历史聚合值冒充为完整账本。

## 2. 已确认的根因

1. `TraderOrder.market_id` 实际多为 Gamma 数字内部 ID，但不同模块把裸字符串猜成 condition ID 或 token ID；数字 Gamma ID 会被 token 查询路径误接收。
2. 交易热路径保存了完整 payload，但因低延迟要求禁止 REST；允许 REST 的终局 watchdog 又把订单投影成不含 payload 的 `SimpleNamespace`，导致 condition ID 和 token ID 丢失。
3. `reconcile_shadow_positions` 同时承担行情标记、策略退出、终局判断、订单状态更新和模拟账本关闭，职责过重；终局网络查询与高频交易周期互相牵制。
4. `close_orchestrator_shadow_fill` 没有行锁、条件状态更新或结算唯一键。并发关闭同一账户上的不同交易时，基于旧余额生成的绝对值 UPDATE 可能丢失其中一笔现金/PnL 增量；同一订单也可能重复触发内存状态和反向信号副作用。
5. `SimulationService` 仍有 `close_orchestrator_shadow_fill` 与旧 `resolve_trade` 两套派彩逻辑，费用口径和 commit 所有权不同。
6. Live verifier 有多条核验实现，其中 `verify_orders_against_market_resolutions` 没有进入正式 worker 调度；`TraderOrderVerification` 的 ORM 镜像异常会被静默吞掉。
7. 模拟账户只有 `current_capital` 等聚合字段，没有可重建余额的不可变现金分录。

## 3. 必须成立的不变量

### 3.1 市场身份

- 新的 Polymarket 订单必须同时固化 `provider_market_id`、`condition_id`、`token_id` 和 `outcome_index`。
- `condition_id` 必须是 32 字节十六进制；`token_id` 必须出现在该市场的 token 数组中；`outcome_index` 必须与 token 数组位置一致。
- 任何模块不得再根据字符串是否全数字来推断 ID 类型。
- 身份不完整或互相矛盾时：Shadow 订单拒绝进入 proof-grade 账本；Live 订单在提交到场所前 fail closed。

### 3.2 终局事实

- 终局状态只由冷平面获取，交易热路径不发起 Gamma/RPC REST 查询。
- 允许结算的 Gamma 事实必须同时满足：`closed=true`、不再接受订单、UMA/官方状态明确为 resolved、token/outcome 数组对齐，并能得到唯一 winning token。
- 若没有显式 winner，可在上述终局条件全部成立时，由严格的终局价格向量推导：恰好一个 token 为 1、其余为 0；不使用 0.98 等近似阈值做最终派彩。
- 新观测与已保存 final 事实冲突时，事实进入 `conflicted`，停止新增结算并报警；系统不得自动改写历史余额。

### 3.3 经济效果

- 每个订单、每种结算类型最多有一个生效结算凭证。
- 重试、进程重启、重复 provider 响应和两个 worker 并发运行，必须产生相同的最终余额、PnL、订单状态和现金分录数量。
- Shadow 结算凭证、交易、持仓、现金分录、账户投影和订单状态在同一数据库事务提交；任一步失败全部回滚。
- 账户聚合余额必须等于 opening entry 与全部现金分录之和，误差上限为 USDC 最小记账精度；不一致时停止该账户的新 proof-grade 写账并报警。
- 已入账事实需要更正时只能追加 reversal/adjustment，不允许 UPDATE 或 DELETE 历史现金分录。

### 3.4 Live 真实性

- `market_final` 只表示市场结果确定；`claimable` 表示持仓可兑换；`redeem_submitted` 和 `redeem_confirmed` 分别表示链上交易提交与确认；`cash_verified` 才表示真实资金已核验到账。
- Gamma 终局可形成 projected payout，但不能直接标记真实资金到账。
- Live 实际收益优先使用机器人自身 CLOB 成交 lineage、链上 redemption 回执或可归因 closed-position 证据；共享钱包的无订单关联 FIFO SELL 不得覆盖机器人收益。

## 4. 数据模型

所有 schema 变更均为 additive，接续运行库当前 Alembic head `202608100001`。

### 4.1 `trader_orders` 身份字段

新增可空字段用于兼容历史行：

- `venue`: 当前为 `polymarket`。
- `provider_market_id`: Gamma 数字市场 ID。
- `condition_id`: CTF condition ID。
- `token_id`: 本订单实际持有/交易的 outcome token。
- `outcome_index`: token 在市场 token 数组中的位置。
- `identity_status`: `complete`、`legacy_inferred`、`ambiguous`、`invalid`。

新订单必须为 `complete`；历史订单只能通过只读预览和显式补偿流程升级。
历史订单的升级严格拆成三个动作：

1. `observe` 可用纯函数 `identity_from_order` 在内存中得到 `legacy_inferred`，仅用于抓取并保存官方终局事实；不得回写订单 identity，也不得产生 settlement/journal。
2. 历史补偿 preview 使用同一推断结果生成 proposed typed identity、官方事实和候选经济值；摘要必须覆盖订单版本、原始 payload、proposed identity 与 resolution evidence。
3. 只有显式 apply 在摘要复核通过后，才可在同一个数据库事务内固化 typed identity、建立 legacy checkpoint 并委托统一 coordinator 结算。普通 `shadow` worker 仍只自动处理已持久化安全 identity，不能把运行时推断直接变成自动经济写入。

订单时 `complete` 的判定是：存在明确 outcome token、唯一 outcome index，且至少存在一个显式市场标识（provider market ID 或 condition ID）。`condition_id` 可以由冷平面根据 provider market ID/token 补全，但在进入终局结算前必须已唯一确定；交易热路径不得为补全它发 REST。

### 4.2 `online_market_resolutions`

保存每个市场的当前规范化终局事实，唯一约束为 `(venue, condition_id)`：

- provider market ID、condition ID、token/outcome 数组；
- `state`: `open`、`closed_pending`、`final`、`conflicted`、`invalid`；
- winning token、winning outcome index/label；
- provider resolved time、首次/最后观察时间、finalized time；
- 事实版本、最新 evidence hash 和规范化证据 JSON。

### 4.3 `online_market_resolution_observations`

保存追加式 provider 观察证据。唯一约束 `(provider, condition_id, evidence_hash)` 防止同一语义快照重复插入。`evidence_hash` 只对规范化业务字段计算，不包含抓取时间。

### 4.4 `trader_order_settlements`

保存订单级结算凭证，唯一约束 `(trader_order_id, settlement_kind)`，并对 `idempotency_key` 单独唯一：

- mode、resolution fact/version、held/winning token；
- quantity、cost basis、gross payout、fee、net payout、realized PnL；
- `status`: `detected`、`applied`、`projected`、`verified`、`manual_review`、`reversed`、`failed`；
- `authority`: `gamma_final`、`ctf_payout`、`bot_sell_fill`、`closed_position`、`redeem_receipt`、`manual_adjustment`；
- evidence hash、尝试次数、最后错误和各阶段时间。

现金字段使用固定精度 Decimal/NUMERIC；不以二进制 Float 作为新账本事实。

### 4.5 `simulation_cash_ledger_entries`

保存 orchestrator Shadow v2 账户的不可变现金分录：

- account、trade、order、settlement 关联；
- `entry_type`: `opening_balance`、`entry_debit`、`settlement_credit`、`reversal`、`manual_adjustment`；
- 有符号 USDC amount、币种、发生时间、幂等键、证据 JSON；
- `idempotency_key` 唯一。

`SimulationAccount` 新增：

- `ledger_version`: legacy 为 1，proof-grade 为 2；
- `ledger_integrity_status`: `legacy`、`complete`、`checkpointed`、`mismatch`、`blocked`；
- `ledger_verified_at`。

只有 version 2 且 integrity 为 `complete` 的账户可显示“账本已核对”。

## 5. 服务边界

### 5.1 `market_identity.py`

提供不可变 `MarketIdentity` 和显式解析函数。输入必须标明 ID 类型；它可从完整订单 payload 兼容提取历史身份，但返回 `confidence/evidence`，不直接写数据库。

### 5.2 `online_resolution.py`

负责 provider adapter、严格 finality 判断、证据规范化和 hash。网络抓取与数据库写入分离：先释放数据库连接并限流抓取，再用短事务 UPSERT fact/observation。

### 5.3 `settlement_coordinator.py`

只消费已保存的规范化 final facts，不直接联网。它按订单生成确定性 idempotency key，调用 Shadow 或 Live adapter，并返回结构化结果。它不执行策略退出、不创建反向交易、不改变策略参数。

### 5.4 `simulation_ledger.py`

负责 orchestrator Shadow v2 的 entry/settlement journal 和账户投影。锁顺序固定为 account → trade → position → order → settlement；使用数据库行锁、唯一约束和条件状态转换。旧 `resolve_trade` 继续供 legacy API 使用，但不得处理 v2 账户；所有 orchestrator 结算只走新 adapter。

### 5.5 Live adapter

市场 final 后创建 projected settlement；现有 bot-lineage、closed-position 和 redeemer 证据将其升级为 verified。Live adapter 不修改链上余额，只记录本地可审计状态；实际 redemption 仍由已有 `redeemer_worker` 的安全门禁执行。

## 6. Worker 调度

复用 `worker-host` 的独立 `reconciliation` 冷平面：

1. 扫描接近到期或已过期但未 final 的订单，按 condition ID 去重。
2. 每轮最多 100 个市场、HTTP 并发 4、单请求 8 秒超时；网络阶段不持有数据库事务。
3. 保存 observation/fact 后，另起短事务批量领取待结算订单。
4. 通过 `FOR UPDATE SKIP LOCKED` 或等价条件领取，避免多个实例处理同一行。
5. 失败按市场指数退避；身份冲突、终局冲突和账本不一致不自动重试写账，进入人工处理队列并告警。

运行模式只有一个总开关：`off`、`observe`、`shadow`、`live`。默认 `observe`；部署不会自动开启 Live。

## 7. 历史补偿

历史订单默认只读：

1. 生成 identity、官方终局、现有订单/模拟交易/持仓、候选 payout/fee/PnL、余额变化和阻断原因。
2. 将订单分为 `safe`、`ambiguous`、`blocked`。
3. 对规范化预览计算 SHA-256 digest，并包含订单 `updated_at`/事实版本，防止预览后状态变化。
4. 应用接口必须提交相同 digest 和明确确认；digest 失效则拒绝写账并要求重新预览。
5. 应用时仍经过正常幂等结算事务，不提供直接 UPDATE 余额的旁路。
6. 对可唯一推断的历史 identity，apply 必须把 identity 固化与 settlement 放在同一事务；任一失败整体回滚。含糊或非法 identity 不得升级。

当前纽约和西雅图两笔卡单只进入预览。此前只读计算的候选净收益合计为 `+$1.274244382887093`，该数字在正式预览重新抓取事实并核对费用前不视为已实现收益。

## 8. API 与运维输出

不新增图表，仅提供现有界面/API 可读取的结构化状态：

- settlement worker：最后成功抓取、待处理、final、applied、verified、manual-review、冲突和重试计数；
- 订单：identity 状态、market final 状态、settlement 状态、projected/verified PnL、evidence authority；
- 模拟账户：ledger version、integrity、journal balance、aggregate balance、delta；
- 历史补偿：只读 preview 与显式 apply；apply 默认禁用，需运行态开关和确认 digest。

未配置 Polymarket Live 凭据时，健康状态为 `not_configured`，不每 5 秒输出 ERROR；状态变化或节流周期才记录一条日志。Shadow 终局抓取不依赖 Live 私钥或下单凭据。

## 9. 测试与故障注入

必须先 RED 后 GREEN，至少覆盖：

- 数字 Gamma ID 不再被当作 token ID；identity 完整、缺失、冲突和多 outcome 映射；
- finality 必须具备完整终局信号，开放市场和近似 0/1 价格不能派彩；
- observation 重放去重、final 后冲突进入 `conflicted`；
- 同一订单顺序重放和两个并发 session 只产生一条 settlement/credit；
- 同一账户两笔并发结算不丢余额增量；
- 在 settlement、cash entry、account projection、order close 各边界注入异常，事务均完整回滚；
- 进程重启后从数据库恢复并继续，无内存状态依赖；
- journal 重建余额与账户投影一致，篡改聚合值会进入 mismatch/blocked；
- observe 模式只写事实，不关闭订单或改变余额；
- Live 无凭据保持 not_configured，Shadow 仍能抓取终局；
- 历史 preview 不写库，digest 过期拒绝 apply，重复 apply 经济效果不变；
- 现有策略、交易热路径 no-REST、verifier、redeemer 和前端构建不回归。

并发/锁测试必须使用 PostgreSQL，SQLite 测试只用于纯函数和普通事务行为。

## 10. 分阶段上线与回滚

1. 备份 schema 和相关订单/模拟账户行；记录当前镜像、Git SHA、migration head。
2. 部署 additive migration，运行 schema/模型测试，不启用 worker 写账。
3. `observe` 模式保存终局事实并生成差异报告，与旧 lifecycle 只读判断对照。
4. 为新建或校验通过的 Shadow v2 账户启用 journal；完成重复、并发、断网、超时、重启和冲突测试。
5. 生成历史补偿预览，人工核对后才允许单次 apply。
6. Shadow 稳定后运行 Live readiness：身份完整率、钱包 cache、CLOB/RPC、余额/allowance、时钟、费用和告警全部通过；仍不自动下单。
7. Live 只能由用户另行批准启用。

紧急回滚只需把 runtime mode 切回 `observe/off` 并回滚应用镜像；additive 表保留以保存审计证据。已经生效的现金分录不删除，如需更正必须追加 reversal。数据库 downgrade 仅允许在尚无任何 settlement/cash entry 时执行。

## 11. 验收标准

- 当前卡住市场能由冷平面识别为 final，历史 preview 给出逐笔可核验收益，但部署本身不自动写余额。
- 同一组结算输入重复执行 100 次，订单状态、settlement 数、cash entry 数和账户余额保持不变。
- 两笔同账户结算并发运行，最终余额等于串行数学结果，且每笔恰好一个 credit。
- 模拟账户聚合余额与 journal 重算差为 0（按 6 位 USDC 精度）。
- Gamma 超时、返回不完整、冲突和数据库事务中断均不产生半结算。
- 结算网络和写账不进入交易热路径，交易周期不因新链增加 REST 等待。
- 所有新增行为有看过失败的回归测试；聚焦测试、相关回归、迁移检查、固定源码镜像构建和运行态 smoke check 均通过后，才能称为修复完成。
