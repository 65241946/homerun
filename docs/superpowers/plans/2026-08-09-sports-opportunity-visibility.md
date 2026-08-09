# Sports Opportunity Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让体育页和体育数量正确包含结构化体育机会，并让扫描器状态显示已加载的体育策略，同时保持交易策略、信号、风控和执行行为完全不变。

**Architecture:** 在 `api/routes.py` 的统一只读过滤路径增加体育分类纯函数，保留非体育分类的精确匹配；在 `services/scanner.py` 复用既有市场刷新来源集合生成状态行。前端查询、机会原始分类和交易执行链均不修改。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、pytest、Docker Compose、PowerShell。

---

### Task 1: 恢复相关扫描器测试的正确收集边界

**Files:**
- Modify: `backend/tests/test_scanner_pipeline.py:150-220`

- [ ] **Step 1: 证明现有嵌套测试没有被 pytest 收集**

Run from `backend`:

```powershell
uv run pytest tests/test_scanner_pipeline.py --collect-only -q | Select-String 'test_polymarket_active_filter_requires_condition_and_clob_token'
```

Expected: no matching collected test, because the method is currently nested inside `test_scanner_worker_loads_and_refreshes_market_strategy_sources`.

- [ ] **Step 2: 恢复测试结构，不改生产代码**

把 `test_polymarket_active_filter_requires_condition_and_clob_token` 保持为 `TestScannerInit` 的方法，并把顶层的 worker 来源测试移到该类结束之后：

```python
    def test_polymarket_active_filter_requires_condition_and_clob_token(self):
        ...


def test_scanner_worker_loads_and_refreshes_market_strategy_sources():
    from workers import scanner_worker

    assert scanner_worker._MARKET_REFRESH_STRATEGY_SOURCE_KEYS == ("scanner", "sports")
```

- [ ] **Step 3: 验证测试重新被收集**

Run from `backend`:

```powershell
uv run pytest tests/test_scanner_pipeline.py --collect-only -q | Select-String 'test_polymarket_active_filter_requires_condition_and_clob_token'
```

Expected: exactly one collected test path contains that name.

---

### Task 2: 用失败测试定义体育分类语义

**Files:**
- Modify: `backend/tests/test_opportunity_subfilters.py`
- Modify: `backend/api/routes.py:220-560`

- [ ] **Step 1: 写体育分类纯函数的失败测试**

在 `test_opportunity_subfilters.py` 中通过模块访问尚不存在的函数，避免导入阶段失败：

```python
from api import routes


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"category": "WNBA", "strategy": "stat_arb", "markets": []}, True),
        ({"category": None, "strategy": "sports_overreaction_fader", "markets": []}, True),
        ({"category": None, "strategy": "stat_arb", "markets": [{"sports_market_type": "moneyline"}]}, True),
        ({"category": "Weather", "strategy": "stat_arb", "markets": [{"game_start_time": "2026-08-10T01:00:00Z"}]}, False),
        ({"category": "Politics", "strategy": "stat_arb", "markets": [{}]}, False),
    ],
)
def test_payload_matches_sports_category(payload, expected):
    assert routes._payload_matches_category(payload, "sports") is expected


def test_payload_non_sports_category_remains_exact():
    assert routes._payload_matches_category({"category": "Economy"}, "economy") is True
    assert routes._payload_matches_category({"category": "Fed"}, "economy") is False
```

- [ ] **Step 2: 运行测试并确认 RED**

Run from `backend`:

```powershell
uv run pytest tests/test_opportunity_subfilters.py -q
```

Expected: FAIL with `AttributeError: module 'api.routes' has no attribute '_payload_matches_category'`.

- [ ] **Step 3: 实现最小体育分类纯函数**

在 `api/routes.py` 的参数辅助函数附近增加：

