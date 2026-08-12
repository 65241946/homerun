# fix-03 实施结论

实施日期：2026-08-12
分支：`claude/batch-2-identity-finality-review-jvcsc5`

## P0-a：现行费用曲线

- `backend/utils/kelly.py:7`：新增 `FEE_RATE_BY_CATEGORY`，录入实施当日 Polymarket 官方类目费率；`backend/utils/kelly.py:22` 的 `DEFAULT_FEE_RATE=0.07` 取当前最高费率，未知/缺失类目不会误落到 0。
- `backend/utils/kelly.py:70`：`polymarket_taker_fee` 改为现行 `feeRate × p × (1-p)`；显式 `fee_rate` 优先，其次类目，最后保守缺省。价格继续夹在 `[0,1]`。
- `backend/utils/kelly.py:91`：新增显式 maker 零费函数；`backend/utils/kelly.py:102`：旧四次曲线只保留为 `polymarket_taker_fee_legacy_quartic`，docstring 明确仅供 2026-01 前历史回测对照；`backend/utils/kelly.py:111`：百分比 helper 透传 `category/fee_rate`。
- `backend/services/strategies/crypto_strategy_utils.py:100`、`:320`：两个加密费用 helper 均委托 `utils.kelly.polymarket_taker_fee_pct(..., category="crypto")`，删除重复四次公式。所有经这两个 helper 的加密策略随之使用单一实现。
- `backend/tests/test_kelly_fee_regime.py:27`：新增数值表、类目顺序、未知类目、显式费率优先、maker 零费、legacy 对照和委托一致性回归测试。旧的 `backend/tests/test_kelly_polymarket_fee.py` 已删除，避免继续把历史四次曲线锁成现行制度。

### 实施当日官方核对

