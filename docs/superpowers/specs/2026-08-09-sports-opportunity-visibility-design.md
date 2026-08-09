# 体育机会分类与运行状态可见性设计

## 1. 目标

修复体育机会页面和数量徽标把真实体育机会错误显示为 0 的问题，并让扫描器状态正确展示已经加载的体育策略。

本次修改只作用于只读查询和诊断状态，不改变机会生成、信号选择、模拟成交、实盘下单、仓位管理、资金账本和风险控制。

## 2. 已确认事实

- 体育页面和顶部数量徽标都调用 `GET /api/opportunities?category=sports`。
- 当前后端对 `category` 使用严格字符串相等判断。
- 实际体育机会可能使用 `WNBA`、`MLB` 等联赛分类，也可能没有顶层分类但市场包含 `sports_market_type` 或 `game_start_time`。
- 当前运行态存在一条分类为 `WNBA` 的机会，但严格查询 `category=sports` 返回 0。
- `sports_overreaction_fader` 已由 detection worker 加载并订阅市场刷新事件；其原始阈值当前没有产生机会。
- 扫描器状态构造只枚举 `source_key=scanner`，因此漏掉了已经加载的 `source_key=sports` 策略。

## 3. 方案选择

采用后端统一分类匹配方案，不修改前端查询参数，也不改写已存储机会的原始分类。

未采用的方案：

- 前端只按 `strategy=sports_overreaction_fader` 查询：会漏掉其他策略识别出的体育机会。
- 在机会生成阶段强制把分类改成 `sports`：会丢失 `WNBA`、`MLB` 等原始分类信息，并进入交易信号数据链，风险边界过大。
- 放宽体育策略阈值：会改变交易行为，不能用于修复显示问题。

## 4. 设计

### 4.1 体育分类匹配

在 API 查询层增加一个纯函数，用于判断机会载荷是否属于请求分类。

非体育分类继续保持现有的大小写不敏感精确匹配。只有请求分类为 `sports` 时增加以下只读识别规则，满足任一项即视为体育机会：

1. 原始机会分类为 `sports` 或明确的体育联赛/项目分类，例如 `NBA`、`WNBA`、`NFL`、`MLB`、`NHL`、`ATP`、`WTA`、足球、篮球、棒球、冰球、网球、综合格斗、电子竞技等；
2. 策略为 `sports_overreaction_fader`；
3. 任一关联市场具有非空 `sports_market_type`；
4. 任一关联市场具有非空 `game_start_time`。

该匹配函数同时用于机会列表、机会 ID 和机会数量统计所依赖的统一过滤路径，确保页面内容和数量一致。

不使用模糊子串扫描整段标题，避免 `ATP`、`F1` 等短词误匹配普通文本。

### 4.2 体育策略状态

扫描器运行状态枚举范围从仅 `scanner` 扩展为现有的市场刷新策略来源集合 `{"scanner", "sports"}`。

该修改只改变 `/api/scanner/status` 的策略和诊断列表，不改变 event dispatcher 的订阅、策略执行顺序、扫描频率或机会合并行为。

### 4.3 明确不修改的内容

- `sports_overreaction_fader` 源码和配置；
- `min_move_pct=5.0`、`move_window_seconds=300`、流动性、价差、结算时间等阈值；
- Quality Filter、TradeSignal、Trader Orchestrator、Order Manager；
- 模拟和实盘仓位、余额、PnL、费用计算；
- 数据库中的原始 `category` 字段；
- 前端刷新频率和 WebSocket 行为。

## 5. 测试设计

先写失败测试，再写实现。

1. `WNBA` 分类能够匹配 `sports`；
2. 顶层分类为空但 `sports_market_type=moneyline` 的机会能够匹配 `sports`；
3. 顶层分类为空但存在 `game_start_time` 的机会能够匹配 `sports`；
4. 普通政治或经济机会不能匹配 `sports`；
5. 非体育分类仍保持精确匹配；
6. 扫描器状态包含已加载的 `source_key=sports` 策略；
7. 现有 scanner、机会过滤和路由测试继续通过。

部署后进行只读验收：

- `/api/opportunities?category=sports` 至少能返回当前已存在的 WNBA 机会；
- 体育页数量与接口总数一致；
- `/api/scanner/status` 能看到 `sports_overreaction_fader` 为 loaded；
- 体育专用策略的机会数仍由原策略真实触发，允许为 0；
- 模拟账户、持仓、交易记录和风控设置在部署前后不发生配置变化。

## 6. 风险与回滚

主要风险是体育分类识别范围过宽。通过只使用结构化分类、策略标识和上游体育字段控制范围，并用负例测试防止误归类。

若验收发现分类错误，只需回滚 API 分类匹配函数和扫描器状态枚举两处代码；数据库、策略配置和交易记录无需迁移或恢复。
