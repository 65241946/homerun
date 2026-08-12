# Docs index

Prior analysis and design work on this codebase, kept here so a new
session can read the conclusions instead of re-deriving them (and
burning tokens re-analyzing what's already been figured out).

## Analysis & design reports

| Doc | What it is | Read it before |
|---|---|---|
| [`architecture-baseline.md`](./architecture-baseline.md) | Architecture map of the orchestrator / worker planes / settlement paths, plus the **V1–V7 regression criteria** used to judge a change safe. | touching the trader orchestrator, position lifecycle, simulation, or worker planes. |
| [`fix-01-shadow-settlement.md`](./fix-01-shadow-settlement.md) | Root cause + fix spec for the **asymmetric shadow simulation-ledger** (open debited, close never credited). Implemented as fix-01 on this branch. | changing shadow settlement, `reconcile_shadow_positions`, or `SimulationAccount` ledger flow. |
| [`audit-02-identity-finality.md`](./audit-02-identity-finality.md) | Batch-2 **identity & finality** audit findings (market identity, settlement finality). | working on market identity, resolution, or settlement finality. |

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