```python
_SPORTS_CATEGORY_ALIASES = frozenset(
    {
        "sports", "nba", "wnba", "nfl", "mlb", "nhl", "ncaa", "ncaab", "ncaaf",
        "soccer", "football", "basketball", "baseball", "hockey", "tennis", "atp", "wta",
        "ufc", "mma", "boxing", "golf", "cricket", "rugby", "formula 1", "f1", "esports",
    }
)
_SPORTS_STRATEGY_SLUGS = frozenset({"sports_overreaction_fader"})


def _payload_matches_category(payload: Mapping[str, Any], requested_category: str) -> bool:
    requested = str(requested_category or "").strip().lower()
    actual = str(payload.get("category") or "").strip().lower()
    if requested != "sports":
        return actual == requested
    if actual in _SPORTS_CATEGORY_ALIASES:
        return True
    if str(payload.get("strategy") or "").strip().lower() in _SPORTS_STRATEGY_SLUGS:
        return True
    markets = payload.get("markets")
    if not isinstance(markets, list):
        return False
    return any(
        isinstance(market, Mapping)
        and bool(str(market.get("sports_market_type") or "").strip())
        for market in markets
    )
```

- [ ] **Step 4: 让生产和兼容过滤路径共同使用纯函数**

生产载荷路径替换严格相等判断：

```python
if category:
    payloads = [payload for payload in payloads if _payload_matches_category(payload, category)]
```

兼容路径构造 `OpportunityFilter` 时不提前按 category 丢弃载荷，序列化后再调用同一纯函数：

```python
legacy_filter = OpportunityFilter(
    min_profit=min_profit,
    max_risk=max_risk,
    strategies=sorted(strategies),
    min_liquidity=min_liquidity,
    category=None,
)
payloads = [opportunity.model_dump() for opportunity in legacy_opportunities]
if category:
    payloads = [payload for payload in payloads if _payload_matches_category(payload, category)]
```

- [ ] **Step 5: 运行测试并确认 GREEN**

Run from `backend`:

```powershell
uv run pytest tests/test_opportunity_subfilters.py tests/test_routes_opportunities_ids.py -q
```

Expected: all tests pass.

---

### Task 3: 用失败测试定义体育策略状态可见性

**Files:**
- Modify: `backend/tests/test_scanner_pipeline.py`
- Modify: `backend/services/scanner.py:2888-2902`

- [ ] **Step 1: 写状态来源范围的失败测试**

在 `TestScannerInit` 中增加：

```python
    def test_runtime_strategy_list_includes_sports_market_refresh_source(self, monkeypatch):
        from services.scanner import strategy_loader

        scanner = _build_scanner()
        scanner._strategy_overrides = None
        scanner_strategy = SimpleNamespace(source_key="scanner", strategy_type="basic", name="Basic")
        sports_strategy = SimpleNamespace(
            source_key="sports",
            strategy_type="sports_overreaction_fader",
            name="Sports Overreaction Fader",
        )
        news_strategy = SimpleNamespace(source_key="news", strategy_type="news_edge", name="News Edge")
        monkeypatch.setattr(
            strategy_loader,
            "get_all_instances",
            lambda: [scanner_strategy, sports_strategy, news_strategy],
        )

        types = {strategy.strategy_type for strategy in scanner._get_all_strategies()}

        assert types == {"basic", "sports_overreaction_fader"}
```

- [ ] **Step 2: 运行测试并确认 RED**

Run from `backend`:

```powershell
uv run pytest tests/test_scanner_pipeline.py::TestScannerInit::test_runtime_strategy_list_includes_sports_market_refresh_source -q
```

Expected: FAIL because the current result only contains `basic`.

- [ ] **Step 3: 最小修改状态枚举来源范围**

将 `_get_all_strategies` 改为复用已经存在的市场刷新来源集合：

