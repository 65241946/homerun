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
