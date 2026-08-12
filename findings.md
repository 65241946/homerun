# Findings & Decisions: HOMERUN 实盘级结算链

## Requirements
- 基于 HOMERUN 现有完整基座复用成熟能力，不做脱离项目架构的旁路系统。
- 不接受针对两笔天气单的最小补丁；需覆盖市场身份、终局事实、Shadow/Live 结算、收益账本、事务、恢复、告警和审计。
- 面向未来实盘，任何收益和余额写入必须幂等、可追踪、可重放、可回滚验证。
- 先检查配置和真实链路，再判断代码问题；不臆想，不把盘口 0/1 当作官方已结算。
- 当前实盘账户暂未配置；先在模拟环境跑通并证明正确性。
- Phase 7 新增边界：解释共识钱包为何未产生订单、第二模拟账户来源，并逐模块核对天气/体育/加密/新闻链路；必须先区分配置、数据、门禁和代码问题，当前只读诊断。

## Research Findings
- Phase 8 初步变更归因：`_RUNTIME_TRIGGER_DEFAULT_CYCLE_TIMEOUT_SECONDS=10.0`、`_run_trader_once_with_timeout` 的 task cancel 路径以及 timeout 后仅写 `cycle_timeout` 事件的结构，均来自上游 Braedon Saunders 2026-03-12/03-22 等提交；`HEAD` 与 `origin/main` 在该文件无差异。当前未提交 diff 只改了 Shadow simulation ledger backfill 的 stale marker/commit 处理，没有改 timeout wrapper。故“超时后 selected 不收口”这一状态机缺口不是本轮直接写入的代码，但仍需继续核查本地 order/session/settlement 改造是否延长 submit 阶段、把上游潜在缺陷触发出来。
- 当前分支相对 `origin/main` ahead 8 个 commit，其中 5 个为文档/前端活动流，1 个体育机会分类修复，1 个钱包共识标签澄清；timeout wrapper 不在这些 ahead commits 中。工作树仍有大量未提交二开，必须逐文件继续归因，不能仅凭 blame 断言所有现象都与二开无关。
- Phase 7 账户事实：数据库仅有 2 个模拟账户。主账户 `4473f507-5e6e-4269-b04a-de4a967284a0` 创建于 2026-08-08，承载 16 笔主流程交易；第二账户 `settlement-validation-r2-20260811` 创建于 2026-08-11，是结算链受控一胜一负证明账户，仅承载 2 笔隔离测试交易，不是自动交易系统重复创建的主账户。
- 当前钱包采集 schema 已包含 `wallet_activity_rollups.token_id/outcome/outcome_index`，说明此前固定基线中 BUY/SELL 丢失 YES/NO 身份的问题至少已进入本地 schema；仍需核对运行数据是否实际填充及数据库策略源码是否消费这些字段。
- Shadow 账户并非按页面当前选择自动决定：`trader_orchestrator_worker._resolve_shadow_account_id` 从全局 control settings 或 trader metadata 读取 `shadow_account_id`。必须核对真实 control/trader 配置，不能只看顶部账户下拉框判断钱包跟随会扣哪个账户。
- 当前全局 orchestrator 配置正常指向主账户：`mode=shadow`、enabled、未暂停、kill switch=false，`shadow_account_id=4473f507-...`。Wallet Consensus trader 也 enabled/unpaused/block_new_orders=false，最近持续运行；因此“没扣余额”不是因为选择了第二账户或全局暂停。
- 钱包上游确实活跃：19,891 个 discovered wallet、49 个 top-pool、1 个手动 tracked wallet；最近 1 小时有 1,996 条 rollup，覆盖 57 钱包/691 市场，且 1,996/1,996 均有 token/outcome/outcome_index。Wallet Consensus trader 最近 1 小时产生 106 个 decision、历史 5,345 个 decision，但订单总数始终为 0。故断点已收敛到“信号/策略决策之后、订单创建之前”，不是钱包扫描为空。
- trader snapshot 显示 `traders_confluence` 最近存在 13 个进入低延迟执行统计的样本，但当前周期 `orders_count=0`；需继续按 decision/check/gate reason 分解，判断是策略拒绝还是下单前风险/市场门禁拒绝。
- 共识钱包历史 5,345 个判断中，绝大多数明确被策略/执行门禁拒绝：主要为严格 WebSocket 报价缺失、channel threshold、edge 不足以及盘口 spread 高于 75bps；这部分“没有订单、余额不变”符合当前安全配置。另有 4 个 `selected` 判断通过风险快照但没有 `trader_orders`，正在继续核对 execution session/preflight 最终结果，尚不将其定性为正常或代码缺陷。
- 4 个 `selected` 无订单样本现已收敛为确定的 Shadow 超时一致性缺口：它们均先由 `_run_trader_once` 提交 `selected` 决策，随后卡在 `submit_order/ExecutionSessionEngine.execute_signal`；同一轮 10 秒 runtime-trigger timeout 取消任务，数据库没有 execution session、order、execution_latency/decision 终态事件，也没有把原 decision 回写为 timeout/failed。当前代码仍对 Shadow 使用默认 10 秒外层超时，并在提交 selected 后进入可取消的 submit；因此 UI 留下“已选中”但实际未下单的孤儿决策。余额不变是因为没有订单，不是扣错账户。
- 四模块初始 dispatch 盘点：天气有独立 `weather_distribution` trader；新闻有独立 `news_edge` trader；加密只有 `btc_eth_convergence` trader；体育虽有已启用的 `sports_overreaction_fader` 策略并曾产生 scanner signal，但数据库没有 sports trader，因此当前体育链最多到机会/信号，不能自主下单。这首先是配置链缺失，不是下单代码故障。
- 加密当前仍更新的是 `crypto_digital_sigma_edge`（最近 1 小时 67 条），唯一加密 trader 只订阅 `btc_eth_convergence`；后者最后生成于 18:38 UTC、19:00 后全 expired。trader 虽 heartbeat 到 21:20，却没有匹配 signal。需继续区分生产停止是市场窗口、配置还是 worker 故障。
- 新闻 `news_edge` 只有 1 条 signal、60 次历史 skipped decision、0 order；trader 最后运行 11:38 UTC。另一路 `news_momentum_breakout` 属于 scanner，最后 signal 18:39、20:43 expired。需对照 news worker 的 article/intent/snapshot 定位为何 UI 新闻机会未进入 `news_edge`。
- 天气存在 135 条 signal（36 expired、93 pending、6 skipped）且产生 1,955 次 decision，但 0 order；最近 trader 决策 20:29 UTC 后停止。天气不是“完全没有数据”，下一步查当前批次与主要门禁。
- 天气最新 workflow 实际扫描 200 个市场、生成 46 个新 intent，并由策略产出 97 个可见 opportunity；`signals_emitted_last_run=0` 是因为相同稳定键已有 93 个 pending signal 被刷新/复用，并非桥接停摆。trader 随后确实处理它们，但绝大多数被两类执行门禁挡住：`strict WS pricing source=unknown`，或真实 order book spread 高于 150bps；另有少量 Shadow maker 排队未成交。天气全链到决策层正常，订单层当前没有可成交样本。
- 新闻 workflow 每 120 秒运行且最新有 12 个 pending intent，但样本 edge 仅 0.05%–3%、多数 confidence 0.24–0.63；`news_edge` trader 配置要求 edge>=8%、orchestrator edge>=10%、confidence>=0.55，并要求至少 2 篇/2 来源，因此当前 `signals_emitted_last_run=0` 符合门槛，不是 AI key 或 worker 停止。
- 体育 scanner 当前每分钟真实扫描：13 个候选中识别 11 个体育市场，5 个通过静态过滤，但 0 个出现策略所需的赔率移动；拒绝统计为 outside_move_band=8、low_liquidity=6、outside_favorite_band=2、not_sports=2。`sports_overreaction_fader` v2 本身为 loaded/enabled，无加载错误。当前体育 0 opportunity 属正常策略结果；但即使未来产生 signal，因没有 sports trader，仍不会进入 Shadow 下单，这是独立的配置缺口。
- Phase 7 最终运行复查时，唯一手动 tracked wallet 为 `0x46992d0e547e3822a9c28723f51bfa804ae4e03c`，监控表已有 2,077 条事件，最近一次检测延迟约 1.09 秒，说明“加入追踪”确实已进入采集层。其分析数据为 `CAUTION`、`is_profitable_pattern=false`，当前最新 6 个共识信号的 `source_breakdown.tracked_wallets=0`，均由 pool wallet 贡献；故这个手动钱包当前没有贡献共识下单信号。
- 当前唯一钱包机器人是 `traders_confluence`，系统没有任何订阅 `traders_copy_trade` 的 trader。直接跟踪钱包只决定监控/共识候选范围，不等于逐笔复制；要做单钱包逐笔跟单必须单独建立 copy-trade trader，并独立配置风险和 Shadow 验证，不能把两类语义混在一起。
- 共识钱包最新轮次实际产生 6 个 executable confluence signal，但全部在决策/执行门禁结束：1 个 `channel_threshold`，其余盘口 spread 为 124.2–1818.2 bps，超过当前 75 bps 上限；因此没有订单、主账户余额不变是可解释的安全结果。历史 4 个 selected 孤儿仍是独立代码缺陷。
- 两个模拟账户当前用途已完全核实：全局 `selected_account_id` 和 `shadow_account_id` 都指向主账户 `4473f507-...`；`settlement-validation-r2-20260811` 是受控结算一胜一负/幂等性证明账户，不参与自动交易。主账户当前 `current_capital=945.550990`、`total_pnl=3.054009`；证明账户为 `1047/+47`。
- 天气最终复查：worker 每 4 小时运行，最新扫描 200 市场、460 pending intent、97 visible opportunity、93 pending TradeSignal；最近 4 小时 145 个决定因 strict WS source unknown 阻断，另有大量盘口 spread 超过 150 bps及 5 次 shadow queue 未成交。部分天气 token 能取得 `ws_strict`，说明 WS 服务未整体宕机，但订阅/身份覆盖不足；不能用放宽 strict gate 代替覆盖修复。
- 体育最终复查：scanner healthy，最近完整诊断为 11/11 体育市场、22 outcomes、0 qualifying move/0 signal；拒绝为 invalid_price=12、outside_move_band=5、outside_favorite_band=4、wide_spread=1。页面当前体育分类虽有 1 条 `stat_arb` opportunity，但不是 `sports_overreaction_fader` 信号；且没有 sports trader，故体育自主下单链配置不完整。
- 加密最终复查：worker 每 2 秒运行，15 个市场、8 个 crypto strategy handler、9,000+ dispatch、无 last_error；当前扫描 3–8 市场时 oracle move 最大约 0.16%–0.18%，低于 0.3% 触发阈值，所以本轮 0 signal。`crypto_digital_sigma_edge` 仍曾在 21:21 UTC 产出 signal，但唯一 crypto trader 只订阅 `btc_eth_convergence`；自动交易链存在策略生产者/消费者不对齐的配置缺口。历史 convergence decision 还曾因 Chainlink 年龄和 edge gate 全部 skipped，未有自动订单；现有 7 个该 trader 订单均为 UI manual_buy，不能冒充策略链已跑通。
- 新闻最终复查：worker 120 秒周期、running/enabled、无 last_error、非 degraded，最新完整轮次读 200 篇文章/12 clusters/27 findings，但 0 actionable；当前 8 pending intent 的 edge 仅 0.15%–1.5%（样本最高不足 trader 的 8%/10% 双门槛），因此没有新 `news_edge` signal/order。DeepSeek/LLM 调用链在运行，零交易是质量门槛结果而非密钥失效。
- 2026-08-10 20:45 CST，纽约市场 `3412941` 与西雅图市场 `3412928` 官方均为 `closed=true`、`umaResolutionStatus=resolved`，结果为 NO。
- 官方实际关闭时间分别为 13:13:01 和 16:18:13 CST；天气市场并非每天北京时间 08:00 固定结算，规则要求结算源发布次日第一条数据。
- 本地两笔 `TraderOrder`、`SimulationTrade`、`SimulationPosition` 仍为 open，`actual_profit/actual_pnl` 为 null。
- 同一 `reconcile_shadow_positions` 在 `hot_path_no_rest()` 内只读运行：`matched=2, held=2, would_close=0`，并记录 `market_info_lookup` 被拦截 8 次。
- 同一函数在允许 REST 且读取完整订单 payload 时只读运行：`would_close=2`、两笔 `resolved_win`，候选净收益合计 `+$1.274244382887093`。
- 终局 watchdog 首次查询投影为 `SimpleNamespace`，不含 `payload_json`；因此丢失 condition ID 和 token IDs。数字 Gamma market ID 随后被按 token ID 查询，无法获得终局状态。
- 完整订单 payload 已包含合法 condition ID、YES/NO token ID 和 simulation ledger 引用，说明数据采集存在，故障位于终局事实获取/调度边界。
- 当前 Gamma REST 可访问，实测两次约 712–858ms。17:14–17:17 CST 有短暂网络/WS/RPC 超时，但网络恢复后仓位仍未结算，不能解释持续故障。
- `git blame` 显示 watchdog 投影、市场候选解析和热路径 REST 门禁来自上游历史提交；相关缺陷不是本轮诊断产生，也不位于今天未提交的这些行。
- 项目已有 `MarketSettlement` / `backtest.settlement_store`，但模型注释明确其为离线回测导入事实，不能直接充当在线实盘结算账本；可复用字段语义和 outcome 映射，不应把在线 writer 塞入离线模块。
- 项目已有 `scanner` 的 `market_id <-> condition_id` 双向内存映射、`market_cache` 的 condition ID 持久缓存、`polymarket_trade_verifier` 的 condition ID 提取/官方核验，以及 CTF 链上 payout/redeem 能力。完整设计应组合这些成熟能力，而非重复实现身份和链上事实解析。
- `polymarket.py` 已具备 condition ID 与 token ID 的严格规范化和市场匹配辅助，但缺少统一处理 Gamma 数字 market ID 的公共入口；多个模块自行调用 `/markets/{market_id}`，存在身份语义漂移。
- 当前仓库相对 `origin/main` ahead 7，且存在大量未提交代码；相关结算文件中 `database.py`、`simulation.py`、`position_lifecycle.py`、`trader_orchestrator_worker.py` 已有改动，实施必须先逐段审查 overlap。
- 既有 2026-08-09 runtime 修复规格只覆盖新闻/体育/加密和 Shadow ledger durability/display，明确采用“最小修改”；不能作为本次实盘级结算体系设计，只能复用其中事务提交风险证据。
- `polymarket_trade_verifier._fetch_market_info` 已明确注释 `TraderOrder.market_id` 是 Gamma 数字内部 ID，而不是 condition ID，并正确优先从完整 payload 提取 condition ID；这与本次 watchdog 丢弃 payload 的故障完全一致，可作为在线身份解析参考实现。
- trade verifier 已有 HTTP 预取并发上限 4、单市场 8 秒超时、每轮 100 行预算、释放 DB 连接后再联网等成熟保护；新的终局事实抓取层应复用这些容量控制思想。
- 离线 `MarketSettlement` 以 condition ID 为主键、winning token ID 判胜，可抵御 crypto Up/Down 被错误标成 Yes/No 的问题；在线实盘事实也应以 winning token ID 为第一判定依据，outcome label 仅作展示/辅助。
- 冷平面联网周期的最坏网络预算为 `ceil(100/4) × 8s = 200s`；若在 reconciliation 主协程中直接 await，会让 worker heartbeat 长时间不更新。因此采用单实例后台周期、240s 独立 timeout、60s 完成后冷却，并在主 worker snapshot 中收割上轮 stats。
- 已持久化 final fact 必须优先于再次联网：Observe 写入后，即使进程重启，Shadow 周期可直接从 current fact 结算；这既减少 Gamma 依赖，也保证断网恢复不重新猜测终局。
- 非法 condition、非 Polymarket venue、unsafe identity 和 provider-market 冲突不能占用 100 个有效市场批次；候选 SQL 先过滤并单独计入 blocked，避免脏历史行长期饿死真实待结算市场。
- `PolymarketClient.get_market_by_condition_id` 已主动探测 active 与 `closed=true`，并把规范化结果写入内存/SQL cache；`get_market_by_token_id` 仅按 token 查询。当前 `_looks_like_token_id` 接受任意数字串，因此 Gamma 短数字 ID 会被误分类为 token ID，暴露出“裸字符串 + 猜类型”这一根本设计缺陷。
- `_extract_market_info` 已统一输出 `id`、condition ID、token IDs、outcomes、outcome prices、closed/resolved/UMA 等字段，可作为在线终局事实 adapter 的原始规范化入口，但当前没有记录 provider 观测版本、证据时间或最终性等级。
- `simulation.close_orchestrator_shadow_fill` 已存在 already-closed 分支，说明模拟层已有部分幂等保护；仍需审计其行锁、账户/交易/仓位原子性、commit 所有权和重复并发关闭行为。
- `simulation.close_orchestrator_shadow_fill` 当前用普通 `session.get` 读取账户、交易和仓位，没有 `SELECT ... FOR UPDATE`、条件状态更新或独立结算幂等键。两个并发 worker 都可能读到 `OPEN`；同一账户上的不同交易会用各自读到的旧聚合余额做绝对值 UPDATE，存在丢失其中一笔余额/PnL 增量的风险；同一交易也可能重复触发内存状态/反向信号等副作用。现有 `already_closed` 只防顺序重复，不能保证并发 exactly-once 经济效果。
- 同一 `simulation.py` 仍保留旧的 `resolve_trade` 写账路径，采用另一套派彩/费用逻辑并在函数内部提交事务。若两条路径均可到达，将形成“双重事实源 + 不同事务所有权”的实盘级风险，完整设计必须收敛写账入口而不是继续叠补丁。
- 当前 `simulation.py` 工作树差异主要是权益/ROI 计算调整，并非本次 close 并发问题；后续实现必须保留该既有修改并逐 hunk 合并。
- Live 侧 `verify_orders_from_bot_lineage` 和 `verify_orders_against_closed_positions` 由冷/交易 reconciliation 后置任务周期调用；它们只查询 `mode='live'` 且已进入 closed/resolved 状态的订单，不能负责把仍为 open 的 Shadow 仓位推进到终局。
- `verify_orders_against_market_resolutions` 虽实现了官方终局覆盖逻辑，但当前 reconciliation worker 未调用它；系统存在“已写实现但不在正式调度链”的漂移，说明完整修复需要显式注册表/单入口，不能依赖函数存在即认为功能生效。
- `TraderOrderVerification` 是为减少 verifier 与 orchestrator 对同一订单行锁竞争而增设的 1:1 表，但当前仍处于双写阶段：ORM 事件每次写 `TraderOrder` 后 UPSERT side table，并且捕获所有异常后静默忽略。源码注释声称“保证始终同步”，实现却允许无告警丢镜像；该表目前不能作为无条件可靠的账本事实源。
- `TraderOrderVerificationEvent` 是追加式证据表，但没有 provider resolution identity 或唯一幂等约束；相同终局事件重复运行可产生多条事件。它适合作为审计日志的一部分，不足以单独保证 exactly-once 入账。
- 项目现有 worker host 已有独立的 `reconciliation` 冷平面，允许认证只读并明确禁止下单/撤单；完整在线终局抓取和结算协调可挂载该平面，无需把 REST 或重任务塞回交易热路径，也不必新增一套旁路进程管理。
- 当前模拟账户只有 `current_capital/total_pnl/winning_trades/losing_trades` 聚合字段，没有逐笔不可变现金分录。仅靠聚合行无法从账本重建余额，也无法可靠检测/修复并发丢更新；实盘级 Shadow 证明需要增加 append-only cash journal，并让账户聚合成为可校验投影。
- `redeemer_worker` 与 `ctf_execution` 已具备只读 dry-run、condition ID 严格校验、链上 payout 读取和显式 redemption；Live 终局链应复用该能力，把“市场已终局/可领取”和“链上已兑换/资金到账”分为两个状态，不应在 Gamma resolved 时就假装真实资金已到账。
- 当前运行库 Alembic 版本为 `202608100001`，对应工作树中尚未跟踪的 wallet outcome identity migration；新增结算 schema 必须从该实际 head 接续并保护这份既有工作，不能按远端 main 的旧 head 生成迁移。
- 当前未配置 Polymarket 实盘凭据属于用户已知状态，但 reconciliation/trading 每约 5 秒重复初始化并在 30 分钟产生约 951 条 missing-credentials 错误/警告；这不会阻止 Shadow 终局 REST，但会淹没有效实盘告警。实盘完善方案应增加明确的 `not_configured` 健康状态和日志节流，而不是把“暂未配置”当持续 ERROR。
- 最近 30 分钟 Shadow 交易周期多次耗时 10–20 秒，其中单次批量 commit 约 6.5–15.4 秒（65–133 dirty rows）。结算新链必须使用短事务、小批次和冷平面，且不得继续扩大现有交易周期的 commit 面积；该延迟问题需要列入上线门槛，但不应与结算逻辑揉成同一大事务重构。
- 既有 2026-08-09 规格和计划明确限定为“最小修改”，且实施计划没有逐步 RED/GREEN 命令、预期失败原因、schema 幂等约束或故障注入；本次不能在该计划上追加几条步骤，必须新建独立的在线结算规格与完整 TDD 计划。
- `SimulationService` 有两条开仓现金路径：通用 `execute_opportunity` 与 orchestrator 专用 `record_orchestrator_shadow_fill`，两者都直接修改 `SimulationAccount.current_capital`；结算又有 `close_orchestrator_shadow_fill`、旧 `resolve_trade` 和 `position_monitor` 等现金写路径。为了避免一次性重写全部 legacy 模拟器，proof-grade journal 将先明确覆盖 HOMERUN orchestrator Shadow v2 账户；legacy 路径保持兼容但不得出现在“账本已证明”指标中。
- `SimulationAccount` 当前没有 ledger 版本/校验状态。新增账户级 `ledger_version` 和 `ledger_integrity_status` 可以让 UI/API 明确区分 legacy 聚合账户与 v2 可重建账户，并允许分阶段迁移而不是把历史聚合值伪装成完整账本。
- 运行模式可沿用 `config.Settings` 的环境配置模式，worker plane 已通过 `HOMERUN_WORKER_PLANE` 区分；无需把结算开关写进策略参数。计划使用单一 `HOMERUN_SETTLEMENT_RUNTIME_MODE=off|observe|shadow|live`，默认 observe。
- 历史补偿与账本完整性 API 最合适放在现有 `/api/simulation/accounts/{account_id}` 路由族中；不新增独立前端或新的顶层 router，降低接口扩散。
- `config.Settings` 是纯 Pydantic environment settings，适合新增单一运行模式字段和 validator；无需数据库迁移设置表。
- `docker-compose.yml` 使用显式的 `x-backend-env` 环境白名单，而不是将 `.env` 全量透传；因此新增 `HOMERUN_SETTLEMENT_RUNTIME_MODE` 时必须同步加入共享环境锚点与 `.env.example`，否则本地 Python 可见但容器 worker 不可见。
- reconciliation worker 的主循环已经把重型核验放在 post-cycle，并有每项独立 timeout。新的 settlement cycle 应仅在 `_IS_COLD_RECONCILE_PLANE` 运行，并拥有单独超时/统计；不能塞进每个 trader 的 `_run_reconciliation_cycle`，否则会重复扫描且扩大事务。
- simulation router 已有 Pydantic request DTO 与账户子资源模式；补偿 apply 应使用带 `preview_digest`、`confirm` 和 order IDs 的显式 DTO，并返回 409 表示预览失效，不能沿用无 DTO 的手工 `resolve_trade` 入口。
- Task 1 实施时确认项目统一使用 `UTCDateTime` 在数据库保存 UTC-naive、Python 侧恢复 UTC-aware；新在线结算时间列复用该类型，避免引入与现有 asyncpg/schema 不同的时间语义。
- 新增 migration 已从实际 head `202608100001` 接续为唯一 head `202608100002`；旧 simulation account 明确保留 ledger v1/`legacy`，没有根据历史聚合余额伪造完整 journal。
- fast-tier 的统一 order builder 位于 venue submit 之后，不能在那里把 identity 缺失的订单改成 failed，否则会造成“venue 已下单、DB 伪装失败”。identity 执行门禁因此放在所有正常下单共用的 `submit_execution_leg`，且位于余额/盘口/venue I/O 前；builder 只负责 post-wire 如实持久化。
- 当前 21 个真实订单的只读 typed identity 解析结果为 16 个 `legacy_inferred`、5 个 `ambiguous/missing_selected_token`。这验证大多数旧 payload 足以恢复 condition/token/outcome，同时证明缺 token 的旧行必须留在补偿人工审查，不可猜测。
- order-manager 回归暴露信号级 `selected_token_id` 可能与 per-leg outcome 不同，以及 live context 可能属于另一个 market；执行身份必须以已解析的 per-leg token 为准，并忽略 market 不匹配的 live context，不能把合法 bundle leg 误报为冲突。
- 2026-08-10 对 Gamma 最新 100 个 closed market 的只读抽样显示：100/100 的 `umaResolutionStatus=resolved`，但 `resolved`、`winner`、`winningOutcome` 均为 null；`outcomePrices` 为精确 `["1","0"]` 或 `["0","1"]`。因此 v2 不能强制依赖旧 `resolved/winner` 字段，必须把 market-level UMA resolved 与二元精确 one-hot token 对齐合并判定。
- 当前 Gamma 已终局市场仍可返回 `active=true`，而 `acceptingOrders=false`；终局门禁不能错误要求 `active=false`，但必须明确要求 `closed=true` 且停止接单。
- Gamma 的 `umaResolutionStatuses` 聚合/历史字段可在 market-level `umaResolutionStatus=resolved` 时仍为 `["proposed"]`；它不能取代当前 market-level 状态或含糊子串匹配。
- provider 返回的 condition/provider market ID 必须与请求 lookup 一致；否则即使 winner 字段完整也必须拒绝，防止缓存污染、provider 异常或并发错配造成串单派彩。
- 对 final current fact 采用“winner 冻结、证据继续追加”：同 winner 的新 provider 证据进 observation 但不改 fact version；不同 winner/token identity 使 current 进入 `conflicted`，旧 winner 和旧 evidence hash 不被覆盖。
- 只靠 frozen dataclass 不足以防止证据脱节，因为 `dataclasses.replace` 仍能保留旧 hash 而替换 typed 字段；入库前必须重算 canonical hash，并校验 provider/venue/market/token/outcome/price/state/winner/provider time 与 evidence JSON 一致。
- 当前 current fact 的数据库写链是 observation upsert 后初始化 current、再行锁推进，两者处于同一 caller-owned transaction；触发器在 current insert 强制报错后两张表均为 0 行，证明没有 observation 半写。
- v2 账本接入后，旧通用 `execute_opportunity` 与旧 `resolve_trade` 仍会直接修改 `SimulationAccount.current_capital`；这不是配置问题，而是明确的第二写账入口。现已在账户行锁后拒绝 v2，legacy v1 兼容行为保持原样。
- v2 新账户创建时即有不可变 opening marker；因此沿用旧物理删除接口会直到 commit 才触发 PostgreSQL FK violation。正确边界是在业务层先拒绝 append-only 账户删除，并把 API 表达为 409 conflict。
- 现金 journal 使用 `NUMERIC(20,6)`/Decimal、账户级行锁和连续 sequence；opening marker 为 0，避免在 `initial_capital + journal delta` 公式下重复计算初始余额。
- 历史聚合账户无法从现有数据恢复逐笔现金史；显式 checkpoint 只记录当前余额与初始余额之差，覆盖标记必须保持 `checkpointed`，不能升级成 `complete`。
- Live 的 provider 终局只证明持有份额已形成应收权利，不证明钱包已收到 USDC；因此 Gamma final 只进入 `market_final/projected`，不得写 `actual_profit`。只有完整 bot-owned CLOB SELL、身份/钱包/时间一致的官方 closed-position 证据，或 redeem confirmed 后可归属的余额证据才能进入 `cash_verified`。
- 官方 closed-position 行必须同时对齐 condition ID、outcome index、查询钱包、平仓时间与 `realizedPnl`；只看 `curPrice`、钱包聚合盈亏或共享钱包 FIFO SELL 会把手工交易归到机器人订单，已从 managed Live 路径排除。
- CTF 提交状态回调采用“广播前失败关闭、广播后继续等待 receipt 并审计回调错误”的边界：避免状态库故障时盲目发交易，也避免交易已广播后因本地回调异常丢失链上确认结果。
- `TraderOrder` 到 verification side-table 的镜像错误现已向上抛出；真实 PostgreSQL 触发器故障注入证明 order/settlement/event 会在同一事务整体回滚，不再出现主表已核验、镜像静默缺失的假成功。
- 历史修复 preview 现在只读取本地已持久化的官方 fact，不调用 Gamma/CLOB/Data API；摘要覆盖账户投影、订单 `updated_at`、trade/position 状态、resolution fact version/evidence hash 与候选经济值，任一变化都会使 apply 返回冲突且零写入。
- 历史 apply 具有独立的默认关闭开关 `HOMERUN_SETTLEMENT_REPAIR_APPLY_ENABLED=false`，同时要求 `confirm=true`、精确唯一 order IDs 和 64 位摘要；跨账户、Live、含糊身份、非 final/conflicted fact 均在写账前拒绝。
- 旧聚合账户的修复不会伪造完整历史：同一事务先写 `legacy_checkpoint` adjustment，再委托正常 settlement coordinator 写 settlement credit，完整性保持 `checkpointed`；相同摘要重放返回同一 settlement，不重复改变余额。
- Task 11 的互斥测试文件统计为 183 个唯一核心测试，全部通过；一次 worker-cycle 分组被重复执行，因此日志中的总执行次数为 195，不应把重复项宣传成额外覆盖。
- 新结算核心服务和新增迁移/测试的 Ruff 检查已通过。旧 `routes_simulation.py` 仍有 7 个本任务前既有 lint 问题；为避免交易接口无关重构，本任务只对新增路由做局部 noqa/导入整理。
- 当前运行数据库仍为 migration `202608100001`，代码唯一 head 为 `202608100002`；容器仍运行 `local-20260810-trader-token-bridge-v6`。所以测试通过仅证明候选源码，不代表运行系统已经应用结算修复。
- `git diff --check` 当前通过，仅输出 Windows 工作树 LF→CRLF 提示；这不是 whitespace error，也不应在本任务中批量改换行符。
- 新镜像首次 observe 两个周期均为 `applied=0`、网络/持久化/apply 错误为 0，账户 `current_capital=919.403948315238`、`total_pnl=0` 未变，四张新表仍为 0；证明 observe 未产生经济写入。
- 两个周期同时返回 `blocked=22, candidates=0`。数据库核实 23 个订单的 typed identity 列全部为空；历史 repair preview 对关联的 17 单全部返回 `ambiguous/identity_not_settlement_safe`。规格要求 preview 生成历史 identity，但实现只读取 persisted typed 列，这是确定的规格遗漏。
- 正确修复不是自动回填历史订单：observe 只用 `identity_from_order` 的内存推断抓 fact，普通 shadow 仍不处理未持久化 identity；历史 repair apply 才在摘要/开关/confirm/精确 IDs 门禁下原子固化 identity 并结算。
- Task 10A 运行结果证明上述边界可行：R2 observe 从 23 个 typed 列全空的历史订单中发现 14 个唯一市场，首轮保存 14 个官方事实/证据，其中 5 个严格 final；订单 typed 列、旧账户余额/PnL、settlement 和 journal 均未变化。
- 可推断历史 identity 仍不是普通 Shadow 的自动结算许可。R2 保留 `blocked=22/health=attention`，同时允许 candidates/observations 增长；若把这些行从 blocked 扣除，会让监控把“等待显式修复”错误显示为健康。
- 当前历史 repair preview 已从全 ambiguous 收敛为 safe=5、ambiguous=1、blocked=11；safe 只表示在当前订单版本和 persisted final fact 下可计算，候选 PnL `+3.054009` 仍不是已实现收益，因为 apply 未获授权且保持关闭。
- 隔离 v2 运行验证得到确定的一胜一负：winner 净派彩 247、PnL +147；loser 派彩 0、PnL -100；最终余额 1047、总 PnL 47。两笔 settlement 与两条 credit 同账户 journal 差额为 0，证明统一 coordinator 的经济投影在真实运行库可重建。
- 对两笔终局单执行 100 轮、共 200 次重放，余额/PnL/胜负计数/settlement/journal 均不增长；这是运行库证据，不只是单元测试结论。
- provider 超时注入使 9 个非 final 请求显式进入 `network_errors=9/health=degraded`，但没有 observation、settlement 或余额写入；冷 worker 重启后的真实周期恢复为 0 错误，证明网络失败没有跨过事务边界。
- Compose 的固定源码安全不仅取决于正在运行的容器：项目 `.env` 若仍指向旧标签，新的 shell 中执行 `docker compose start` 可能拉起陈旧的一次性 migrate 容器。此次数据库 head 未改变，最终通过固定 `.env` 为 R2、重建 migrate 和统一 `up --no-deps` 规则消除该运维漂移。
- 用户已单独授权 5 笔历史安全订单 repair：写入前冻结工作器、重做全量和定向 preview，并确认 safe 集合严格等于批准的 5 个 ID。repair 原子提交后净派彩 `26.147042`、已实现盈亏 `3.054009`，账户账本重建差额为 `0.000000`；blocked/ambiguous 订单未写入。
- 当前 backend、migrate 和全部 Python worker 已统一为 `local-settlement-20260811-r2`，数据库为 `202608100002`；冷结算已持久切到 shadow、historical apply=false、Live 无凭据。两个 Shadow 周期没有网络/持久化/apply 错误，也没有对已修复订单重复入账。
- Phase 8 首轮 Git 归因：`_RUNTIME_TRIGGER_DEFAULT_CYCLE_TIMEOUT_SECONDS=10.0`、外层 task cancel 以及 selected 决策先持久化再进入 `submit_order` 的主体链均来自 `origin/main` 上游历史，当前 ahead 8 个提交没有修改该 timeout 区域。
- 当前工作树对下单链并非零侵入：`order_manager.py` 在常规 outcome order 提交前新增 typed market identity 校验；`trader_orchestrator_worker.py` 的本地差异集中在 Shadow ledger marker 校验和 backfill commit。前者需要继续用真实阶段耗时/四个 timeout 样本判断是否触发上游潜在超时，不能仅凭 diff 直接免责或归罪。
- `session_engine.py` 当前未出现在相对 `origin/main` 的相关 diff 统计中；其已有测试覆盖 live provider cancel placeholder 和 Shadow commit，但尚需核对外层 worker 在 session engine 建立前取消时是否存在终态契约测试。
- 四个最新 `traders_confluence` 孤儿 selected 决策均无 `execution_sessions`、无 `execution_session_events`、无 `trader_orders`，说明取消发生在 Shadow session projection 最终持久化之前；外层 timeout 事件又没有 decision_id/stage，当前审计信息不足以直接断言具体 await 点。
- 同时间窗其他被 spread preflight 拒绝的信号，`submit_round_trip_ms` 仅 9–308ms；四个 selected 决策本身在进入决策时的 `armed_to_now_ms` 为约 0.2s、0.46s、1.16s 和 11.27s。第四个是在上一轮 timeout 后继续产生，表明 runtime-trigger 并发/排队会跨越 10 秒窗口，不能把现象简单归因为全局数据库或全局下单持续缓慢。
- `ExecutionSessionEngine` 的 Shadow 路径在执行波完成后才持久化 session/order；现有取消终态保护只针对 `mode=live` 且已存在 pre-submit placeholder。外层 10 秒取消 Shadow 时，没有等价的 durable intent/terminal state，因此 selected 可能永久无订单。这是状态机契约缺口，而不是应靠调大 timeout 掩盖的症状。
- Shadow 提交的两个主要外部 await 是上游既有的 Redis 日限额读取与 WS feed 的 `get_order_book`；后者在缓存不新鲜时会自动走 HTTP fallback，调用点自身没有局部 timeout。preflight 的 max-spread gate 与正式 submit 又会各调用一次同一 book resolver，因此在 stale/missing cache 场景可能重复承担网络等待。
- `ws_feeds.py`、`daily_spend_tracker.py`、`session_engine.py` 相对 `origin/main` 均无本地差异；`_resolve_shadow_book_and_tape`、HTTP fallback 和 preflight venue gate 也均由上游提交引入。当前二开新增的 typed identity resolver 在这些 await 之前执行，但它是同步纯解析，不会直接制造 10 秒阻塞。
- 这并不排除二开间接影响：若本地订阅/身份改动降低了目标 token 的 WS cache 覆盖，会让上游原本很少触发的 HTTP fallback 变成常态。必须继续对四个目标 token 的当时/当前订阅与固定镜像配置做证据核对。
- FeedManager 在启动时将 stale-book fallback 设为 Polymarket CLOB `/book`；`PolymarketClient` 共享 HTTP client timeout 是 30 秒，而 runtime-trigger 外层默认只给 10 秒。这里存在确定的超时预算倒置（inner 30s > outer 10s），且 fallback 调用本身没有更短局部 timeout。
- 当前 backend/worker 均运行固定镜像 `local-settlement-20260811-r2`；host 与 trading container 的 worker、order manager、session engine、WS feed、market identity SHA-256 全部一致。当前运行态没有“改了源码但没重建”的版本漂移，但四个历史 timeout 发生在更早镜像，仍需做镜像时间线对比。
- 历史镜像哈希闭环：四个 timeout 发生时可用的 `local-20260810-trader-token-bridge-v6` 中，`order_manager.py`、`session_engine.py`、`ws_feeds.py` 与原始 `source-389d246` 完全相同，且当时镜像根本没有 `market_identity.py`。因此当前 typed identity/结算 R2 改造不可能是这四次 timeout 的直接成因。
- 当时镜像的 `trader_orchestrator_worker.py` 与原始镜像不同，但相对 `origin/main=389d246` 的实际 diff 仅为 Shadow ledger backfill 对 stale marker 的校验及 flush→commit；timeout wrapper、selected→submit 路径没有改动。该 backfill 在 selected 前执行，四个样本的决策在多数轮次 0.2–1.16 秒内已生成，暂未发现它造成这四次提交阻塞的证据。
- 归因结论需保持双层：孤儿状态机和 30s/10s 预算倒置是原版上游缺陷；二开确实改变了钱包信号身份/筛选和运行配置，可能改变触发频率与可见症状，但不能据此说这些 timeout 代码是二开写出来的。
- 配置审计确认 11 个当前 Shadow trader 全由 `clean-baseline/codex` 配置修订创建；当前没有任何 `sports_overreaction_fader` trader，crypto 只有 `btc_eth_convergence` trader。虽然全局 `enabled_strategies` 列表包含 sports 与 `crypto_digital_sigma_edge`，但没有消费者 trader，属于之前基线配置遗漏，不是原项目接口字段 bug。
- 第二模拟账户 `settlement-validation-r2-20260811` 创建于 2026-08-10 20:21 UTC，是本次结算一胜一负/200 次重放验证的隔离证明账户；全局 `selected_account_id` 与 `shadow_account_id` 始终都指向主账户 `4473f507-...`。它不参与策略交易，但账户列表展示它是测试残留带来的产品困惑。
- 钱包 trader `Wallet Consensus Baseline 2026-08-10` 也是 `codex` 创建的配置：策略为 `traders_confluence`，scope=`tracked,pool`，`min_wallet_count=2`；系统没有 `traders_copy_trade` trader。因而 UI 的“添加跟踪”只把钱包纳入观察范围，并不等价于单钱包立刻跟单；这与用户对“跟随买入”的直觉不一致，属于配置/交互语义边界，不应冒充策略已端到端跑通。
- 天气核心代码（weather worker、weather strategy、WS feed、live market context）相对 `origin/main` 无本地 diff；weather opportunity 经通用 `bridge_opportunities_to_signals` 时会调用 `intent_runtime.prewarm_source_tokens`，因此项目并非完全遗漏订阅动作。当前 `strict WS source unknown` 需要继续查 prewarm 等待预算、订阅容量/淘汰和 token identity，不应直接加一个重复 subscribe 补丁。
- `intent_runtime` 的 prewarm 会先 subscribe，再对缺少 mid price 的 token 用最多 8 个 worker 调 `feed_manager.get_order_book`；该调用仍可能走 30 秒 HTTP fallback，且 seed gather 没有每 token 局部 timeout。完成 seed 后只等待 0.5–1.0 秒的新鲜 WS quote。天气一次约 97 个机会时，这条链可能形成长 seed 周期或个别无效 token，无证据前不能简单延长 wait 或放宽 strict gate。
- trading plane 不受 WS subscription 数量预算淘汰，但通用 cache eviction 每 60 秒会移除 600 秒无更新的 token；weather 4 小时周期会在下一轮重新 subscribe。由此排除“检测平面的数量预算直接挤掉天气 token”，仍需查无更新原因和 identity 是否对应真实可交易 CLOB token。
- 现有自动化测试进一步确认状态机覆盖不对称：worker 只测试了外层 soft-timeout 能取消 cycle 并记录 `cycle_timeout`；session engine 只测试了 Live 在已持久化 placeholder 后取消会收口为 failed，以及 Shadow 正常完成时必须 commit。没有测试覆盖“Shadow 在 submit 外部 await 中被外层取消后，仍留下 durable execution session/terminal audit”。这与四个真实 selected-without-session 样本完全一致。
- 最新天气决策不是全量“无报价”：同一轮既有正常 order book 但因 spread=152.7–6666.7bps 被拒，也有一次进入 Shadow 撮合后因 `queue_not_reached_by_trade_flow` 未成交；另有一部分 token 为 `strict WS source=unknown`。因此天气采集、token 提取和部分行情链已实际跑通，未知报价是 token/盘口覆盖的子集问题，不能用关闭 strict gate 或全局放宽 spread 处理。
- 天气调度没有漏跑：当前北京时间 06:05，最近周期 04:29 完成，固定 interval=14400 秒，下一轮约 08:29。该轮扫描 200、形成 97 opportunities、46 intents；历史 weather order 仍为 0。
- 对上一轮 4 个真实 token 调官方 CLOB `/book`：两个当时 unknown 中，一个当前只有 0 bid/76 ask，按项目 `_safe_binary_mid` 必然返回 None，属于真实单边盘口；另一个当前有 7 bid/68 ask、约 0.007/0.018，说明 unknown 还可能来自订阅后 0.5–1 秒内未收到 fresh WS snapshot，而不是 token identity 错误。两个 spread 样本当前也都有双边簿。需通过进程内诊断或可控测试区分“单边簿”与“prewarm 等待预算不足”。
- `/debug/feeds` 只读取 API 进程自己的 FeedManager；trading worker 的 cache 是进程内单例，不能拿 API 的 cache 数量替代交易进程证据。修复前若要补观测，应复用 worker snapshot/event，而不是新增交易旁路。
- 当前固定 R2 trading worker 自 2026-08-10 20:53:58 UTC 启动后，90 分钟内没有新 `selected`、没有 selected-without-session、也没有 `cycle_timeout`；但这段时间 `traders_confluence` 全部是 blocked/skipped，主要为 channel_threshold、edge、strict WS unknown 和 max_spread=75bps，因此只能证明“缺陷尚未再次触发”，不能证明取消状态机已修复。
- 同一窗口数据库只有 1 个新订单，来源 `manual/manual_buy`、状态 submitted、名义 25 美元；自动 trader 订单为 0。钱包余额不变与当前门禁事实一致，不是 UI 漏显示订单。
- 钱包的 `max_spread_bps=75` 不是临时写死的新数字，而是原项目 `StrategySDK.TRADER_RISK_DEFAULTS` 的统一默认值；当前 Codex 创建的 wallet trader 沿用了它。问题在于把通用默认直接用于真实钱包市场后，当前多数盘口无法通过，并非应把门禁代码删掉。调整前必须用该策略的历史 spread 分布/净优势回测确定专用阈值。
- 原项目策略目录同时原生提供 `traders_confluence` 与 `traders_copy_trade`。当前只创建了 confluence trader，故“跟踪钱包”不触发单钱包立即买入是已配置策略的真实语义，不是 copy-trade 代码缺失；若要 direct copy，应新增独立 Shadow trader，不能混入共识策略。
- `ExecutionSessionEngine.execute_signal` 已有三段可复用职责：`_commit_pre_submit_projection`、`_persist_execution_projection_safely`、`_finalize_cancelled_live_submit`。当前 pre-submit placeholder 和取消 finalizer 明确只服务 Live，Shadow 只在 wave 返回后进入最终 projection；修复应扩展同一状态机职责，不在 worker 另造第二套订单状态。
- `execute_signal` 在任何外部 preflight 前已经构建出 session/leg ORM 行；`_commit_pre_submit_projection` 采用 session+legs+TraderOrder 的第一阶段 flush、ExecutionSessionOrder 第二阶段 flush、events 第三阶段 flush、最后 commit，以满足真实 FK 顺序。这是可直接复用的 durable-intent 事务模式；Shadow intent 不需要伪造 TraderOrder，只需先提交 session+legs+event。
- 进一步读取主流程发现 preflight 本身也包含外部 order-book await，并发生在 Live placeholder commit 之前；因此只保护 `submit_execution_wave` 仍会遗漏“preflight 被取消”。任何取消收口必须覆盖从 durable intent 之后的整个 preflight+submit 区域，而不是只在 wave wrapper 增加 except。
- 原代码刻意把 Live placeholder commit 放在 preflight 之后，注释记录无效信号提前 commit 曾耗 5–13 秒。若 Shadow 在每次 preflight 前新增一次独立 commit，会扩大数据库负载且可能反过来撞上 10 秒预算。实施计划必须先用测试/计时证明事务边界，不能机械复用 Live placeholder 时点。
- worker 在调用 `submit_order` 前已经把 selected decision/claim/cursor 原子 commit，并 reset 主 session；Shadow 的 `submit_order` 随后打开独立 `AsyncSessionLocal`。因此取消收口可以完全局限在 session engine 的独立事务，不应让 worker 回头修改共享主 session 或账户。
- 外层 `_run_trader_once_with_timeout` 在超时后 cancel task、最多等 cancel-grace，然后只写 trader 级 `cycle_timeout`，payload 只有 process_signals/timeout_seconds。它不知道当前 decision/session/stage；即使 session engine 正确收口，外层审计也无法直接串联。审计增强应从当前 cycle stage/decision 上下文读取只读快照，不能在超时 handler 猜测“最后一条 selected”。
- 项目已有 `ExecutionSessionEngine.cancel_session(session_id, ..., skip_provider_io=True)`，且在没有 ExecutionSessionOrder/TraderOrder 时仍会原子把所有 legs、session、signal 和 session event 收口并 commit；终态幂等，已终态直接返回。可用于 Shadow durable intent 的取消恢复，无需新写一套 SQL 状态机。
- 最小跨层接口可以是 session engine 暴露当前 `{session_id, decision_id, signal_id, stage, elapsed}` 上下文，`submit_order` 捕获任意 `CancelledError` 后用全新 `AsyncSessionLocal` + `asyncio.shield(cancel_session)` 收口。这样覆盖 preflight、wave 和后续任意 await，并避免在被取消/可能失效的原 session 上继续写。
- Shadow `submit_execution_leg` 只读取 Redis/行情、运行纯内存微观结构估算并返回 `LegSubmitResult`；它不写模拟账户、持仓或订单。经济/订单投影都在 session engine 后续统一持久化。因此取消发生在 wave 返回与 projection 之间时，用 durable session 收口为 failed 不会掩盖已经扣款的 Shadow 交易；唯一额外副作用是既有 fire-and-forget 策略 fill callback，不能作为经济成交证据。
- Shadow 的实际慢点明确为 daily-spend Redis await 和 `_resolve_shadow_book_and_tape`；preflight 与 submit leg 会重复解析同一 token 的 book。预算修复首先应给外部 await 传递剩余 deadline；不能把 HTTP fallback 写入 strict WS cache，否则会错误把 REST 价格冒充 `ws_strict`。
- worker 已有 `_remaining_cycle_budget_seconds`，并在 live-context、循环 bail 和 strategy evaluation 中使用；提交链无需新造全局 deadline 设施。可在 submit 前用同一 monotonic cycle start 计算剩余预算并预留终态持久化时间，再把相对预算传给 Shadow session engine；Live 继续保留原有最少 60 秒、不改行为。
- 外层 cancel grace=5 秒、硬清理阈值=90 秒。Shadow 内部 deadline 应先于 10 秒外层触发并返回失败结果，让 worker 走现有 decision `selected→failed/skipped` 正常更新；外层 cancel 只作为兜底。这样能修复根因，而不是只让取消后的 session 可见。

