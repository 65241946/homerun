# 运行链路修复实施计划

> 当前工作由主任务内联执行。每项先写失败测试，再做最小生产修改。

## 任务 1：锁定新闻和体育回归

**文件：**
- 修改 `backend/tests/test_news_feed_quality_guards.py`
- 修改 `backend/tests/test_scanner_pipeline.py`
- 修改 `backend/tests/test_workers_host.py`

**步骤：**
1. 增加“旧发布时间、当前抓取时间仍应返回”的新闻测试。
2. 增加 sports source 进入扫描器分区的测试。
3. 增加 detection plane 加载 sports 策略桶的测试。
4. 运行这些测试并确认原代码失败。

## 任务 2：修复新闻和体育链路

**文件：**
- 修改 `backend/services/news/feed_service.py`
- 修改 `backend/services/scanner.py`
- 修改 `backend/workers/host.py`

**步骤：**
1. 删除把文章发布时间误当抓取时间的过滤。
2. detection plane 加载 `sports`。
3. 市场刷新分区接受 `scanner`/`sports`。
4. 运行任务 1 测试。

## 任务 3：锁定并修复加密配置恢复和 ML 导入

**文件：**
- 修改 `backend/tests/test_market_runtime_crypto_lane_toggle.py`
- 新增或修改策略加载器测试
- 修改 `backend/services/crypto_service.py`
- 修改 `backend/services/market_runtime.py`
- 修改 `backend/services/strategy_loader.py`

**步骤：**
1. 增加“运行时按 10 秒节流重载设置”的测试。
2. 增加 `services.ml` 是合法项目模块、危险模块仍被拒绝的测试。
3. 在正式运行循环中节流调用 `apply_search_filters()`，不在每秒热路径重复访问数据库。
4. 精确允许 `services.ml`。
5. 运行聚焦测试。

## 任务 4：锁定并修复模拟账本落库和 shadow 展示

**文件：**
- 修改 `backend/tests/test_trader_orchestrator_shadow_backfill.py`
- 修改 `backend/workers/trader_orchestrator_worker.py`
- 修改 `frontend/src/components/PositionsPanel.tsx`

**步骤：**
1. 把回填测试改为关闭原 session 后从新 session 读取，确认原代码回滚失败。
2. 回填成功后提交包含 simulation ledger 的事务。
3. 前端兜底接受 `shadow`，兼容旧 `paper`/`simulated`。
4. 运行后端回归测试和前端构建。

## 任务 5：整体验证和运行态验收

**步骤：**
1. 运行全部本次聚焦测试。
2. 运行相关后端测试组与 `frontend` build。
3. 检查 git diff，确认无阈值、仓位或实盘权限改动。
4. 从当前源码重建受影响容器并重启。
5. 核验容器健康、worker 加载日志、机会计数、simulation ledger/position 数据。
6. 给出已修复、仍受策略门槛限制和未验证项的分层结论。
