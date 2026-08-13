# fix-03 审查:费用制度 + 交易员 provenance

> 审查对象:commit `78cede8`。审查方在干净容器独立核对 diff、亲跑测试、数值验证,非照抄实现方自述。

## 结论:通过

两项地基级 P0 均改对,回归测试证明有效。

## P0-a 费用曲线

实现正确:二次曲线 `feeRate × p × (1−p)`;类目表;显式 `fee_rate` 优先于类目;**未知/缺失类目取最保守 `DEFAULT_FEE_RATE=0.07`(不落 0)**;maker 显式零费;旧四次曲线保留为 `polymarket_taker_fee_legacy_quartic`,生产代码零调用(已 grep 确认)。

审查方实测数值(fee 占价格百分比):

| p | crypto(0.07) | sports(0.05) | legacy 四次 |
|---|---|---|---|
| 0.10 | 6.30% | 4.50% | 0.20% |
| 0.30 | 4.90% | 3.50% | 1.10% |
| 0.50 | 3.50% | 2.50% | 1.56% |
| 0.90 | 0.70% | 0.50% | 0.20% |
| 0.99 | 0.070% | 0.050% | 0.002% |

**一个曾被怀疑、现已澄清的点**:被删的旧 docstring 声称"线性 `p(1−p)·rate` 形状在 p=0.30 过收 ~4×、p=0.10 过收 >20×,导致费用感知策略拒掉可盈利交易",而新公式正是那个形状。实现方核对官方文档确认 `C × feeRate × p × (1−p)` **就是现行公式**,故尾部变贵是 2026 制度的真实变化,旧四次曲线才是过时的;该历史注释已不适用。

实现方超出 spec 做对的两件事:补齐官方全部类目(finance/economics/culture/weather/tech/mentions…,本 spec 只列了 4 个);明确记录 Kalshi 的向上取整与 series multiplier **未**混入 Polymarket 实现。

## P0-b 交易员 provenance

删除两处 `include_source_context=False`(`traders_firehose_pipeline`、`traders_confluence.detect_async`);区分"缺 provenance"(新拒绝原因 `missing_source_provenance`)与"显式不合格";三处死缺省(`min_tier`、`firehose_exclude_crypto_markets`、`min_confluence_strength`)统一读 `DEFAULT_CONFIG`。

## 测试有效性(最硬证据)

- 修复后:`test_kelly_fee_regime` + `test_traders_firehose_provenance` = **19 passed**(审查方独立复现)。
- 把 provenance 代码退回修复前、保留新测试:**5 failed**,其中包括端到端的 `test_strategy_filtered_pipeline_returns_qualified_rows` → 证明"回测/UI 侧恒返回 0 机会"确实已修复。

## 测试夹具改动核查(逐行)

费用上调后 4 处既有夹具被调整,审查方逐行核对,**均为提高输入信号强度、未放宽生产阈值,断言本身未改**:
- `test_crypto_strategy_safety_gates.py`:`move_5m_percent 6.0→10.0`;补 `oracle_price/binance_direct` 一致值。
- `test_strategy_threshold_single_source.py`:CTF 两腿 `bid 0.515→0.522`(新费率下需更高边际才有真实套利)。

## 遗留 / P1

1. **provenance 注入成本**:实测约 161 ms/次(冷连接;按地址批量查 pool/tracked/group 三源)。选择了"一致但慢",未保留"快但恒零"的死路径。若高频路径成为瓶颈,属 P1 优化。
2. **动态 feeSchedule 未接线**:Polymarket 市场对象提供 `feeSchedule`,当前仅预留了显式 `fee_rate` 覆盖入口,未接到市场/订单 DTO。属 P1。
3. **⚠️ 对策略调优的连带影响(必须周知)**:费后 EV 闸门现在显著变严(中间价位费用约 ×2.2,尾部更多)。原先"能过闸"的策略可能大量被拒——这不是回归,而是此前系统性高估 edge 的纠正。**任何基于旧费率的阈值调优结论作废,P1 调参必须基于新费率重做。**
