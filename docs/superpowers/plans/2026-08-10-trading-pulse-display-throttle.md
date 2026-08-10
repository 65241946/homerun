# Trading Pulse Display Throttle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让机器人普通决策/事件只随现有只读 API 每 30 秒更新界面，同时保持后台全速、订单和严重风险即时显示，并提供无交易副作用的手动刷新。

**Architecture:** 浏览器继续接收现有频道订阅允许的 WebSocket 数据，但主 `App` 调用方不把普通 `trader_decision` 和非严重 `trader_event` 发布到 React state；订单和严重风险继续沿用即时 WebSocket 路径。机器人页直接复用已有的 decisions/events/firehose GET 查询作为 30 秒数据库快照，避免新增第二套缓冲、定时器和缓存覆盖顺序。

**Tech Stack:** React 19、TypeScript、TanStack React Query、原生 WebSocket、`node:test` + 已安装的 `vite-node`、Vite。

---

## 文件结构

- Create: `frontend/src/hooks/tradingActivityVisibility.ts` — 普通活动与严重事件的纯消息分级。
- Create: `frontend/tests/tradingActivityVisibility.test.ts` — 分级安全边界测试。
- Modify: `frontend/src/hooks/useWebSocket.ts` — 为当前调用方增加普通交易活动的 React 发布抑制选项。
- Modify: `frontend/src/App.tsx` — 只在主调用方启用抑制，其他 WebSocket 使用者保持原行为。
- Modify: `frontend/src/components/TradingPanel.tsx` — 复用现有 30 秒 GET 查询、增加手动刷新和刷新时间、移除 1 Hz 慢速滴入。
- Modify: `frontend/src/i18n/locales/en.json` — 英文批量刷新与“只暂停界面”文案。
- Modify: `frontend/src/i18n/locales/zh.json` — 中文批量刷新与“只暂停界面”文案。
- Modify: `docs/superpowers/specs/2026-08-10-trading-pulse-display-throttle-design.md` — 记录复用现有 30 秒 API 快照的实现收敛。

## 不允许修改

- `backend/` 下任何文件；
- WebSocket 服务端、worker、扫描周期、策略参数、订单、持仓、余额、PnL、费用和数据库；
- `frontend/src/hooks/useRealtimeInvalidation.ts` 的订单/风险即时处理语义；
- `frontend/src/components/PositionsPanel.tsx` 及工作区已有的其他用户改动；
- 未启用新选项的其他 `useWebSocket` 调用方。

### Task 1: 建立并接入 WebSocket 展示分级边界

**Files:**
- Create: `frontend/tests/tradingActivityVisibility.test.ts`
- Create: `frontend/src/hooks/tradingActivityVisibility.ts`
- Modify: `frontend/src/hooks/useWebSocket.ts:1-204`
- Modify: `frontend/src/App.tsx:786-793`

- [ ] **Step 1: 写消息分级失败测试**

创建 `frontend/tests/tradingActivityVisibility.test.ts`：

```ts
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  isImmediateTraderEvent,
  shouldPublishWebSocketMessage,
  shouldSuppressOrdinaryTradingActivity,
  type WebSocketMessage,
} from '../src/hooks/tradingActivityVisibility'

const message = (
  type: string,
  extra: Record<string, unknown> = {},
): WebSocketMessage => ({ type, data: extra })

test('ordinary decisions and informational trader events are suppressed', () => {
  assert.equal(shouldSuppressOrdinaryTradingActivity(message('trader_decision', { id: 'd1' })), true)
  assert.equal(shouldSuppressOrdinaryTradingActivity(message('trader_event', { id: 'e1', severity: 'info' })), true)
})

test('orders and backend notifier issue severities remain immediate', () => {
  assert.equal(shouldSuppressOrdinaryTradingActivity(message('trader_order', { id: 'o1' })), false)
  for (const severity of ['warn', 'warning', 'error', 'critical', 'failed']) {
    const row = message('trader_event', { id: severity, severity })
    assert.equal(isImmediateTraderEvent(row), true)
    assert.equal(shouldSuppressOrdinaryTradingActivity(row), false)
  }
})

test('kill switch and live preflight events remain immediate even with info severity', () => {
  for (const eventType of ['kill_switch', 'live_preflight']) {
    const row = message('trader_event', { id: eventType, severity: 'info', event_type: eventType })
    assert.equal(isImmediateTraderEvent(row), true)
    assert.equal(shouldSuppressOrdinaryTradingActivity(row), false)
  }
})

test('unrelated websocket messages remain unchanged', () => {
  for (const type of ['scanner_status', 'position_marks_update', 'crypto_markets_update']) {
    assert.equal(shouldSuppressOrdinaryTradingActivity(message(type)), false)
  }
})

test('publication suppression is opt-in and never hides critical events', () => {
  const ordinary = message('trader_decision', { id: 'd1' })
  const critical = message('trader_event', { id: 'e1', severity: 'critical' })
  assert.equal(shouldPublishWebSocketMessage(ordinary, false), true)
  assert.equal(shouldPublishWebSocketMessage(ordinary, true), false)
  assert.equal(shouldPublishWebSocketMessage(critical, true), true)
})
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run:

```powershell
cd frontend
npx.cmd --no-install vite-node tests/tradingActivityVisibility.test.ts
```

Expected: FAIL，错误包含 `Cannot find module '../src/hooks/tradingActivityVisibility'`。

- [ ] **Step 3: 实现与后台通知边界一致的纯分级函数**

创建 `frontend/src/hooks/tradingActivityVisibility.ts`：

```ts
export type WebSocketMessage = {
  type: string
  data?: Record<string, any>
}

