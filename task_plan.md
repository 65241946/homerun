# Task Plan: HOMERUN 实盘级市场结算与收益账本完善

## Goal
基于 HOMERUN 现有基座，建立统一、幂等、可恢复、可审计的市场身份解析与 Shadow/Live 结算链，消除终局仓位未结算、重复派彩、错误盈亏和静默卡单风险，并完成模拟验证与部署核验。

## Current Phase
Phase 8：运行链缺口变更归因与根因修复设计

## Phases

### Phase 1: Requirements, Evidence & Existing Architecture
- [x] 固化当前天气仓位故障的可重复证据
- [x] 盘点市场身份、市场状态、Shadow 结算、Live 核验、模拟账本、事务和告警的现有链路
- [x] 区分配置、外部网络、接口兼容、缓存与代码责任
- [x] 记录必须保留的上游能力和当前脏工作树边界
- **Status:** completed

### Phase 2: Architecture Options & Approved Design
- [x] 给出 2–3 个完整修复方案及风险/容量/回滚对比
- [x] 明确实盘级不变量、失败策略和迁移/补偿范围
- [x] 获得用户设计批准
- [x] 写入并自审正式设计规格
- **Status:** completed

### Phase 3: Detailed TDD Implementation Plan
- [x] 映射精确文件、接口、数据流和事务所有权
- [x] 为每项行为定义 RED/GREEN 验证命令和回滚点
- [x] 写入并自审实施计划
- **Status:** completed

### Phase 4: TDD Implementation
- [x] 市场身份规范化与官方终局状态解析
- [x] 独立结算协调器与 Shadow/Live 分层写账
- [x] 幂等键、事务提交、重试、恢复与补偿
- [x] 监控、告警、审计字段与运维入口
- [x] 每项生产代码前先运行对应失败测试
- **Status:** completed

### Phase 5: Verification, Rebuild & Controlled Replay
- [x] 聚焦测试、相关回归、全量必要测试和静态检查
- [x] 本地固定源码重建，不使用浮动 latest 镜像
- [x] 补齐运行验证发现的历史 identity 只读发现/显式升级缺口
- [x] 在隔离模拟账户验证已结算、未结算、网络失败与重复运行
- [x] 既有卡住仓位仅生成只读 preview；历史 apply 与 Live 保持关闭，等待另行明确授权
- **Status:** completed

### Phase 6: Delivery & Operational Handoff
- [x] 输出修改清单、证据、风险、回滚和运行手册
- [x] 确认 API/worker 日志能区分官方终局、Shadow 账本入账和 Live 核验状态；未新增可视化
- [x] 经用户单独授权，仅对最新预览中的 5 笔安全历史订单执行原子 repair；写入后立即恢复 apply=false
- [x] 持久启用 `shadow` 自动结算，恢复交易/冷结算工作器并观察两个完整周期
- [x] 提交明确范围内代码；不混入无关脏工作树
- **Status:** completed

### Phase 7: Runtime Trading-Path Diagnosis
- [x] 核对所有模拟账户的创建时间、用途、订单/持仓/账本关联，解释第二账户来源
- [x] 追踪共识钱包从采集、池选择、共识信号、策略决策到 Shadow 订单和账户扣款的真实运行链
- [x] 分别核对天气、体育、加密、新闻的采集、机会、策略、决策、订单、持仓与阻断原因
- [x] 区分正常等待、配置缺失、数据源问题、风险门禁和代码缺陷；先完成根因证据，不先改代码
- [x] 输出按影响排序的结论与后续修复/配置方案
- **Status:** completed

### Phase 8: Forensic Attribution & Root-Cause Repair Design
- [x] 对照 `origin/main`、ahead commits、未提交 diff 与固定运行镜像，逐项确认问题是否由二开引入
- [x] 为 timeout 孤儿决策、钱包跟踪语义、第二账户、体育 trader、加密策略订阅和天气报价覆盖建立根因证据
- [x] 找出项目内相同职责的正常实现模式，避免旁路补丁和重复状态机
- [x] 给出 2–3 个修复边界方案及风险/回滚对比，并提交用户批准
- [x] 批准后写正式规格和逐项 TDD 实施计划，再按单一变更逐步实施
- **Status:** completed