- Polymarket 官方 [Fees 页面](https://docs.polymarket.com/trading/fees) 当前公式为 `C × feeRate × p × (1-p)`，maker fee 为 0。类目费率为：Crypto 0.07、Sports 0.05、Finance 0.04、Politics 0.04、Economics 0.05、Culture 0.05、Weather 0.05、Other/General 0.05、Mentions 0.04、Tech 0.04、Geopolitics 0。spec 中列出的 Crypto/Sports/Politics/Geopolitics 四项与官方一致，无需以官方值覆盖 spec；实现同时补齐了官方页面其余类目。
- Polymarket 官方 [Predictions changelog](https://docs.polymarket.com/changelog/predictions) 还说明市场对象会提供 `feeSchedule`。本批次按 spec 实现类目表与显式 `fee_rate` 覆盖口，未扩展到市场 API 的 `feeSchedule` 接线。
- Kalshi 官方 [Fee Schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf)（2026-07-07 生效）当前通用 taker 为向上取整的 `M × 0.07 × C × P × (1-P)`，通用 maker 为 `M × 0.0175 × C × P × (1-P)`，且不同 series 可有 multiplier。本 P0-a 修复对象是 Polymarket helper，因此未把 Kalshi 的合约数、向上取整和 series multiplier 混入 Polymarket 实现，也未改 Kalshi 代码。

### 费用调用点逐项处理

- `backend/services/strategies/crypto_strategy_utils.py:102`、`:325`：上下文确定为 crypto，显式传 `category="crypto"`；`crypto_distance_edge` 等间接调用者自动继承。
- `backend/services/strategies/crypto_entropy_maker.py:424`、`backend/services/strategies/crypto_spike_reversion.py:345`：策略只处理 crypto，显式传类目。
- `backend/services/strategies/ctf_basic_arb.py:280`、`:372`：函数已有父 `Event`，使用 `event.category`；没有父事件时由 helper 保守回退 0.07。
- `backend/services/strategies/stat_arb.py:955`：检测路径已经解析父事件类目，直接透传。
- `backend/services/backtest/matching_engine.py:266`：`BacktestOrder` 不携带市场类目，保守走 `DEFAULT_FEE_RATE`，调用处已注释。
- `backend/services/fee_model.py:131`：该通用 API 只收到平台和逐腿价格，没有类目，保守走缺省并注释。
- `backend/services/strategies/base.py:1826`：通用 edge helper 的签名没有市场/事件上下文，保守走缺省并注释。
- `backend/services/strategies/cross_platform.py:771`：费用 helper 只收到价格，没有父事件参数，保守走缺省并注释。
- `backend/utils/kelly.py:154`、`:172`：`fee_adjusted_edge`/`breakeven_edge` 是只含价格的平台通用 helper，保守走缺省并注释。
- `backend/tests/test_backtest_engine.py:766`：测试期望值直接调用现行 canonical helper，未写死另一套曲线。
- 全仓 grep 结果：`polymarket_taker_fee_legacy_quartic` 仅在定义和历史对照回归测试出现；生产代码没有调用 legacy 函数。

费用上调后，旧测试夹具不再满足真实闸门：`backend/tests/test_crypto_strategy_safety_gates.py:51`、`:149` 只提高成功路径的行情强度，`backend/tests/test_strategy_threshold_single_source.py:62`、`:86` 只调整 CTF 测试报价；生产阈值没有放宽。

## P0-b：trader source_flags

- `backend/services/trader_data_access.py:237`、`:246`：核实 `include_source_context` 缺省仍为 `True`，缺省路径会执行真实 provenance 注入；无需改动该既有正确实现。
- `backend/services/traders_firehose_pipeline.py:187`：删除 `include_source_context=False`，UI/API 策略管道恢复真实来源信息；`:151` 在进入策略前不再提前 normalize，以免把“缺键”不可逆地变成“显式全 False”。
- `backend/services/strategies/traders_confluence.py:755`：`detect_async` 删除显式 False，恢复默认 provenance 查询。
- `backend/services/strategies/traders_confluence.py:147`、`:328`、`:365`、`:499`：保留缺失 provenance 与显式 False 的语义差别，防止 normalize 抹平信息；完全缺少 `from_pool/from_tracked_traders/qualified` 时以 `missing_source_provenance` 拒绝，不再误报 `unqualified_wallet_source/source_scope_mismatch`。显式全 False 仍按真实不合格来源拒绝。
- `backend/services/strategies/traders_confluence.py:430`、`:453`、`:840`：`firehose_exclude_crypto_markets`、`min_tier`、`min_confluence_strength` 的 inline fallback 全部引用 `DEFAULT_CONFIG`，消除三处死缺省。
- `backend/tests/test_traders_firehose_provenance.py:40`：覆盖真实 pool provenance 通过、缺失 provenance 独立原因、显式 False 仍拒绝、data-access 默认注入、策略管道端到端返回 `>0`、`detect_async` 默认注入以及三个配置缺省/覆盖行为。

provenance 查询有数据库成本：在本机已运行的 Postgres 上，用 1 行/1 钱包调用 `annotate_trader_signal_source_context` 实测 160.98 ms（单次冷连接测量）；它会按地址批次查询 pool、tracked wallet、active group 三类来源。该成本换取所有入口一致且真实的资格信息，没有保留“更快但恒零”的 False 路径。

## 测试证据

- `python -m pytest backend/tests/test_kelly_fee_regime.py backend/tests/test_traders_firehose_provenance.py -q`：新增回归测试通过。
- `python -m pytest backend/tests/test_backtest_engine.py backend/tests/test_backtest_settlement.py backend/tests/test_backtest_crypto_on_event_dispatch.py backend/tests/test_crypto_strategy_safety_gates.py backend/tests/test_strategy_threshold_single_source.py backend/tests/test_trader_data_access_and_strategy_sdk.py backend/tests/test_traders_firehose_provenance.py backend/tests/test_kelly_fee_regime.py -q`：`102 passed, 1 warning`；warning 是既有 `services/scanner.py:60` 的 event-loop deprecation。
- `git diff --check`：通过，仅有 Git 的 LF→CRLF 工作区提示，无空白错误。
- `rg -n "include_source_context\\s*=\\s*False" backend/services --glob "*.py"`：无生产调用残留。
- `rg -n "legacy_quartic" backend --glob "*.py"`：仅 canonical 定义和回归测试。
- `python -m ruff check ...`：未执行，当前 Python 环境没有安装 `ruff`（`No module named ruff`）。

## 未验证项

- 未运行整个 `backend/tests/` 全量套件；本次运行的是所有新增测试及本批次直接受影响的 102 项测试。
- 未用真实 Polymarket/Kalshi 订单验证平台最终扣费；数字来自 2026-08-12 实施当日官方文档。
- 未构建/重启 Docker 服务；当前运行容器仍是此前镜像，本批次只完成源码、测试、提交与推送流程。
- 未把 Polymarket 市场对象的动态 `feeSchedule` 接到订单/市场 DTO；显式 `fee_rate` 参数已经预留官方 schedule 覆盖入口，但该数据接线不在 fix-03 spec 点名范围。