## 2026-08-11 钱包共识手动选择后零订单排障

- 用户报告刚刚自行选择钱包共识信号下单，但主账户余额未变化且 UI 无订单；本阶段只读追踪，不先修改交易逻辑。
- 主账户仍为 `945.550990`、PnL `3.054009`、16 笔交易；隔离证明账户也未变化，说明该操作没有产生任何经济写入。
- 最新 `traders_confluence` 信号与决策持续生成，但最近一轮全部是 `skipped/blocked`：策略层为 `edge` 或 `edge, channel_threshold`；行情层为 `Strict WS pricing required`；进入 session 的样本被 `max_spread_bps=75` 拒绝，实际 spread 约 `137.9–3432.0 bps`。
- 因此“余额不变”的直接原因已确定为没有 TraderOrder；尚未确定的是用户点击是否本应绕过自动策略门禁立即下单。必须继续读取前端 handler 和后端路由契约，不能把自动评估结果直接当成手动点击结果。
- backend 最近 20 分钟日志未出现匹配 trader/wallet/signal/order 的 POST/PUT/PATCH/DELETE 访问行；可能是访问日志未记录、WebSocket 调用、或该控件仅本地/池配置动作，不能据此单独断言请求未发送。
- 前端入口已收敛到 `TraderSignalViews.tsx`、`AddWalletToBotDialog.tsx`、`traderBotActions.ts` 和 `WalletTracker.tsx`。其中 `AddWalletToBotDialog -> addWalletToTraderBot` 的静态职责是创建/更新 Trader 的 `traders_scope`，并非直接写订单；但仍需逐行确认用户本次点击是否走该入口。
- 用户描述的“钱包共识信号下单”对应 `TraderSignalViews -> BuyButton`。该按钮会打开真实下单对话框，选机器人和金额后调用 `POST /traders/{trader_id}/manual-buy`；因此它不是“仅加入跟踪”的控件，按界面契约应返回明确成功或失败结果。
- `buildConfigFromTraderSignal` 当前把 `token_id` 固定为空字符串，仅携带 `market_id + outcome + price` 给后端。是否可由后端可靠补齐 token identity 是当前最关键的契约检查点。
- 数据库已定位用户刚才两次点击：北京时间约 09:19 写入 `$50`、09:22 写入 `$25`，均属于 trader `c7631836...`，状态 `shadow/submitted`；对应 `manual_buy` 事件也写入成功。因此前端请求和后端路由都到达了，但这不代表完成模拟成交。
- 两条新订单以及此前大多数同入口订单的 `payload_json.token_id` 均为空；路由直接把订单标为 `submitted`，没有调用统一 `ExecutionSessionEngine` 或 Shadow 撮合/账户账本，只在提交后用订单聚合生成 `trader_positions`。这能解释“有路由成功提示但余额不变”，也说明订单状态语义不够严谨。
- Git blame 显示：`manual-buy` 主体来自上游提交 `23b146c20`，空 token 的钱包信号构造来自上游提交 `a3239035e`，不是当前 Shadow timeout 修复所引入；仍需用 ancestor 检查确认这些提交属于 `origin/main`，并核对页面为什么没有展示已写入的 TraderOrder。
- 两个提交均通过 `git merge-base --is-ancestor ... origin/main`，确认属于上游主线历史，而不是当前分支或本轮改造。
- 精确运行日志已给出余额不变的直接失败点：trading worker 周期性 backfill 这两条订单时抛出 `Unsupported direction 'buy_elina svitolina'` 与 `Unsupported direction 'buy_ekaterina alexandrova'`。`manual-buy` 把展示 outcome 文案拼成 `buy_{label}`，而 `SimulationService._direction_to_position_side` 只接受规范 YES/NO 方向，导致账户 Trade/Position/CashLedger 全部零写入。
- 当前主账户仍为 `945.550990`、16 笔，两个新订单均无 `simulation_ledger`；所以不存在“已扣款但 UI 没刷新”。后端却已经把路由响应和事件标成成功，是错误成功语义。
- `/api/traders/orders/all` 实际能返回这两条订单，但它们没有 `simulation_ledger.account_id`。`PositionsPanel` 在已选择主沙盒账户时会过滤所有 `linkedAccountId != selectedSandboxAccount` 的 Autotrader 行，因此这两条 unassigned 订单在用户当前视图不可见；账户页的 autotrader overlay 在上游代码中也把 `linkedSandboxAccountId` 固定为 null。
- 用户这两次下单实际选中了 trader `c7631836...`（`Official Source btc_eth_convergence 389d246`），即把体育市场的人工订单挂到了加密机器人上；Buy 对话框当前列出所有 enabled Trader，没有按来源/策略匹配或说明账户绑定，属于另一个必须收口的交互/权限边界。
- 原工作器与手动路由的 Shadow 账户解析优先级均为 `control.settings.shadow_account_id` 在前、`trader.metadata.shadow_account_id` 在后；隔离 API 证明不能通过悄悄反转该优先级实现，必须暂停调度、临时切换全局账户并在验证后恢复。
- 当前 `traders_latest` snapshot 有 8 条钱包共识机会，均已携带真实 token、market、YES/NO outcome 与 canonical direction；因此修复可完全依赖当前服务端 snapshot，不需要相信浏览器提交的展示名称或猜 token。
- 响应层额外发现并修复账户串写掩盖风险：Shadow 订单只有全部 ledger marker 指向 decision 固定的同一账户才允许返回 success；配置后来切换时，幂等 replay 仍以 decision 中原账户为准。
- 真实 HTTP 证明不是伪造订单：请求经过当前 snapshot、盘口和 75 bps spread gate；60.914 bps 样本以实际有效价 `0.497259...` 成交 `4.691275...`，在同一事务形成订单、模拟交易、模拟持仓、现金分录和 trader inventory。
- 幂等和拒绝路径已在运行库证明：同 request id 重放零新增；1621.622 bps 样本明确 409 且只有 skipped decision/session、无 TraderOrder 和经济写入。主账户在整个隔离证明期间零变化。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 官方终局事实必须同时记录 provider、provider market ID、condition ID、token/outcome identity、状态、结果、观测时间和原始证据摘要 | 防止数字 ID/token ID/condition ID 混用，便于审计与重放 |
| 终局抓取应位于热路径之外 | 保持下单热路径无 REST、低延迟和故障隔离 |
| 经济效果采用幂等 settlement identity，而不是依赖“函数只调用一次” | 重试、重启和重复事件在实盘必然发生 |
| `closed + resolved/winner` 才允许按 0/1 派彩；仅 outcome price 接近 0/1 只能作为候选或告警 | 避免盘口极端价造成提前、错误结算 |
| 现有卡单先只读生成补偿清单；是否写回单独审批 | 避免在架构改造时悄悄改变历史余额 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| 交易热路径拥有完整身份但禁止 REST；watchdog 允许 REST却丢失身份 | 设计独立终局状态层/协调器，统一身份后由非热路径获取官方事实 |
| 仓库脏工作树很大 | 设计和实施均记录精确文件与 diff，避免覆盖用户或前序工作 |
| 并行架构检索输出超过工具上限且一个 `rg` 子命令返回非零 | 已保留关键命中；后续改为按模块小范围读取，不重复大范围命令 |
| 钱包入口检索包含不存在的 `frontend/src/locales`，且首次计划日志补丁上下文不精确 | 改按真实文件小范围读取；失败均为只读/文档补丁且零生产代码修改 |
| 一次 PowerShell `rg` 命令的双引号转义不完整 | 改用单引号正则成功定位 `BuyButton`；失败仅为只读命令，未修改运行态 |
| 首次查询 `traders.status/created_by` 使用了不存在的列 | 已改查 `information_schema` 获取真实 17 列；失败查询只读且未写入数据库 |

