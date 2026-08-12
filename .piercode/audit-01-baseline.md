# Settlement chain audit — Batch 1: independent baseline verification

### Verdict

独立检查不支持完整的自报基线：Claim 2、3、5 得到支持；Claim 1、4、7 被当前证据反驳；Claim 6 的实现和测试只支持更窄的顺序重放性质，完整声明无法验证。

### Per-claim results

> **Claim 1**: 扩大交易链回归为 `83 passed, 1 warning in 169.16s`，手动执行专项为 `7 passed`。
>
> **Method**: 在仓库根目录设置 `PYTHONDONTWRITEBYTECODE=1`、`HOMERUN_ALLOW_TEST_WALLET_DB_WRITES=0` 并禁用 pytest cache。实际运行：
>
> ```powershell
> $env:PYTHONDONTWRITEBYTECODE='1'; $env:HOMERUN_ALLOW_TEST_WALLET_DB_WRITES='0'; python -m pytest backend/tests/test_routes_trader_manual_buy.py -q -p no:cacheprovider
> $env:PYTHONDONTWRITEBYTECODE='1'; $env:HOMERUN_ALLOW_TEST_WALLET_DB_WRITES='0'; python -m pytest backend/tests/test_execution_session_engine.py -k "inline_shadow_ledger" -q -p no:cacheprovider
> $env:PYTHONDONTWRITEBYTECODE='1'; $env:HOMERUN_ALLOW_TEST_WALLET_DB_WRITES='0'; python -m pytest backend/tests/test_routes_trader_manual_buy.py backend/tests/test_execution_session_engine.py backend/tests/test_simulation_cash_ledger.py backend/tests/test_trader_orchestrator_shadow_backfill.py backend/tests/test_trader_order_manager_live.py -q -p no:cacheprovider
> $env:PYTHONDONTWRITEBYTECODE='1'; python -m pytest backend/tests/test_routes_trader_manual_buy.py backend/tests/test_execution_session_engine.py backend/tests/test_simulation_cash_ledger.py backend/tests/test_trader_orchestrator_shadow_backfill.py backend/tests/test_trader_order_manager_live.py --collect-only -q -p no:cacheprovider
> ```
>
> **Observed**: 手动路由为 `4 passed in 66.71s`；inline Shadow ledger 为 `3 passed, 34 deselected in 28.96s`，专项合计仍为 `7 passed`。扩大回归当前只收集 `73 tests collected in 1.95s`，实际结果为：
>
> ```text
> ERROR backend\tests\test_trader_orchestrator_shadow_backfill.py::test_backfill_passes_shadow_simulation_fee_and_slippage_to_ledger
> ERROR backend\tests\test_trader_orchestrator_shadow_backfill.py::test_backfill_repairs_marker_when_referenced_ledger_rows_are_missing
> 71 passed, 1 warning, 2 errors in 140.82s (0:02:20)
> sys:1: RuntimeWarning: coroutine 'Connection._cancel' was never awaited
> ```
>
> 两项 ERROR 都发生在 `tmp_path` fixture setup：`PermissionError: [WinError 5] 拒绝访问。: 'C:\Users\Administrator\AppData\Local\Temp\pytest-of-Administrator'`，不是断言失败。
>
> **Result**: REFUTED
>
> **Note**: `7 passed` 子声明已复现；`83 passed` 子声明未复现，而且当前同一五文件目标总共只有 73 项。历史命令使用的 `py -3.12` 当前返回 `No installed Python found!`，因此改用 PATH 上的 Python 3.12.8 / pytest 7.4.3。测试所需数据库均为随机命名的隔离 `homerun_test_*` 数据库，不写 live `homerun`；这些 suites 自身会创建、写入并删除隔离测试库。如果“不得修改任何数据库行”也包含临时测试库，则应舍弃执行结果，但只读的 `--collect-only` 仍以 73 项直接反驳当前 suite 可产生 `83 passed`。两项受阻测试未通过新建 `--basetemp` 绕过只读约束。

> **Claim 2**: Alembic 单一 head 为 `202608100002`；升级为 additive；迁移时的历史账户默认 v1/legacy。
>
> **Method**: 在 `backend` 目录运行 `$env:PYTHONDONTWRITEBYTECODE='1'; python -m alembic -c alembic.ini heads --verbose`；逐行检查 `backend/alembic/versions/202608100002_online_settlement_ledger.py` 的 `upgrade()`；在 live DB 的 `transaction(readonly=True)` 中执行：
>
> ```sql
> SELECT version_num FROM alembic_version;
> SELECT column_name, column_default, is_nullable
> FROM information_schema.columns
> WHERE table_schema='public'
>   AND table_name='simulation_accounts'
>   AND column_name IN ('ledger_version','ledger_integrity_status')
> ORDER BY column_name;
> ```
>
> **Observed**:
>
> ```text
> Rev: 202608100002 (head)
> Parent: 202608100001
> live alembic_version=202608100002
> ledger_integrity_status default='legacy'::character varying, nullable=NO
> ledger_version default=1, nullable=NO
> ```
>
> `upgrade()` 只调用 `safe_add_column`、`safe_create_index`、`safe_create_table`；未发现 upgrade 方向的删除、重命名、覆盖或历史余额推断。
>
> **Result**: VERIFIED
>
> **Note**: “additive”只描述 `upgrade()`；`downgrade()` 会删除新增对象。`1/legacy` 是迁移时历史账户的默认值，新建账户由服务显式初始化为 v2/complete。本批未通过写入隔离旧账户再迁移的方式重演历史默认落值。