### Phase 9: Shadow Timeout Root Repair (Approved)
- [x] 固化设计规格、事务不变量、明确文件边界和可回滚实施计划
- [x] RED：复现 Shadow 在外部 await 被取消后 selected-without-session 的缺口
- [x] GREEN：复用现有 ExecutionSession 持久化 pending intent，并在取消后原子收口
- [x] RED/GREEN：使内层行情网络等待受外层剩余预算约束，不通过调大总 timeout 掩盖问题
- [x] 增加 decision/session/stage/elapsed 审计并验证无敏感信息、无高频日志放大
- [x] 聚焦回归、正常 Shadow、Live cancellation、commit failure 与相关结算/账本回归
- [x] 固定镜像部署后的 restart/replay 运行回归
- [x] 构建固定源码镜像并核对镜像内目标源码哈希
- [x] 保持 Live 未配置，只切换 Shadow 所需后端/工作器并完成运行态验证
- [x] 使用事故前固定 R2 镜像与修复镜像隔离重放同一强制慢提交场景，形成 RED/GREEN 差异证据
- **Status:** completed

### Phase 10: Wallet Consensus Manual-Selection No-Order Diagnosis
- [x] 读取主账户、最新 traders signals/decisions/sessions 和订单事实，确认余额未变是因为没有订单写入
- [x] 追踪用户点击控件的前端 handler、请求方法、后端路由与数据库写入语义
- [x] 将该次操作与最新 signal/decision/event/session 按时间和 ID 对齐，区分“加入观察/池”“请求执行”和“自动策略评估”
- [x] 核对账户选择、trader 模式、策略参数、strict WS 与 spread/edge 门禁是否符合产品承诺
- [x] 建立单一根因假设并用运行日志/DB/源码归因闭环；未放宽门禁、未制造模拟订单
- [x] 固化用户批准的原子手动执行设计和逐项 TDD 计划
- **Status:** completed

### Phase 11: Wallet Consensus Manual Execution Atomic Repair
- [x] 冻结旧错误订单、主账户与运行镜像基线
- [x] RED/GREEN：服务端从当前机会快照重建 typed identity，拒绝 stale/conflict
- [x] RED/GREEN：manual decision 幂等 reservation，并统一进入 ExecutionSessionEngine
- [x] RED/GREEN：Shadow 订单、模拟交易、持仓、现金账本与余额同事务提交
- [x] 前端保留 token/canonical direction、稳定 request id、诚实 order type 与正确缓存刷新
- [x] 聚焦回归、固定镜像、隔离账户 API 原子性与幂等重放
- [ ] 用户主 Shadow 账户受控验证；历史错误 submitted 继续只读保留
- **Status:** in_progress

