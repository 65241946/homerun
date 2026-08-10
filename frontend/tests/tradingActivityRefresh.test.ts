import assert from 'node:assert/strict'
import test from 'node:test'
import {
  TRADING_ACTIVITY_REFRESH_MS,
  refreshTradingActivitySnapshots,
} from '../src/lib/tradingActivityRefresh'

test('ordinary trading activity refreshes every 30 seconds', () => {
  assert.equal(TRADING_ACTIVITY_REFRESH_MS, 30_000)
})

test('manual refresh invokes every supplied read-only refetch exactly once', async () => {
  const calls = [0, 0, 0, 0]
  const refetchers = calls.map((_, index) => async () => {
    calls[index] += 1
    return { data: index }
  })

  await refreshTradingActivitySnapshots(refetchers)

  assert.deepEqual(calls, [1, 1, 1, 1])
})
