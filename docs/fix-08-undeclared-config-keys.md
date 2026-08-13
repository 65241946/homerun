# 修复 spec:fix-08 未声明的隐藏配置键(微型批次)

> 基座:分支 `claude/batch-2-identity-finality-review-jvcsc5`(含 fix-01~fix-07)
> 来源:fix-05 审查发现。**这几处是基座既有缺陷,非 fix-05 引入** —— fix-05 未触碰它们,但也未达到 G-1 的"`directional_*` 全 key grep ≥2"验收标准。
> 规模:小。适合作为独立小批次或搭车下一批。

---

## 问题

`backend/services/strategies/btc_eth_directional_edge.py` 中有 4 个配置键**只以 `params.get(key, 魔法数)` 形式存在,从未在 `default_config` 声明**,因此:
- UI / 策略参数编辑器里看不到、无法调;
- 违反施工总则"新增 key 必须同时进 `default_config` + schema(带 description)";
- 违反"单一真值源"(inline 回退是裸魔法数,没有可对照的声明值)。

| 键 | 出现位置 | 当前 inline 回退 |
|---|---|---|
| `directional_min_entry_price_floor` | `:2483` | `0.25` |
| `directional_max_entry_price_ceiling` | `:2506` | `0.99` |
| `directional_max_entry_price_ceiling_buy_yes` | `:2534` | 回退到 `directional_entry_price_ceiling` |
| `directional_max_entry_price_ceiling_buy_no` | `:2543` | 回退到 `directional_entry_price_ceiling` |

**已核实**:这 4 处在 `d0b1f47`(fix-05 之前)的 `:2272/:2295/:2323/:2332` 一模一样 —— 基座既有,fix-05 没有让情况变坏。

## Your task

1. 把这 4 个键加入 `btc_eth_directional_edge.py` 的 `default_config`,**取值与当前 inline 回退完全一致**(`0.25` / `0.99` / 另两个沿用"缺省回退到通用 ceiling"的语义)。
   - ⚠️ **这是纯粹的透明化,不是调参**:不得借机改变任何生效数值。若你认为某个值不合理,写进产出文件供操作员决策,**不要自行改动**。
2. 把 inline 回退改为读 `default_config`(与 fix-02 P0-4、fix-05 G-1 中 `directional_{phase}_min_edge` 的写法一致):
   ```python
   params.get("directional_min_entry_price_floor", cls.default_config["directional_min_entry_price_floor"])
   ```
   两个 `buy_yes`/`buy_no` ceiling 保持"未配置时回退到通用 ceiling"的语义,但通用 ceiling 本身改读 `default_config`。
3. 把 4 个键加入该策略的 config schema(带 description,说明是入场价格上下限、以及 buy_yes/buy_no 覆盖通用值的语义)。schema 位置参照同文件既有 `directional_*` 键的写法;若 schema 在 `opportunity_strategy_catalog.py`,则加在对应策略的 `param_fields`。
4. **顺带核实**:用同样方法扫一遍本文件其它 `params.get("...", <魔法数>)`,看是否还有未声明的隐藏键。**发现的都列进产出文件**(修不修由本 spec 第 1-3 条同样处理,但若数量超过 5 个,停下来先报告,不要无限扩大范围)。

## 回归测试

- 4 个键出现在 `default_config` 且值等于修改前的 inline 回退(**行为不变**是核心判据)。
- 显式传入 `params` 覆盖时,新值生效(证明单一真值源接线正确)。
- 不传时,取 `default_config` 值,且与修改前的实际生效值**逐一相等**(可用数值表断言)。
- `buy_yes`/`buy_no` 未配置时仍回退到通用 ceiling。

## 完成判据

- `directional_*` 全 key grep ≥2(G-1 的原验收标准达成)。
- 无任何生效数值变化(产出文件用数值表证明)。
- 第 4 条的扫描结果写入 `.piercode/fix-08-last-message.md`。
- 在 `claude/batch-2-identity-finality-review-jvcsc5` 分支提交推送;push 前 `git pull --rebase`。