```python
def _get_all_strategies(self) -> list:
    """Return DB-loaded market-refresh strategy instances for this scanner."""
    if self._strategy_overrides is not None:
        return list(self._strategy_overrides)
    plugin_strategies = strategy_loader.get_all_instances()
    return [
        strategy
        for strategy in plugin_strategies
        if str(getattr(strategy, "source_key", "scanner") or "").strip().lower()
        in _MARKET_REFRESH_STRATEGY_SOURCE_KEYS
    ]
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run from `backend`:

```powershell
uv run pytest tests/test_scanner_pipeline.py::TestScannerInit::test_runtime_strategy_list_includes_sports_market_refresh_source -q
```

Expected: PASS.

---

### Task 4: 回归验证与变更边界审计

**Files:**
- Verify: `backend/api/routes.py`
- Verify: `backend/services/scanner.py`
- Verify: `backend/workers/host.py`
- Verify: `backend/workers/scanner_worker.py`
- Verify: `backend/tests/test_opportunity_subfilters.py`
- Verify: `backend/tests/test_scanner_pipeline.py`

- [ ] **Step 1: 运行聚焦回归测试**

Run from `backend`:

```powershell
uv run pytest tests/test_opportunity_subfilters.py tests/test_routes_opportunities_ids.py tests/test_scanner_pipeline.py tests/test_workers_host.py -q
```

Expected: all collected tests pass with zero failures.

- [ ] **Step 2: 运行静态检查**

Run from `backend`:

```powershell
uv run ruff check api/routes.py services/scanner.py workers/host.py workers/scanner_worker.py tests/test_opportunity_subfilters.py tests/test_scanner_pipeline.py
```

Expected: exit code 0.

- [ ] **Step 3: 审计生产代码边界**

Run from repository root:

```powershell
git diff --check -- backend/api/routes.py backend/services/scanner.py backend/workers/host.py backend/workers/scanner_worker.py backend/tests/test_opportunity_subfilters.py backend/tests/test_scanner_pipeline.py
git diff --name-only
```

Expected: this task adds changes only to the six listed sports query/runtime files. Existing unrelated dirty files remain unstaged and untouched.

- [ ] **Step 4: 精确暂存并检查提交范围**

```powershell
git add -- backend/api/routes.py backend/services/scanner.py backend/workers/host.py backend/workers/scanner_worker.py backend/tests/test_opportunity_subfilters.py backend/tests/test_scanner_pipeline.py
git diff --cached --check
git diff --cached --name-only
```

Expected staged names: exactly the six paths above; no trader, order, position, risk, simulation, ledger or settings implementation file.

- [ ] **Step 5: 提交体育修复**

```powershell
git commit -m "fix: classify and expose sports opportunities"
```

Expected: one commit containing only the audited sports files.

---

### Task 5: 只重建并重启 API 与 detection 平面

**Files:**
- Modify runtime only: `.env` image tag
- Do not restart: `worker-trading`, `worker-reconciliation`, `worker-services`, `worker-jobs`, `worker-news`, `worker-discovery`, `worker-recording`

- [ ] **Step 1: 记录部署前只读基线**

```powershell
$settings = Invoke-RestMethod 'http://127.0.0.1:8000/api/settings'
$settingsBefore = [ordered]@{
    polymarket = $settings.polymarket
    kalshi = $settings.kalshi
    scanner = $settings.scanner
    live_execution = $settings.live_execution
} | ConvertTo-Json -Depth 20 -Compress
$settingsHashBefore = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($settingsBefore)))
$tradingBefore = docker inspect homerun-worker-trading --format '{{.Id}}|{{.State.StartedAt}}|{{.Image}}'
$allBefore = @(Invoke-RestMethod 'http://127.0.0.1:8000/api/opportunities?limit=200')
$structuredSportsIdsBefore = @(
    $allBefore | Where-Object {
        $_.category -match '^(Sports|NBA|WNBA|NFL|MLB|NHL|ATP|WTA)$' -or
        $_.strategy -eq 'sports_overreaction_fader' -or
        @($_.markets | Where-Object { $_.sports_market_type }).Count -gt 0
    } | ForEach-Object id
)
$sportsBefore = @(Invoke-RestMethod 'http://127.0.0.1:8000/api/opportunities?category=sports&limit=200')
$statusBefore = Invoke-RestMethod 'http://127.0.0.1:8000/api/scanner/status'
```

Expected: variables contain a settings hash without volatile `updated_at`, immutable trading-container identity, structured sports IDs, current sports count and scanner status.

- [ ] **Step 2: 从固定本地源码构建新后端镜像**

```powershell
$env:HOMERUN_IMAGE_TAG='local-20260809-sports-visibility'
docker compose build backend
```

Expected: build exits 0 and creates `ghcr.io/braedonsaunders/homerun-backend:local-20260809-sports-visibility`.

- [ ] **Step 3: 更新固定标签并只重建两个容器**

Update `.env` line 1 to:

```text
HOMERUN_IMAGE_TAG=local-20260809-sports-visibility
```

Then run:

```powershell
$env:HOMERUN_IMAGE_TAG='local-20260809-sports-visibility'
docker compose up -d --no-deps --force-recreate backend worker-detection
```

Expected: only `homerun-backend` and `homerun-worker-detection` are recreated.

- [ ] **Step 4: 等待健康并验证实时结果**

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
$allAfter = @(Invoke-RestMethod 'http://127.0.0.1:8000/api/opportunities?limit=200')
$sportsAfter = @(Invoke-RestMethod 'http://127.0.0.1:8000/api/opportunities?category=sports&limit=200')
$statusAfter = Invoke-RestMethod 'http://127.0.0.1:8000/api/scanner/status'
$sportsRuntime = $statusAfter.strategies | Where-Object type -eq 'sports_overreaction_fader'
```