const IMMEDIATE_SEVERITIES = new Set(['warn', 'warning', 'error', 'critical', 'failed'])
const IMMEDIATE_EVENT_TYPES = new Set(['kill_switch', 'live_preflight'])

function normalized(value: unknown): string {
  return String(value ?? '').trim().toLowerCase()
}

export function isImmediateTraderEvent(message: WebSocketMessage): boolean {
  if (message.type !== 'trader_event') return false
  return IMMEDIATE_SEVERITIES.has(normalized(message.data?.severity))
    || IMMEDIATE_EVENT_TYPES.has(normalized(message.data?.event_type))
}

export function shouldSuppressOrdinaryTradingActivity(message: WebSocketMessage): boolean {
  if (message.type === 'trader_decision') return true
  return message.type === 'trader_event' && !isImmediateTraderEvent(message)
}

export function shouldPublishWebSocketMessage(
  message: WebSocketMessage,
  suppressOrdinaryTradingActivity: boolean,
): boolean {
  return !suppressOrdinaryTradingActivity || !shouldSuppressOrdinaryTradingActivity(message)
}
```

`warn`、`warning`、`error`、`critical`、`kill_switch` 和 `live_preflight` 与 `backend/services/notifier.py` 的现有问题事件边界一致；`failed` 保留设计规格要求的防御性兼容。

- [ ] **Step 4: 运行分级测试并确认通过**

Run:

```powershell
cd frontend
npx.cmd --no-install vite-node tests/tradingActivityVisibility.test.ts
```

Expected: 5 tests PASS，进程退出码为 0。

- [ ] **Step 5: 给 `useWebSocket` 增加默认关闭的发布抑制选项**

在 `frontend/src/hooks/useWebSocket.ts` 中导入纯分级函数和消息类型，删除文件内重复的 `WebSocketMessage` 接口：

```ts
import {
  shouldPublishWebSocketMessage,
  type WebSocketMessage,
} from './tradingActivityVisibility'