> **Claim 3**: 主 Shadow 账户保持余额 `945.550990`、PnL `3.054009`、16 笔交易。
>
> **Method**: 使用 `asyncpg` 的 `transaction(readonly=True)` 执行：
>
> ```sql
> SELECT a.id, a.current_capital::numeric(20,6), a.total_pnl::numeric(20,6),
>        a.total_trades,
>        (SELECT count(*) FROM simulation_trades t WHERE t.account_id=a.id) AS trade_rows,
>        a.ledger_version, a.ledger_integrity_status
> FROM simulation_accounts a
> WHERE a.id='4473f507-5e6e-4269-b04a-de4a967284a0';
> ```
>
> **Observed**: `current_capital=945.550990`, `total_pnl=3.054009`, `total_trades=16`, `trade_rows=16`, `ledger_version=2`, `ledger_integrity_status=checkpointed`（观测时间 `2026-08-11T12:20:41.212220Z`）。
>
> **Result**: VERIFIED
>
> **Note**: 当前快照与自报数字逐位一致；单次快照只能证明当前状态，不能严格证明期间从未发生过临时变化后又被冲正。

> **Claim 4**: 隔离 proof 账户在证明后为 `1042.308725`，共 3 笔交易。
>
> **Method**: 在同一个 live DB `transaction(readonly=True)` 中执行：
>
> ```sql
> SELECT a.id, a.current_capital::numeric(20,6), a.total_pnl::numeric(20,6),
>        a.total_trades,
>        (SELECT count(*) FROM simulation_trades t WHERE t.account_id=a.id) AS trade_rows,
>        (SELECT count(*) FROM simulation_cash_ledger_entries l WHERE l.account_id=a.id) AS cash_rows,
>        a.ledger_version, a.ledger_integrity_status
> FROM simulation_accounts a
> WHERE a.id='settlement-validation-r2-20260811';
>
> SELECT id, status::text, total_cost::numeric(20,6),
>        actual_payout::numeric(20,6), actual_pnl::numeric(20,6),
>        executed_at, resolved_at
> FROM simulation_trades
> WHERE id='ff467403-9bde-43b9-8669-5a9026b63188';
>
> SELECT ledger_sequence, entry_type, amount_usdc::numeric(20,6), occurred_at
> FROM simulation_cash_ledger_entries
> WHERE simulation_trade_id='ff467403-9bde-43b9-8669-5a9026b63188'
> ORDER BY ledger_sequence;
> ```
>
> **Observed**: 账户当前为 `current_capital=1047.302550`, `total_pnl=47.302550`, `total_trades=3`, `trade_rows=3`, `cash_rows=7`，不是 `1042.308725`。第三笔交易当前为：
>
> ```text
> status=CLOSED_WIN
> total_cost=4.691275
> actual_payout=4.993825
> actual_pnl=0.302550
> executed_at=2026-08-11 02:55:56.239724 UTC
> resolved_at=2026-08-11 03:46:20.838651 UTC
> sequence 6: entry_debit       -4.691275
> sequence 7: settlement_credit +4.993825
> ```
>
> **Result**: REFUTED
>
> **Note**: 交易数仍为 3；余额相对自报的开仓后快照增加 `4.993825`，原因是该 proof trade 随后结算并入账。账本支持 `1042.308725` 曾是 proof 开仓完成后的历史值；但本任务核验的是当前 live baseline，故该当前状态声明判为 REFUTED。