Expected: health is successful; sports query includes the existing structured sports opportunity; `$sportsRuntime.status` is `loaded`. The dedicated fader opportunity count may remain 0.

- [ ] **Step 5: 证明交易配置和交易 worker 未改变**

```powershell
$settings = Invoke-RestMethod 'http://127.0.0.1:8000/api/settings'
$settingsAfter = [ordered]@{
    polymarket = $settings.polymarket
    kalshi = $settings.kalshi
    scanner = $settings.scanner
    live_execution = $settings.live_execution
} | ConvertTo-Json -Depth 20 -Compress
$settingsHashAfter = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($settingsAfter)))
$tradingAfter = docker inspect homerun-worker-trading --format '{{.Id}}|{{.State.StartedAt}}|{{.Image}}'
$sportsIdsAfter = @($sportsAfter | ForEach-Object id)
$allIdsAfter = @($allAfter | ForEach-Object id)
$stillActiveStructuredSportsIds = @(
    $structuredSportsIdsBefore | Where-Object { $allIdsAfter -contains $_ }
)
[pscustomobject]@{
    settings_unchanged = $settingsHashBefore -eq $settingsHashAfter
    trading_worker_unchanged = $tradingBefore -eq $tradingAfter
    sports_before = $sportsBefore.Count
    sports_after = $sportsAfter.Count
    preexisting_structured_sports_visible = @($stillActiveStructuredSportsIds | Where-Object { $sportsIdsAfter -contains $_ }).Count -eq $stillActiveStructuredSportsIds.Count
    sports_strategy_status = $sportsRuntime.status
}
```

Expected: `settings_unchanged=True`, `trading_worker_unchanged=True`, `preexisting_structured_sports_visible=True`, sports count becomes non-zero while a structured sports opportunity remains active, and strategy status is `loaded`.

- [ ] **Step 6: 失败时回滚两个只读平面**

Only if acceptance checks fail:

```powershell
$env:HOMERUN_IMAGE_TAG='local-20260809-runtime-pipeline'
docker compose up -d --no-deps --force-recreate backend worker-detection
```

Expected: API and detection return to the previously running fixed image; trading worker remains untouched.