## Key Questions
1. 既有已结算但卡住的 Shadow 仓位，代码修复后是否允许自动补偿入账，还是先生成只读补偿预览再由用户批准？
2. 哪个组件拥有官方终局状态抓取权，哪个组件拥有账本写入与 commit 权？
3. 如何保证 Shadow、Live、链上核验和 UI 聚合共享同一市场身份但不共享错误的收益语义？
4. 外部接口超时、缓存陈旧、部分成功、进程重启和重复事件下如何保持 exactly-once 经济效果？

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| 当前阶段只读审计和设计，不直接改生产结算代码 | 资金与收益链必须先固定不变量、事务边界和回滚方案 |
| 保留交易热路径禁止 REST 的架构约束 | 不用结算修复换取下单延迟、事件循环阻塞和 WS 饥饿 |
| Shadow 与 Live 共用规范化市场身份/终局事实，分离资金写账适配器 | 共享事实、防止逻辑漂移，同时避免模拟账本污染实盘核验 |
| 历史补偿只处理用户批准且最新定向预览仍为 safe 的 5 个订单 ID | 不把授权扩大到 blocked/ambiguous 或后来新增的订单；摘要漂移时重新预览 |
| 不使用多模型协作 | 遵守用户明确偏好 |
| 不新建 Git worktree，在当前工作树实施并按文件/hunk隔离 | 新 worktree 只包含 Git HEAD，会遗漏当前运行基线依赖的未提交且已批准修复；在不同基线上验证反而增加交易风险 |
| 在当前脏工作树创建 `fix/shadow-timeout-root-repair` 专用分支 | 保留当前固定运行基线，同时避免继续直接在 `main` 上实施；不重置、不覆盖既有改动 |
| Shadow 修复不新增 migration，也不修改 `order_manager.py` | 根因位于 session durability 与 timeout budget；Shadow leg 尚无经济写入，现有模型和取消服务足以表达正确状态 |
| 钱包共识手动下单复用 ExecutionSessionEngine，不保留 direct TraderOrder 旁路 | 统一使用现有 typed identity、盘口、风险、Live placeholder 与 session 状态机，避免第二套交易语义 |
| Shadow inline ledger 仅由 manual route 显式传入 account id 启用 | 修复当前人工链且不在同一变更中迁移全部自动 Shadow 调用；订单与经济账本仍能原子提交 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| 之前只读查询使用字符串比较 UTCDateTime 导致 TypeError | 1 | 改用 timezone-aware datetime；未产生写入 |
| 主机 urllib 未带 User-Agent 请求 Gamma 返回 403 | 1 | 使用明确 User-Agent；随后官方接口成功返回 |
| 初次批量天气查询在 httpx client 关闭后执行 | 1 | 调整 async client 生命周期后成功；未重复错误方式 |
| 并行架构检索输出截断且一个子命令非零 | 1 | 改用按模块分段读取 |
| 规划文件更新补丁含错误路径和空 hunk | 1 | 使用正确路径并拆分有效 hunk |
| 首次读取 Superpowers 技能使用了不存在的 `.codex/skills/.system/superpowers` 路径 | 1 | 按技能根目录映射改用 `plugins/cache/personal/superpowers/5.1.0/skills` 并成功完整读取 |
| worktree/catchup 并行探测因“无 superproject/无 worktrees 目录”返回非零导致组合调用失败 | 1 | 将 catchup 独立执行并显式保持只读命令成功；确认无未同步上下文 |
| 迁移与测试辅助检索的 PowerShell 组合命令存在未闭合字符串 | 1 | 停止复用该组合命令，改为逐文件、逐命令读取 |
| 组合 `rg` 中无匹配项返回 exit 1，导致整组只读检索被标记失败 | 1 | 后续对可选匹配显式容忍无结果，并拆分关键检索 |
| 一次广范围 conftest/迁移输出超过上下文上限 | 1 | 改为只读取目标 migration、Postgres helper 与测试节点附近行 |
| 完整 `pg_dump` 超过首次 60 秒外层命令时限 | 1 | 确认容器内 dump 进程继续正常 I/O，等待自然完成后再校验和复制；未使用中间文件 |
| `pg_restore --list | Select-Object -First 5` 因消费端提前关闭返回非零 | 1 | 改为完整读取到 `/dev/null`；归档验证通过 |
| 恢复会话时按旧 `.agents/skills/superpowers` 路径读取技能失败 | 1 | 按技能目录映射改用 `.codex/plugins/cache/personal/superpowers/5.1.0/skills`；未触碰项目 |
| 一次并行只读恢复因单个可选命令非零而未返回其余输出 | 1 | 改为逐项捕获退出码并继续；没有重复失败方式 |
| 查询 WorkerSnapshot 表名的 PowerShell 双引号未闭合 | 1 | 改用单引号模式后成功；只读命令未触碰项目 |
| 迁移后业务基线查询使用不存在的 `simulation_trades.actual_profit` | 1 | 先读 information_schema，再使用真实 `actual_payout/actual_pnl/fees_paid` 列 |
| 两次订单明细查询使用不存在的 `filled_at/closed_at` | 1 | 查询真实列清单后改用 `executed_at`；均为失败的只读 SELECT |
| observe 运行验证发现历史 preview 未生成 proposed identity | 1 | 对照批准规格确认是实现遗漏；新增 Task 10A，先 RED 测试再修复 |
| Task 10A 完整回归有 4 项既有 repair 测试失败 | 1 | 公共测试夹具用伪 condition/token 却标记 `complete`；改为确定性合法链上 ID，不放宽生产校验，随后 23/23 与 73/73 通过 |
| 受控账户账本查询使用不存在的 `sequence` | 1 | 查询模型与 information_schema 后改用 `ledger_sequence`；失败 SELECT 未写入 |
| 恢复交易 worker 时误用 `docker compose start` 拉起陈旧 migrate 容器 | 1 | 数据库 head 未变；固定 `.env` 为 R2，用 R2 重建 migrate 并退出 0，后续一律使用 `up --no-deps` |
| Phase 7 一次 `rg` 命令因 PowerShell 双引号与正则双引号冲突解析失败 | 1 | 改用单引号包裹正则；失败发生在只读检索，未触碰运行状态 |
| Phase 7 组合检索对 PowerShell 不支持的 `docker-compose*.yml/.env*` 路径通配符返回非零 | 1 | 容器清单已成功取得；后续改用 `rg --files -g` 或显式文件名，不重复该写法 |
| Phase 7 一次复合正则同时混用 PowerShell 单引号与正则引号导致解析失败 | 1 | 拆成两个 `rg -F` 和一个简单正则后成功；未触碰代码或运行数据 |
| Phase 7 钱包关联查询误用不存在的 `trader_decisions.updated_at` 与复数表名 `trader_signal_consumptions` | 1 | 读取 ORM 后确认只有 `created_at`，真实表名为单数 `trader_signal_consumption`；改用真实 schema 复查 |
| Phase 7 模块聚合查询误认为 `traders` 表直接含 `source/strategy_key/status/enabled` 列 | 1 | 读取 information_schema 后确认 source/strategy 属于 decision/order 或 `source_configs_json`，启停列为 `is_enabled/is_paused`；按真实结构重查 |
| Phase 7 天气快照查询使用不存在的 `next_scan_at/last_error` 列 | 1 | 先读取 `weather_snapshot` 真实列清单，再按现有 9 列重查；失败查询只读 |
| Phase 7 策略表查询误用 `is_enabled/last_error` 列 | 1 | 真实列为 `enabled/error_message`；按 information_schema 修正并确认相关策略均为 loaded |
| Phase 7 首次仓库检索使用了不存在的根级 `services/tests` 路径 | 1 | 改用真实 `backend/services`、`backend/tests`、`backend/workers` 路径；只读失败未影响运行态 |
| Phase 7 crypto snapshot 查询误用不存在的 `status` 列 | 1 | 读取 `worker_snapshot` schema 后改用 `running/enabled/current_activity`；只读失败未写入 |
| Phase 7 tracked wallet event 查询误用不存在的 `trade_timestamp` 列 | 1 | 按真实 schema 使用 `detected_at` 复查；只读失败未写入 |
| Phase 7 多文件规划补丁精确上下文不匹配 | 1 | 先读取文件尾部并拆分为精确 hunk；未触碰生产代码或运行数据 |
| Phase 9 规划记录补丁引用了实际位于 `task_plan.md` 的句子 | 1 | 校验失败且零修改；读取真实尾部后按稳定标题分别追加 |
| Phase 9 固定镜像构建命令的外层等待窗口过短，命令返回 124 | 1 | 未把工具超时当成构建失败；复查发现 Docker 后台构建已完成，目标镜像存在，随后逐文件 SHA-256 与主机源码一致 |
| Phase 9 首次将镜像检查与 compose 状态合并输出导致工具截断 | 1 | 改为单一、精简的镜像 inspect 与哈希命令；不再依赖被截断结果 |
| Phase 9 规划日志补丁对 `progress.md` 使用了不精确的句尾上下文 | 1 | 整个补丁校验失败且零修改；拆分文件并按实际行内容追加 |
| Phase 9 首次在生产镜像内部直接运行 tests 路径，目录不存在 | 1 | 确认生产 `.dockerignore` 未打包测试；将主机 tests 只读挂载到临时路径，测试仍加载镜像内生产代码，136 项通过 |
| Phase 9 运行证据补丁误把已完成的聚焦回归写成待办上下文 | 1 | 整个补丁校验失败且零修改；按当前真实复选状态拆分追加 |
| Phase 9 PostgreSQL 测试设施检索包含不存在的根级 `backend/database.py` 等可选路径，`rg` 返回 1 | 1 | 有效结果已取得；后续仅使用真实 `backend/models/database.py`、`backend/tests` 路径 |
| Phase 9 模型类检索正则使用 PowerShell 双引号导致管道字符被解释为命令 | 1 | 改用单引号包裹正则后成功；失败只读且未触碰运行态 |
| Phase 9 静态配置检索包含不存在的根级 `pyproject.toml`，`rg` 返回 1 | 1 | 使用真实 `backend/pyproject.toml`；确认 line-length=120，新 PostgreSQL 测试范围无超长行且 B023 通过 |
| Phase 9 首次用 PowerShell 管道驱动 `git add -p`，首个回答带 BOM 导致 hunk 选择错位 | 1 | 立即撤销该文件全部暂存且不改工作树；改用显式 zero-context patch，缓存中只保留 timeout 常量、参数透传和预算计算，历史 backfill hunk 为 0 |
| Phase 9 新 PostgreSQL 测试直接导入了未提交结算模型，暂存 commit 不自包含 | 1 | 改为运行时检查可选账本表；表存在时严格计数为 0，HEAD 无该表时测试仍可独立收集运行，未混入旧结算改动 |
| Phase 9 临时暂存树全文件回归在 Windows 出现 1 个额外 cleanup flush 断言失败 | 1 | 同一用例在原始 HEAD 同环境稳定复现，确认是既有平台时序断言；固定 Linux 镜像 137/137 通过，本次相关暂存树 37/37 通过，不修改无关断言 |
| Phase 9 尝试使用未安装的 pytest-repeat `--count=3` | 1 | 命令未执行测试；改为 PowerShell 显式循环三次，得到 3/3 相同基线失败后再用原始 HEAD 归因 |
| Phase 9 提交后更新计划状态的首个补丁遗漏了 `patch 并` 中的空格 | 1 | 整个补丁校验失败且零修改；读取精确行后重新更新 |

## Notes
- 仓库当前存在大量与新闻、钱包、体育、模拟账本等相关的未提交修改；实施时必须逐文件核对并只追加本任务必要变更。
- 当前运行镜像与本地 `position_lifecycle.py`、`trader_orchestrator_worker.py`、`polymarket.py` 哈希一致。
- 历史修复 apply 仍默认关闭；未经单独批准，不写回已有订单、余额或实盘状态。
- 当前运行库为 `202608100002`；backend 与全部 Python worker 均使用固定镜像 `local-shadow-timeout-20260811-r1`（ID `sha256:914841f...779a`），迁移检查 exit 0。持久配置为冷结算 `shadow`、historical apply=false、Live 未配置。
- 用户批准的 5 笔历史安全订单已结算：净派彩 `26.147042`、已实现盈亏 `3.054009`；既有账户余额为 `945.550990`，账本 v2/checkpointed、差额 `0.000000`、序列连续。
- Shadow 恢复后的两个周期均无 network/persistence/apply error；第二周期 `duration_ms=782`，没有重复经济写入。
- 隔离 v2 账户一胜一负后余额 `1047.000000`、净盈亏 `47.000000`，100 轮/200 次重放不增写，账本差额 `0.000000`；网络超时后真实周期已恢复。