> **Claim 5**: 两笔历史卡单仍为 `submitted`、空 token、非规范方向且没有 `simulation_ledger`。
>
> **Method**: 依据 `progress.md` 的时间、金额和方向定位两笔订单，然后在 live DB 只读事务中按精确 ID 查询订单字段、payload marker 和现金账本引用：
>
> ```sql
> SELECT o.id, o.status, COALESCE(o.token_id,''), o.direction,
>        COALESCE(o.payload_json->>'token_id',''), o.payload_json->'simulation_ledger',
>        (SELECT count(*) FROM simulation_cash_ledger_entries l
>         WHERE l.trader_order_id=o.id) AS cash_ledger_rows
> FROM trader_orders o
> WHERE o.id IN ('a34cebe40064412dafb954c1da69b319',
>                '78e02caa5f484dfda341f477db29c397')
> ORDER BY o.created_at;
> ```
>
> **Observed**:
>
> ```text
> a34cebe40064412dafb954c1da69b319 | submitted | token='' | payload token='' | buy_elina svitolina          | simulation_ledger=null | cash_ledger_rows=0
> 78e02caa5f484dfda341f477db29c397 | submitted | token='' | payload token='' | buy_ekaterina alexandrova | simulation_ledger=null | cash_ledger_rows=0
> ```
>
> 两行的 `updated_at` 分别仍为 `2026-08-11 01:19:06.987351` 和 `2026-08-11 01:22:00.201591` UTC。
>
> **Result**: VERIFIED
>
> **Note**: 结论只针对上述两笔。按“shadow + submitted + 空 token + 非规范方向 + 无 ledger marker”的广义条件，当前共有 9 条候选，不能把本结论表述为全系统只有两条卡单。

> **Claim 6**: 固定 request id 重放会返回完全相同的 IDs，且经济状态零变化。
>
> **Method**: 运行下列只读检索，并逐行读取命中函数及测试上下文；未按任务要求对 live 数据重放：
>
> ```powershell
> rg -n "client_request_id|manual_decision|request_fingerprint|idempotency|manual-buy|manual_buy" backend/api/routes_traders.py backend/tests/test_routes_trader_manual_buy.py
> rg -n "inline_shadow_ledger|idempotent|replay|simulation_ledger|account" backend/tests/test_execution_session_engine.py
> ```
>
> **Observed**: decision ID 使用 `UUID5(trader_id + client_request_id)`；请求指纹覆盖 trader、opportunity、request id、order type、size 和 positions。已有 decision 且当前前置门禁仍通过时，路由直接读取原 session/order，不再次调用执行引擎；指纹冲突返回 409；并发 reservation 冲突有 `IntegrityError -> rollback -> 读取获胜 decision` 分支。路由测试只断言 replay 的 session/order ID 相同、fake engine 总调用一次，以及同键不同金额返回 409。三个 inline ledger 测试分别验证首次原子写、余额不足回滚和 no-fill 零写入。
>
> **Result**: UNVERIFIABLE
>
> **Note**: 自动测试没有用真实 `ExecutionSessionEngine` 做“首次请求 + replay”的端到端组合，没有直接断言 decision/trade/position/cash-ledger 全部 ID 相同，也没有比较重放前后余额及所有经济表计数。它只证明“成功完成后、配置与门禁未变化的顺序重放复用 session/order，且不再次调用 engine”这一更窄性质；新 HTTP session、进程重启、并发同键请求和配置变化均未被完整证明。计划中的 `-k "idempotent or gate or no_fill"` 实际只收集 2/4 个路由测试，并会漏掉名称不含 `idempotent` 的真正重放断言。

> **Claim 7**: 部署以来 runtime logs 无 traceback、无 failures。
>
> **Method**: 尝试运行：
>
> ```powershell
> docker compose logs --no-color --since '2026-08-11T02:50:00Z' backend worker-discovery worker-news worker-recording worker-trading worker-detection worker-jobs worker-reconciliation worker-services
> ```
>
> Docker stdout 不可访问后，以 proof trade 的 `executed_at=2026-08-11T02:55:56.239724Z` 作为“新部署已确定生效”的保守下界，在 live DB 的 `transaction(readonly=True)` 中执行以下查询。冻结查询窗口为 `[2026-08-11T02:55:56.239724Z, 2026-08-11T12:19:58.553323Z]`：
>
> ```sql
> SELECT count(*), min(created_at), max(created_at)
> FROM trader_events
> WHERE created_at >= TIMESTAMP '2026-08-11 02:55:56.239724'
>   AND created_at <= TIMESTAMP '2026-08-11 12:19:58.553323'
>   AND event_type='shadow_ledger_backfill_failed';
>
> SELECT event_type, severity, count(*), min(created_at), max(created_at)
> FROM trader_events
> WHERE created_at >= TIMESTAMP '2026-08-11 02:55:56.239724'
>   AND created_at <= TIMESTAMP '2026-08-11 12:19:58.553323'
>   AND lower(coalesce(severity,'')) IN ('error','critical')
> GROUP BY event_type, severity
> ORDER BY count(*) DESC, event_type;
>
> SELECT count(*)
> FROM trader_events
> WHERE created_at >= TIMESTAMP '2026-08-11 02:55:56.239724'
>   AND created_at <= TIMESTAMP '2026-08-11 12:19:58.553323'
>   AND (lower(event_type) LIKE '%manual%fail%'
>        OR lower(message) LIKE '%manual execution failed%');
>
> SELECT count(*)
> FROM trader_events
> WHERE created_at >= TIMESTAMP '2026-08-11 02:55:56.239724'
>   AND created_at <= TIMESTAMP '2026-08-11 12:19:58.553323'
>   AND (message LIKE '%Traceback (most recent call last)%'
>        OR payload_json::text LIKE '%Traceback (most recent call last)%');
> ```
>
> **Observed**: Docker 命令返回 `open //./pipe/dockerDesktopLinuxEngine: Access is denied.`。冻结窗口内：
>
> ```text
> shadow_ledger_backfill_failed = 2309
>   first = 2026-08-11 02:56:21.328266 UTC
>   last  = 2026-08-11 12:19:35.097303 UTC
> severity error/critical      = 24
>   12 x execution_failed
>   12 x decision
>   message = Shadow execution deadline exceeded during venue_preflight.
> manual execution failed      = 0
> DB Python traceback marker   = 0
> ```
>
> 首条 backfill failure 指向订单 `1a901e0a45aa460da5f0630ba5108a43`，原因 `record_orchestrator_shadow_fill_failed`，错误 `Unsupported direction 'buy_dn soopers'`。
>
> **Result**: REFUTED
>
> **Note**: “无 failures”已有部署生效后的持久运行事件直接反证。DB 事件中没有 traceback 标头不等于容器 stdout/stderr 没有 traceback；该子项因 Docker API 权限不足仍不可验证。`2309` 是固定上界时刻的快照，运行中的系统随后可能继续增长。

