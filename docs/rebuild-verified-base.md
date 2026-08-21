# 重建后的可信基座

分支：`claude/rebuild-verified-base`，从 `389d246`（与 `main` 的 merge-base）重建。

## 为什么重建

`claude/batch-2-identity-finality-review-jvcsc5` 上有 48 个提交、79 个文件、
+7447/−2702。里面混着三类东西：经过验证的真修复、审计判定为无效或错误的改动、
以及我自己在写 spec 过程中引入的回归。在准备上实盘的基座里，
**每一处相对干净源码的行为差异都必须说得出为什么在那儿**。旧分支做不到这一点。

重建不是重写 7447 行。48 个提交里**只有 17 个是代码提交**，其余全是文档。
17 个里 12 个是干净的、可直接搬运的，4 个需要修正，1 个丢弃。

## 结果

| 原提交 | 内容 | 处置 | 依据 |
|---|---|---|---|
| `dbf0c5c` | fix-01 影子结算单一真相源 | 搬运 | 干净基座的立命之本，审计通过 |
| `f455129` | fix-02 策略层 P0 门/阈值一致性 | 搬运 | 含 copy-trade EV 公式改动，**保留**（见下） |
| `02e594a` | 允许 ML capability 导入 | 搬运 | |
| `8716e76` | 前端实时事件批处理 | 搬运 | 纯增量 |
| `30fd136` | markout / 逆向选择核心 | 搬运 | 纯新模块 |
| `78cede8` | fix-03 2026 费率制 + trader 溯源 | 搬运 | 费率公式客观正确；旧曲线下调过的阈值全部作废 |
| `3333581` | F-1 copy-trade 默认值与实盘门 | 搬运 + 修正 | 引入了 `params = self.config` 回归，由 `6398fce` 修正 |
| `6cbae42` | F-2 trader confluence 单遍加权 | 搬运 | |
| `5bb6ef0` | F-3 news edge 统一 | 搬运 | |
| `b13d2cb` | F-4 news momentum 阈值与退出 | 搬运 | |
| `be0cd53` | F-5 侧向感知的 certainty shock | 搬运 | |
| `dd485e5` | F-2 删除死的 medium tier 权重 | 搬运 | 死代码删除 |
| `7c8c825` | P-1 手动买入走影子成交路径 | 搬运 | 用户实测症状，已复现修复 |
| `81324f0` | P-2 影子模式脱离实盘凭证 | 搬运 | 代码正确；**其根因陈述与测试命名有误**，见"遗留" |
| `e88e450` | P-3 保留 crypto 生产者来源 | 搬运 | |
| `c18b441` | fix-07 交易面预热 traders WS 定价 | 搬运 + 两处修正 | 见下 |
| `97d8f96` | G-0 共享 crypto 概率估计器 | 搬运 | 新模块 |
| `6ae51ab` | G-1 方向性 edge 概率化 | 搬运 | **注意单位变了**：从"资产波动%"改为"概率点"，`min_edge_percent` 需要重新选值 |
| `8f53c6b` | G-2 convergence 近到期语义重做 | **丢弃** | 见下 |
| `8cac370` | G-3 maker quote 库存经济学 | 搬运 | |
| `732eff0` | G-4 小型 crypto 策略经济学 | 搬运 | |
| `58890f1` | 部署 detection/reconciliation/services/jobs 面 | 搬运 | 实测：market 源 0 → 525 条信号 |
| `c95d6c9` | CI：worker 面无进程即构建失败 | 搬运 | |
| `68a79c8` | trader 模板端点修复 + 删除死 seeder | 搬运 | |

三个回归修复（`d0653e6` / `6398fce` / `5d3fd09`）保留为独立提交而非合并进原提交。
它们记录的是真实陷阱（共享单例 vs 每 trader 参数；异构批次里 `break` 与 `continue`
的区别；一个被全仓库其他调用点刻意后台化的 helper 被同步 await 到热路径上），
合进去等于把这些说明删掉。基座的**最终代码状态**两种做法完全一致。

## 丢弃 G-2 的理由

G-2 改写了 `btc_eth_convergence.py` 659 行，并给 convergence 声明了 20 个配置键。
它不是 bug 修复，是**未经请求的策略语义重设计**，没有任何数据验证过新语义。
丢弃后声明与读取一并消失，状态自洽（不会留下悬空的死配置键）。

这与 `fix-10-forward-plan.md` 里"等漏斗数据再决定"是一致的，不是矛盾：
**正因为还没决定，它就不该被烘焙进基座**。G-2 完整保留在
`claude/batch-2-identity-finality-review-jvcsc5` 上，随时可以 cherry-pick
`8f53c6b` 作为一次独立评审的改动重新引入。

## 保留 fix-02 EV 公式的理由（改判）

我一度计划回滚它。不该回滚 —— 回滚会换回一个**更糟**的编造：

- 旧公式：`edge = |0.5 − p| × 200`，`payout = min(1, p + max(0.01, edge/100))`。
  任何 p ≥ 0.667 的合约都被断言为 **100% 必赢**，且每一条信号都声称有正利润。
- fix-02 公式：`edge = (confidence − p)/p`，`payout = confidence`。
  p > confidence 时 edge 归零。

两者都是编造，因为 `confidence` 在唯一的生产者
（`traders_copy_trade_signal_service.py:554`）里是**硬编码常量 0.70**。
但在一个要上实盘的系统里，"少报 edge"比"声称必赢"安全得多。
真正的修复在上游（钱包 skill 先验怎么定），属策略层，不在本基座范围内。

## 遗留（明确记录，不假装已解决）

1. **copy-trade `confidence` 硬编码 0.70** —— 归策略会话。
2. **P-2 的根因陈述有误**，`test_shadow_wallet_freshness_gates.py` 的命名会误导
   后续读者以为问题出在新鲜度门上。代码本身正确，待重命名。
3. **G-1 改变了 `min_edge_percent` 的单位** —— 沿用旧值等于换了一把尺子还按老刻度读。
4. **`min_seconds_to_resolution` 10 → 60**、**`max_risk_score` 0.70 → 0.60** 等
   默认值改动随 F/G 系列一并带入。注意这些是 `default_config`，
   而 `ensure_system_opportunity_strategies_seeded` 从不覆盖已存在的
   `strategies` 行 —— **对已部署的实例它们不生效**，只影响全新安装。
5. **GitHub Actions 在仓库层面是关闭的**（`list_workflows` 返回 0）。
   `ci.yml` 一直在 `main` 上但从未执行过，所以本分支带来的 worker-plane 守卫
   目前不提供任何保护。需要在仓库 Settings → Actions 启用。

## 下一步

基座就位后，按 [`fix-10-forward-plan.md`](./fix-10-forward-plan.md) 推进：
fix-10 信号漏斗归因（先做、独立）、fix-11 体育源生产者、fix-12 news 静默失败。
