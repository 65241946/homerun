export type TraderEventCacheRow = Record<string, any>

const FIREHOSE_VERBOSITIES = new Set(['whisper', 'murmur', 'voice', 'shout'])

function eventId(row: unknown): string {
  if (!row || typeof row !== 'object') return ''
  return String((row as TraderEventCacheRow).id || '').trim()
}

function eventTimestamp(row: unknown): number {
  if (!row || typeof row !== 'object') return 0
  const parsed = Date.parse(String((row as TraderEventCacheRow).created_at || ''))
  return Number.isFinite(parsed) ? parsed : 0
}

function mergeEvent(previous: TraderEventCacheRow | undefined, incoming: TraderEventCacheRow): TraderEventCacheRow {
  return {
    ...(previous || {}),
    ...incoming,
    payload: incoming.payload ?? previous?.payload ?? {},
  }
}

export function isFirehoseTraderEvent(event: unknown): boolean {
  if (!event || typeof event !== 'object') return false
  const verbosity = String((event as TraderEventCacheRow).verbosity || '').trim().toLowerCase()
  return FIREHOSE_VERBOSITIES.has(verbosity)
}

export function mergeTraderEventRows(
  current: unknown,
  incomingEvents: TraderEventCacheRow[],
  maxRows = 800,
): TraderEventCacheRow[] {
  const pendingById = new Map<string, TraderEventCacheRow>()
  for (const incoming of incomingEvents) {
    const id = eventId(incoming)
    if (!id) continue
    const previous = pendingById.get(id)
    pendingById.delete(id)
    pendingById.set(id, previous ? mergeEvent(previous, incoming) : incoming)
  }

  const previousRows = Array.isArray(current) ? current : []
  const updatedRows = previousRows.map((row) => {
    const id = eventId(row)
    const incoming = id ? pendingById.get(id) : undefined
    if (!incoming) return row as TraderEventCacheRow
    pendingById.delete(id)
    return mergeEvent(row as TraderEventCacheRow, incoming)
  })

  const newRows = Array.from(pendingById.values())
    .reverse()
    .map((row) => mergeEvent(undefined, row))

  return [...newRows, ...updatedRows]
    .sort((a, b) => eventTimestamp(b) - eventTimestamp(a))
    .slice(0, Math.max(0, maxRows))
}
