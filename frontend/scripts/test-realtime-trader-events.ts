import assert from 'node:assert/strict'
import { isFirehoseTraderEvent, mergeTraderEventRows } from '../src/lib/traderEventCache'

const current = [
  { id: 'old', created_at: '2026-08-12T00:00:00Z', payload: { retained: true } },
  { id: 'update', created_at: '2026-08-12T00:00:01Z', payload: { retained: true }, message: 'before' },
]
const merged = mergeTraderEventRows(current, [
  { id: 'new-1', created_at: '2026-08-12T00:00:02Z', payload: { n: 1 } },
  { id: 'new-2', created_at: '2026-08-12T00:00:02Z', payload: { n: 2 } },
  { id: 'update', created_at: '2026-08-12T00:00:03Z', message: 'after' },
])
const repeatedUpdate = mergeTraderEventRows(current, [
  { id: 'update', created_at: '2026-08-12T00:00:02Z', payload: { queued: true } },
  { id: 'update', created_at: '2026-08-12T00:00:03Z', message: 'latest' },
])

assert.deepEqual(merged.map((row) => row.id), ['update', 'new-2', 'new-1', 'old'])
assert.equal(merged[0].message, 'after')
assert.deepEqual(merged[0].payload, { retained: true })
assert.deepEqual(mergeTraderEventRows(undefined, [merged[1]])[0], merged[1])
assert.deepEqual(mergeTraderEventRows(current, [merged[1]], 2).map((row) => row.id), ['new-2', 'update'])
assert.equal(repeatedUpdate[0].message, 'latest')
assert.deepEqual(repeatedUpdate[0].payload, { queued: true })
assert.equal(isFirehoseTraderEvent({ verbosity: 'WHISPER' }), true)
assert.equal(isFirehoseTraderEvent({ severity: 'info' }), false)

const burst = Array.from({ length: 1_000 }, (_, index) => ({
  id: `burst-${index}`,
  created_at: new Date(Date.UTC(2026, 7, 12, 0, 0, index)).toISOString(),
}))
const cappedBurst = mergeTraderEventRows([], burst, 800)
assert.equal(cappedBurst.length, 800)
assert.equal(new Set(cappedBurst.map((row) => row.id)).size, 800)
assert.equal(cappedBurst[0].id, 'burst-999')
assert.equal(cappedBurst.at(-1)?.id, 'burst-200')

console.log('realtime trader event cache regression checks passed')
