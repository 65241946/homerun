import {
  MANUAL_BUY_INVALIDATION_KEYS,
  buildConfigFromTraderSignal,
} from '../src/components/BuyButton'
import { normalizeTraderOpportunity } from '../src/components/TraderSignalViews'

function invariant(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message)
}

const noToken = '2'.repeat(72)
const signal = normalizeTraderOpportunity({
  id: 'wallet-opportunity-contract',
  strategy: 'traders_confluence',
  title: 'Wallet consensus contract',
  total_cost: 0.66,
  confidence: 0.82,
  detected_at: '2026-08-11T00:00:00Z',
  markets: [
    {
      id: 'provider-market-contract',
      question: 'Will the selected player win?',
      yes_price: 0.34,
      no_price: 0.66,
    },
  ],
  positions_to_take: [
    {
      token_id: noToken,
      action: 'BUY',
      price: 0.66,
      market_id: 'provider-market-contract',
      market: 'Will the selected player win?',
      outcome: 'NO',
    },
  ],
  strategy_context: { source_key: 'traders', outcome: 'NO' },
} as never)

const config = buildConfigFromTraderSignal(signal)
invariant(config.opportunityId === 'wallet-opportunity-contract', 'opportunity id was dropped')
invariant(config.positions.length === 1, 'execution position was dropped')
invariant(config.positions[0].token_id === noToken, 'authoritative NO token was dropped')
invariant(config.positions[0].direction === 'buy_no', 'canonical buy_no direction was dropped')

const cacheRoots = new Set(MANUAL_BUY_INVALIDATION_KEYS.map((key) => key[0]))
for (const requiredRoot of ['simulation-accounts', 'positions-panel', 'accounts-panel']) {
  invariant(cacheRoots.has(requiredRoot), `missing cache invalidation root: ${requiredRoot}`)
}

console.log('manual-buy contract check passed')
