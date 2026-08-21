# 批次 2:身份与终局判定审查

审查对象:`65241946/homerun` @ `389d246`(容器 clone)
基准口径:Python 3.12(与 CI `.github/workflows/ci.yml:66` 一致),2555 tests collected,0 collection errors

> 口径说明:本批次未使用 `homerun-pmr/.piercode/audit-01-baseline.md` 的切片定义(该文件位于本机工作区外)。基线中的 83/73 在本 clone 的任何计数口径下均无对应值,须以基线文件回填校准。

---

## 一、身份(identity)

### F-2.1 `condition_id` 全库无大小写归一,但 `token_id` 有 —— 归一策略不对称

`condition_id` 是 0x 十六进制标识,全库 20 余处构造点一律只做 `strip()`,无一处 `lower()`:

- `services/wallet_state_cache.py:555`、`613`
- `services/trader_orchestrator/position_lifecycle.py:3355`、`3979`、`4100`
- `services/trader_orchestrator_state.py:735`
- `services/scanner.py:666`、`689`
- `services/news/workflow_orchestrator.py:1475`
- `services/strategies/ctf_basic_arb.py:93`

而 `services/intent_runtime.py:396` 是全库唯一做小写归一的标识入口:

```python
token_id = str(value or "").strip().lower()
```

**后果**:`condition_id` 被直接用作字典键 —— `services/wallet_state_cache.py:226` 的
`self._condition_to_token_ids: dict[str, set[str]]`,写入点在 `577`、`586`、`621`、`845`、`850`,
读取点在 `939`(`self._condition_to_token_ids.get(pos.condition_id)`)。
上游任一端点返回混合大小写的 hex(EIP-55 checksum 形式),同一市场即分裂为两个键:
仓位归集失败,`939-943` 的清理分支拿不到 condition_set,索引泄漏。

`token_id` 侧则相反:`intent_runtime` 存小写键,`wallet_state_cache` 存原样键,两侧键空间不一致。

### F-2.2 `_is_hex_id` 前缀判定大小写敏感

`services/polymarket.py:1754`:

```python
def _is_hex_id(value: str) -> bool:
    """Check if a value looks like a hex condition_id (0x-prefixed)."""
    return value.startswith("0x")
```

`0X` 前缀漏判。该函数在 `1762`、`1766`、`1787`、`1789` 决定走 condition_id 查询分支还是
token_id 查询分支 —— 漏判直接导致错误的上游查询路径,并在 `1787-1790` 把 `resolved` 误判为 `False`,
使已归集的市场被当作未解析重新查询。

---

## 二、终局判定(finality)

### F-2.3 三套终局词表并存,其中两套各自重复定义

| 词表 | 定义位置 | 取值 |
|---|---|---|
| 持仓终局 | `services/trader_orchestrator_state.py:11136`(内联 tuple)<br>`services/polymarket_trade_verifier.py:78` `_RESOLVED_STATUSES` | `resolved`, `resolved_win`, `resolved_loss`, `closed_win`, `closed_loss`, `win`, `loss` |
| 订单终局 | `services/trader_orchestrator_state.py:124` `PENDING_LIVE_EXIT_TERMINAL_STATUSES` | `filled`, `superseded_resolution`, `superseded_external`, `cancelled` |
| 信号终局 | `services/intent_runtime.py:58` `_SIGNAL_TERMINAL_STATUSES`<br>`services/signal_bus.py:43` `SIGNAL_TERMINAL_STATUSES` | `executed`, `skipped`, `expired`, `failed` |

持仓终局的两份副本目前逐字相同,信号终局的两份副本目前逐字相同 —— **当前无行为差异**,
属潜伏分叉:任一处增删状态而另一处未同步,终局判定即静默不一致。

这直接违反 `agents.md` 原则 2(「Clean cut, not backwards compatible … 重复实现和死分支是金融 bug 的藏身处」)。
持仓终局那份还是内联字面量而非具名常量,`11136` 的 tuple 无法被引用复用。

### F-2.4 `redeemable` 字段缺失时乐观默认为 True

`services/wallet_state_cache.py:623`:

```python
pos.is_resolved = True
pos.redeemable = bool(raw.get("redeemable", True))
```

上游 `closed_positions` 条目若不带 `redeemable` 字段,系统假定可赎回。
终局标志位上的乐观默认应改为悲观(`False`)或三态(`None` = 未知),
由显式信号驱动而非缺省驱动。

### F-2.5 陈旧仓位清理跳过零尺寸仓位

`services/wallet_state_cache.py:591-593`:

```python
stale = [
    tid for tid, pos in self._positions.items()
    if tid not in seen_token_ids and not pos.is_resolved and pos.size > 0.0
]
```

`pos.size == 0.0` 且未标记 resolved 的仓位既不会被 seed 更新也不会被清理,永久滞留在 `_positions` 中。

---

## 三、更正

前一轮口头提到「`market_resolutions` 表缺唯一约束、存在重复写入终局记录的口子」—— 该结论作废。
`models/database.py:2318` 的表实为 `resolution_analyses`,存的是 LLM 对市场**解析规则**的
清晰度/风险评分与建议(`clarity_score`、`ambiguities`、`recommendation`),属研究辅助数据,
不是结算终局记录,不构成终局判定权威。缺唯一约束在该语义下不是缺陷。

---

## 四、待回填

`homerun-pmr/.piercode/audit-01-baseline.md` 中 83/73 的口径定义未获取,本批次未与批次 1 基线对齐。
另需确认本 clone(`389d246`)与本机 `homerun-pmr` 工作区是否同一代码状态。
