# 实跑诊断:模拟账户零订单(2026-08-12)

> 环境:操作员本机 shadow 模式 + 模拟账户,运行代码为 `689095e`(含 fix-01~fix-04,已验证容器内符号)。
> 数据来源:运行库直查 + worker 日志。所有结论都有证据,推测项已标注。

## 现象

1. 机会里的交易员机会,手动买入**不扣款、不产生订单**
2. 体育与天气机会**一直为 0**
3. 系统运行数小时,**一个模拟订单也没有**

## 实测数据

| 表 | 结果 |
|---|---|
| `trader_decisions` | skipped **80918** / blocked **6308** / failed **2282** / **selected 0** |
| `trader_orders` | 仅 6 笔:`shadow/submitted/manual` ×5、`shadow/closed_loss/manual` ×1;**自动订单 0** |
| `simulation_accounts` | `current_capital = 10000.00`(=初始)、`total_pnl = 0`、`total_trades = 0` |
| `trade_signals` | traders 4609 skipped / crypto 352 expired / weather 109 expired;**无 sports 源** |

拒绝原因 TOP(`trader_decisions.reason`):

| 原因 | 次数 |
|---|---|
| `Orderflow alignment: orderflow imbalance unavailable`(合计各组合) | ~37,000 |
| `Signal must originate from crypto_worker; got source='crypto' signal_type='crypto_opportunity'` | 4,226 |
| `Edge persistence: elapsed_ms=0 required_ms=250` | 4,305 |
| `Edge threshold: min=0.35`(真实阈值拒绝) | ~7,000 |
| `Strict WS pricing required: source=unknown age_ms=unknown`(traders_confluence 全 blocked) | ~4,200 |
| `copy_trade_gate_failed:min_notional / entry_drift / max_age` | 2,291 |

## 根因(症状 3):缺少 Polymarket 凭证 → 行情管道全断 → 多道闸 fail-close

`worker-trading` 日志逐秒复现:

```
ERROR services.live_execution_service: Missing Polymarket API credentials. Cannot initialize trading.
WARNING trader_reconciliation_worker: WalletStateCache reseeder skipped:
        live_execution_service not ready and cache has no pinned wallet;
        the freshness gate will keep refusing trades.
        Last init error: missing_polymarket_credentials
```

因果链:

```
.env 缺 Polymarket API 凭证
 → live_execution_service 初始化失败(持续重试)
 → WalletStateCache degraded(cycle 265+ 全失败,seed_count=0)
 → 行情/WS 喂价不可得
     ├─ "Strict WS pricing: source=unknown"      → traders_confluence 全 blocked
     ├─ "orderflow imbalance unavailable"        → btc_eth_convergence / maker_quote / directional 全 skipped
     └─ "Edge persistence: elapsed_ms=0"         → 计时状态无数据
 → selected = 0 → 零自动订单
```

日志原文 "the freshness gate will keep refusing trades" 即为此现象的自述。

**⚠️ 附带的设计缺陷**:运行的是 **shadow 模拟账户**,却因缺少**实盘下单凭证**而完全无法产出模拟订单。shadow 只需要行情数据,不需要下单能力;当前实现把二者耦合在 `live_execution_service` 上,导致"没有实盘密钥 = 连模拟盘也跑不了"。见 fix-06 P-2。

### 相关闸门代码

- `btc_eth_directional_edge.py:1684`:`allow_missing_orderflow_alignment` 默认 **False** → 数据缺失即判负。`orderflow_alignment_modes` 默认 `{"maker_quote","convergence"}`,与实测 skipped 榜首(convergence 27852、maker_quote 27655)吻合。同段逻辑在 `btc_eth_convergence.py:1661`、`btc_eth_maker_quote.py:1659` 各复制一份(fix-05 WO-B0 计划统一)。
- `btc_eth_directional_edge.py:4583-4607`:`source_origin_predicate` 要求 `source=="crypto"` 且(`payload.strategy_origin=="crypto_worker"` 或 `signal_type` 以 `crypto_worker` 开头)。

## 根因(症状 1):手动买入不接模拟账本 —— 真 bug

`api/routes_traders.py:2245 manual_buy`:
- shadow 分支**只写一行 `TraderOrder`**(status=`submitted`,`executed_at=None`);
- **完全不触碰 `SimulationAccount`** → 不扣款;
- `place_order` 仅在 `if mode == "live"` 分支 → **shadow 无任何模拟成交**,订单永远停在 `submitted`。

证据吻合:5 笔 manual 单卡在 `submitted`,模拟账户余额与成交数均为初始值。

**已核实**:干净基座 `389d246` 的同一函数同样不接 `SimulationAccount`,**非 fix-01 引入**。fix-01 删除的是自动路径上"只扣不还"的半成品镜像;手动路径从来就没有接过模拟账本。

## 根因(症状 2):体育无信号,天气被同批闸拦

- `trade_signals` 中**不存在 sports 源**(仅 traders/crypto/weather/scanner)⇒ 体育是**信号未产生**,不是被闸拦。sports 归 trading plane(`_PLANE_CONFIGS["trading"].strategy_source_keys` 含 `"sports"`),该 plane 在运行。具体为何无产出**未定位**,需单独排查数据源。
- 天气**有信号**(109 expired / 62 pending / 24 skipped)但 `weather_distribution` 366 blocked ⇒ 与症状 3 同一批 fail-close 闸门相关。

## 次要缺陷:`strategy_origin` 漏打标(4,226 条)

生产者**确实**在多处打标(`btc_eth_directional_edge.py:4422/4478`、`btc_eth_convergence.py:4389/4445`、`btc_eth_maker_quote.py:4387/4443`),`position_lifecycle.py:1581` 另有兜底补写。但失败样本的 `signal_type='crypto_opportunity'` 表明**存在另一条生产路径未打标且不匹配前缀** ⇒ 生产者/消费者口径缺口,占比约 5%,非主因。

## 处置

| 项 | 处置 | 归属 |
|---|---|---|
| 缺 Polymarket 凭证 | 填 `.env`(`POLYMARKET_API_KEY/_SECRET/_PASSPHRASE/_PRIVATE_KEY/_FUNDER`)后重启 | **操作员** |
| shadow 依赖实盘凭证 | fix-06 P-2 | 代码 |
| 手动买入不接模拟账本 | fix-06 P-1 | 代码 |
| `strategy_origin` 漏打标 | fix-06 P-3 | 代码 |
| 体育无信号 | 待排查(数据源层) | 后续 |

**注意**:填凭证后症状 2/3 预计大幅缓解,但 fix-06 的三项是独立的真实缺陷,与凭证无关。