## Resources
- `backend/services/trader_orchestrator/position_lifecycle.py`
- `backend/workers/trader_orchestrator_worker.py`
- `backend/workers/trader_reconciliation_worker.py`
- `backend/services/polymarket.py`
- `backend/services/simulation.py`
- `backend/models/database.py`
- `backend/tests/test_trader_position_lifecycle_resolution.py`
- `backend/tests/test_trader_orchestrator_shadow_backfill.py`
- `https://gamma-api.polymarket.com/markets/3412941`
- `https://gamma-api.polymarket.com/markets/3412928`

## Visual/Browser Findings
- 本任务暂不需要 UI 或架构图作为决策输入；以代码、数据库、官方接口与自动化测试证据为准。

## 2026-08-11 Shadow Timeout Repair Boundaries
- `ExecutionSession` 当前没有 decision 级唯一约束；本次不新增 schema，而是沿用单次 claim/submit 所有权，并用现有 session ID 绑定 intent、leg 与事件，避免在脏运行库上加入未经必要性证明的迁移。
- 现有 `cancel_session(..., skip_provider_io=True)` 能在无 venue order 时原子关闭 session、legs、signal 并写事件，可承担重启扫尾；热路径取消仍必须先有 durable Shadow intent，不能事后根据 trader 事件猜测对应 decision。
- Shadow leg submit 在行情/Redis await 后只返回内存撮合结果；账户、`TraderOrder` 和 `ExecutionSessionOrder` 由 session engine 最终投影时才持久化。因此超时收口必须丢弃未提交的内存成交投影，不能把超时前的估算误记为模拟成交。
- 当前目标中 `session_engine.py` 与两个聚焦测试文件为 Git-clean；`trader_orchestrator_worker.py` 已含前序未提交账本改动，后续只能追加并精确暂存本任务 hunk，不能把整文件旧改动混入本任务提交。
