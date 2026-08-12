# Docs index

Prior analysis and design work on this codebase, kept here so a new
session can read the conclusions instead of re-deriving them (and
burning tokens re-analyzing what's already been figured out).

## Analysis & design reports

| Doc | What it is | Read it before |
|---|---|---|
| [`architecture-baseline.md`](./architecture-baseline.md) | Architecture map of the orchestrator / worker planes / settlement paths, plus the **V1–V7 regression criteria** used to judge a change safe. | touching the trader orchestrator, position lifecycle, simulation, or worker planes. |
| [`fix-01-shadow-settlement.md`](./fix-01-shadow-settlement.md) | Root cause + fix spec for the **asymmetric shadow simulation-ledger** (open debited, close never credited). Implemented as fix-01 on this branch. | changing shadow settlement, `reconcile_shadow_positions`, or `SimulationAccount` ledger flow. |
| [`fix-02-p0-strategy.md`](./fix-02-p0-strategy.md) | Spec for the **strategy-layer P0 correctness batch** (copy-trade delay/edge, certainty_shock exclusion/shock-window, detect-vs-evaluate threshold single-source). Implemented as fix-02. | changing copy-trade, certainty_shock, or any strategy's detect/evaluate thresholds. |
| [`fix-02-audit.md`](./fix-02-audit.md) | Independent review of fix-02 + full-strategy P0-4 sweep. **Flags a separate latent issue** (declared default_config thresholds vs base hardcoded evaluate fallback) for the P1 session. | tuning strategy thresholds; before assuming a `default_config` threshold takes effect in live evaluate. |
| [`fix-03-foundation-p0.md`](./fix-03-foundation-p0.md) · [`fix-03-audit.md`](./fix-03-audit.md) | **Fee regime** (pre-2026 quartic → category-rated quadratic; fees were understated ~2.2x at p=0.50) and **trader provenance** (backtest/UI yielded zero trader opportunities). Spec + independent review. | any fee-aware EV work, or any trader/copy-trade backtest. **Threshold tuning done on the old fee curve is void.** |
| [`fix-04-batch-a-reconciled.md`](./fix-04-batch-a-reconciled.md) | Batch-A strategy repairs **reconciled against fix-02/fix-03** — says which WO-A items are already done, which are superseded, which still stand. | implementing WO-A1–A5. Use this, not the raw spec in `repair/`. |
| [`fix-05-batch-b-reconciled.md`](./fix-05-batch-b-reconciled.md) | Batch-B crypto-HF repairs (BTC/ETH engines + 5 small strategies), reconciled against fix-03. Confirms `convergence_*` / `maker_quote_*` config blocks are entirely dead (declared, never read). | implementing WO-B0–B4. Note G-2 is a deliberate behaviour change. |
| [`diagnosis-live-run-2026-08-12.md`](./diagnosis-live-run-2026-08-12.md) | **Live-run diagnosis**: shadow account produced zero orders in hours. Root cause chain from missing Polymarket credentials → dead market-data pipeline → fail-closed gates, plus three independent code defects it exposed. | any "why is nothing trading" question; before assuming a strategy threshold is at fault. |
| [`fix-06-shadow-runtime.md`](./fix-06-shadow-runtime.md) | Spec for the three defects that diagnosis exposed: manual buy never settles in shadow, shadow blocked by *live* trading credentials, and an unstamped `strategy_origin` producer path. | fixing shadow-mode runtime behaviour. |
| [`fix-07-ws-coverage.md`](./fix-07-ws-coverage.md) | After credentials were fixed, **every remaining block** is `strict WS pricing: source=unknown` — WS subscriptions only cover crypto markets, so trader/copy-trade signals on other markets never get a live price. | anything about "signals blocked on pricing", WS subscription scope, or relaxing shadow pricing. |
| [`fix-08-undeclared-config-keys.md`](./fix-08-undeclared-config-keys.md) | Four `directional_*` entry-price keys exist only as `params.get(key, magic)` and were never declared — invisible in the UI, unreachable by tuning. Pre-existing on the clean base. | tuning directional entry-price bounds, or chasing a knob that "does nothing". |
| [`analysis-wallet-consensus-chain.md`](./analysis-wallet-consensus-chain.md) | Wallet-consensus → trader-signal → opportunity → execution map; the two meanings of "trader"; verified vs unverified findings. | working on wallet consensus, copy-trade, or trader scope matching. |
| [`audit-02-identity-finality.md`](./audit-02-identity-finality.md) | Batch-2 **identity & finality** audit findings (market identity, settlement finality). | working on market identity, resolution, or settlement finality. |
| [`repair/`](./repair/) | Strategy-session deliverables: WO-A/WO-B work orders, 2026 fee addendum, new-strategy gap analysis, data-recording guide. | broader strategy repair/优化 context. **`traders_copy_trade_p0_draft.patch` is void** — superseded by fix-02. |

## Reference & ops

| Doc | What it is |
|---|---|
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | How to contribute. |
| [`SECURITY.md`](./SECURITY.md) | Security policy. |
| [`plane_isolation_handoff.md`](./plane_isolation_handoff.md) | Worker-plane isolation notes (why the trading/news/discovery planes stay split). |

---

**Baseline:** all current development is on the clean base `389d246`
(GitHub `65241946/homerun`, upstream `braedonsaunders/homerun`) plus
fix-01, on branch `claude/batch-2-identity-finality-review-jvcsc5`.
