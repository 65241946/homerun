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