type UseWebSocketOptions = {
  suppressOrdinaryTradingActivity?: boolean
}
```

将 hook 签名改为：

```ts
export function useWebSocket(
  url: string,
  messageTypes?: string[],
  options: UseWebSocketOptions = {},
) {
```

在现有 ref 初始化区加入：

```ts
  const optionsRef = useRef(options)
  optionsRef.current = options
```

将 listener 内原来的 `setLastMessage(msg)` 替换为：

```ts
      if (!shouldPublishWebSocketMessage(
        msg,
        Boolean(optionsRef.current.suppressOrdinaryTradingActivity),
      )) {
        return
      }
      setLastMessage(msg)
```

不要改变共享 socket、频道订阅、重连、ping、可见性或发送逻辑。第三个参数缺省时行为与修改前完全一致。

- [ ] **Step 6: 只在 App 主调用方启用抑制**

把 `frontend/src/App.tsx` 的主调用改为：

```ts
  const { isConnected, lastMessage, sendMessage } = useWebSocket('/ws', undefined, {
    suppressOrdinaryTradingActivity: true,
  })
```

不修改 `CryptoCycleChart`、`CryptoMarketsPanel`、`RecentTradesPanel`、新闻、体育和天气组件的调用。

- [ ] **Step 7: 运行测试与完整前端构建**

Run:

```powershell
cd frontend
npx.cmd --no-install vite-node tests/tradingActivityVisibility.test.ts
npm.cmd run build
```

Expected: 5 tests PASS；`tsc && vite build` 退出码为 0。

- [ ] **Step 8: 精确提交 Task 1**

```powershell
git add -- frontend/src/hooks/tradingActivityVisibility.ts frontend/src/hooks/useWebSocket.ts frontend/src/App.tsx frontend/tests/tradingActivityVisibility.test.ts
git diff --cached --check
git commit -m "perf: suppress ordinary trading websocket renders"
```

### Task 2: 固定 30 秒数据库快照与只读手动刷新

**Files:**
- Modify: `frontend/src/components/TradingPanel.tsx:1-100,5344-5373,5691-5723,8914-9016,10596-10667,11495-11555`
- Modify: `frontend/src/i18n/locales/en.json:5333-5345,5687-5706`
- Modify: `frontend/src/i18n/locales/zh.json:5332-5344,5686-5705`

- [ ] **Step 1: 固定三个普通活动查询的挂载和焦点复核行为**

保留 `allDecisionsQuery` 和 `allEventsQuery` 的 30 秒周期；把 `firehoseHistoryQuery` 从 60 秒改为 30 秒。三个 query 均显式加入：

```ts
refetchOnMount: 'always',
refetchOnWindowFocus: 'always',
refetchIntervalInBackground: false,
```

最终关键配置应为：

```ts
  const allDecisionsQuery = useQuery({
    queryKey: ['trader-decisions-all', traderIdsKey],
    enabled: traderIds.length > 0,
    refetchInterval: 30000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    refetchIntervalInBackground: false,
    staleTime: 0,
    queryFn: () => getAllTraderDecisions(traderIds, {
      limit: Math.min(5000, Math.max(200, traderIds.length * 160)),
      per_trader_limit: 160,
    }),
  })

  const allEventsQuery = useQuery({
    queryKey: ['trader-events-all', traderIdsKey],
    enabled: traderIds.length > 0,
    refetchInterval: 30000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    refetchIntervalInBackground: false,
    queryFn: () => getAllTraderEventsBulk(traderIds, { limit: 500 }),
  })

  const firehoseHistoryQuery = useQuery({
    queryKey: ['trader-firehose-recent'],
    refetchInterval: 30000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    refetchIntervalInBackground: false,
    queryFn: () => getRecentFirehoseEvents({ limit: 500 }),
  })
```

这些 queryFn 全部是现有 GET 接口；不新增 mutation。

- [ ] **Step 2: 增加只读手动刷新和最近刷新时间**

在 `TradingPanel.tsx` 从 `lucide-react` 导入 `RefreshCw`，增加 state：

```ts
const [activityRefreshPending, setActivityRefreshPending] = useState(false)
```

在三个活动 query 定义后加入：

```ts
  const activityLastUpdatedAtMs = Math.max(
    allDecisionsQuery.dataUpdatedAt,
    allEventsQuery.dataUpdatedAt,
    firehoseHistoryQuery.dataUpdatedAt,
  )

  const handleActivityRefresh = async () => {
    if (activityRefreshPending) return
    setActivityRefreshPending(true)
    try {
      await Promise.all([
        allDecisionsQuery.refetch(),
        allEventsQuery.refetch(),
        firehoseHistoryQuery.refetch(),
        allOrdersQuery.refetch(),
      ])
    } finally {
      setActivityRefreshPending(false)
    }
  }
```

在全局活动流和单机器人终端的控制区各加入：

```tsx
<Button
  size="sm"
  variant="outline"
  onClick={() => void handleActivityRefresh()}
  disabled={activityRefreshPending}
  title={t('tradingPanel.terminal.refreshUi')}
  className="h-5 px-1.5 text-[10px]"
>
  <RefreshCw className={cn('mr-1 h-3 w-3', activityRefreshPending && 'animate-spin')} />
  {t('common.refresh')}
</Button>
```

活动流标题改用 `tradingPanel.allBots.recentActivity30s`；在事件数旁加入：

```tsx
{activityLastUpdatedAtMs > 0
  ? ` · ${t('tradingPanel.terminal.lastUiRefresh', {
      time: new Date(activityLastUpdatedAtMs).toLocaleTimeString(),
    })}`
  : ''}
```

- [ ] **Step 3: 移除与 30 秒上限冲突的 1 Hz 慢速滴入**

删除以下内容：

- `terminalSlowMode` state；
- `slowModeQueueRef`、`slowModeTimerRef`、`slowModePending`；
- 两处乌龟慢速按钮；
- 慢速队列提示；
- 每秒 `setInterval` 及其卸载清理 effect。

将活动列表更新 effect 收敛为每次查询快照或即时订单/严重事件只合并一次：

```ts
  useEffect(() => {
    if (terminalPaused) return
    const fresh: ActivityRow[] = []
    const seen = seenIdsRef.current
    for (const row of filteredTraderActivityRows) {
      const key = `${row.kind}:${row.id}`
      if (!seen.has(key)) {
        seen.add(key)
        fresh.push(row)
      }
    }
    if (fresh.length === 0) return
    setDisplayedActivityRows((previous) => {
      const merged = [...fresh, ...previous]
      merged.sort((left, right) => toTs(right.ts) - toTs(left.ts))
      return merged.slice(0, terminalMaxRows)
    })
  }, [filteredTraderActivityRows, terminalPaused, terminalMaxRows])
```

暂停恢复 effect 只恢复最新 `filteredTraderActivityRows` 并重建 `seenIdsRef`。暂停仍不影响 query、worker 或交易。

- [ ] **Step 4: 增加准确的中英文界面语义**

在英文 `tradingPanel.allBots` 增加：

```json
"recentActivity30s": "Recent Activity (refreshes every 30s)"
```

在中文 `tradingPanel.allBots` 增加：

```json
"recentActivity30s": "最近活动（每30秒刷新）"
```

在英文 `tradingPanel.terminal` 增加：

```json
"lastUiRefresh": "UI updated {{time}}",
"pauseUiUpdates": "Pause UI updates (background trading continues)",
"pausedUiHint": "UI updates paused — background collection and trading continue.",
"refreshUi": "Refresh activity from read-only APIs now",
"resumeUiUpdates": "Resume UI updates"
```

在中文 `tradingPanel.terminal` 增加：

```json
"lastUiRefresh": "界面更新于 {{time}}",
"pauseUiUpdates": "暂停界面更新（后台交易继续）",
"pausedUiHint": "界面更新已暂停，后台采集和交易仍在继续。",
"refreshUi": "立即从只读接口刷新活动",
"resumeUiUpdates": "恢复界面更新"
```

两处暂停按钮 title 改用 `pauseUiUpdates`/`resumeUiUpdates`，两处暂停空状态改用 `pausedUiHint`。其他语言缺少新键时由现有 `fallbackLng: 'en'` 返回英文，不再使用会暗示后台停止的旧文案。

- [ ] **Step 5: 验证 JSON、聚焦测试和完整构建**

Run:

```powershell
cd frontend
node -e "for (const f of ['en','zh']) JSON.parse(require('fs').readFileSync('src/i18n/locales/'+f+'.json','utf8')); console.log('locale json ok')"
npx.cmd --no-install vite-node tests/tradingActivityVisibility.test.ts
npm.cmd run build
```

Expected: 输出 `locale json ok`；5 tests PASS；`tsc && vite build` 退出码为 0。

- [ ] **Step 6: 精确提交 Task 2**

```powershell
git add -- frontend/src/components/TradingPanel.tsx frontend/src/i18n/locales/en.json frontend/src/i18n/locales/zh.json
git diff --cached --check
git commit -m "perf: refresh trading activity in stable snapshots"
```

### Task 3: 运行态验收与交易边界复核

**Files:**
- Verify only: `frontend/dist/`
- Verify only: 浏览器、Network、现有后台日志和只读账户接口

- [ ] **Step 1: 运行完整前端验证**

Run:

```powershell
cd frontend
npx.cmd --no-install vite-node tests/tradingActivityVisibility.test.ts
npm.cmd run build
```

Expected: 5 tests PASS；构建退出码为 0。

- [ ] **Step 2: 检查实现提交没有后端改动**

从仓库根目录运行：

```powershell
git diff HEAD~2..HEAD --name-only
git diff HEAD~2..HEAD -- backend
git diff HEAD~2..HEAD --check
```

Expected: 文件列表只包含本计划列出的前端文件和测试；后端 diff 无输出；diff check 退出码为 0。

- [ ] **Step 3: 浏览器运行态验收**

在当前已启动系统记录以下结果：

1. 打开机器人页保持 90 秒；普通决策/事件只随约 30、60、90 秒的 GET 快照变化，不再逐条跳动；
2. 切换到其他页面 60 秒；机器人活动列表不产生 React commit，返回机器人页后 decisions/events/firehose 各拉取一次；
3. 点击“刷新”；Network 只出现现有 decisions/events/firehose/orders GET 请求，没有 POST、PUT、PATCH 或 DELETE；
4. 观察一条 `trader_order`，确认 WebSocket 到达后 1 秒内出现；观察一条 `warn`、`error` 或 `critical` trader event，确认 1 秒内出现；
5. 点击暂停并观察 30 秒，确认后台 worker 日志和数据库决策计数继续增长，恢复后显示最新快照；
6. 对比优化前后 90 秒 Chrome Performance：普通决策不再触发主 `App` 的逐条 React commit，鼠标和滚动保持可操作。

- [ ] **Step 4: 核对模拟账户和账本无前端副作用**

部署前后各读取一次现有账户总览、订单、持仓和 PnL 只读接口。允许后台策略正常运行造成自然变化；本次前端实现不得创建订单、关闭持仓、修改余额、改写费用或变更策略配置。

- [ ] **Step 5: 最终状态检查**

Run:

```powershell
git status --short --branch
git log -2 --oneline
```

Expected: 两个实现提交依次存在；工作区原有无关改动仍保留且未被纳入这些提交。

## 回滚

若运行态发现重要事件延迟或活动快照异常，按逆序回滚两个实现提交。回滚只影响前端展示，不需要数据库迁移、账本修复或策略恢复。