### Discrepancies

1. `progress.md` 的扩大回归是 `83 passed, 1 warning in 169.16s`；当前同一五文件目标只有 73 项，实际为 `71 passed, 1 warning, 2 errors in 140.82s`。两项是系统临时目录权限导致的 fixture setup error，但 suite 仍不是全绿，也不可能在当前收集结果下产生 83 passed。
2. `progress.md` 的手动专项 `7 passed` 可以复现，但当前拆分结果和耗时为 `4 passed in 66.71s` 加 `3 passed, 34 deselected in 28.96s`。
3. 历史使用的 `py -3.12` 当前不可运行，返回 `No installed Python found!`；本次使用 PATH 上的 Python 3.12.8。
4. proof 账户不再是 `1042.308725`，当前为 `1047.302550`。第三笔交易已在 `2026-08-11T03:46:20.838651Z` 结算，新增 `+4.993825` settlement credit。
5. `progress.md` 声称部署与 proof 后无 `shadow_ledger_backfill_failed`；在新版已确定生效后的冻结窗口内实际有 2309 条该事件，另有 24 条 error 级运行事件。
6. 幂等专项测试没有证明自报声明中的全部 ID 恒等和全部经济计数零变化；它只证明 fake-engine 顺序 replay 复用 session/order 且 engine 调用一次，并把首次经济原子性放在另一组测试中验证。

### What this batch does NOT establish

- 测试通过不证明财务设计、价格来源、费用、滑点、结算结果或盈亏公式整体正确。
- 本批没有配置 Live 凭据、发送真实订单、调用真实 provider 重放或验证链上成交/赎回。
- 当前工作树测试结果未独立证明与正在运行的固定 Docker 镜像逐字节一致；Docker inspect/logs 权限不足。
- 当前数据库快照不能证明主账户在整个期间从未被临时修改后又冲正。
- Alembic 单 head 和 additive 源码不能证明任意历史数据量、锁竞争、异常半迁移状态或回滚都安全。
- 现有测试没有覆盖所有真实进程崩溃、跨进程并发、响应丢失、worker 竞态和重启时序。
- DB 事件里的零 traceback 匹配不能替代容器 stdout/stderr 的完整日志审计。
- 本批没有清理、修复或重新提交任何历史卡单，也没有重新执行 live proof request。

### Unverifiable

- 扩大回归中两项 `tmp_path` 测试在当前权限下无法完成。要验证它们，需要一个已授权、可读写的隔离 pytest 临时目录；本任务只允许创建审计报告，因此未新建 `--basetemp`。
- 容器 stdout/stderr 自精确部署时刻起是否存在 Python traceback 无法验证。需要 Docker named pipe 的只读权限或一份完整、带时间戳的导出日志，再按真实 container `StartedAt` 检索。
- 历史 fixed request id 的“全部 ID 完全相同且所有经济计数零变化”没有独立原始 HTTP/DB 快照，且任务禁止对 live 数据重放。完整验证需要在获授权的隔离数据库中，用真实 ExecutionSessionEngine 做首次请求、顺序 replay、并发 replay 和重启后 replay，并逐表比较前后状态。
- 迁移时既有账户实际被赋为 v1/legacy 的动态行为未通过本批写入测试重演；若要补证，需要在隔离数据库中先插入旧 schema 账户，再执行 `202608100001 -> 202608100002` 并核对行值。
