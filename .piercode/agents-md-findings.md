# Review findings for `agents.md`

Reviewed the complete 534-line `agents.md` against the `writing-for-agents` skill and spot-checked its claims against the current working tree on 2026-08-11.

## Verdict

The document does **not yet meet the skill's standard**, although it contains valuable project knowledge that should be preserved.

Its strongest material is the non-obvious domain and operational guidance: the opportunity identity rules, live-price preference, database contention warning, shared WebSocket rule, strategy infrastructure rule, and financial-system guardrails. Its main failure is information hierarchy. One always-loaded file mixes durable invariants, task-specific recipes, copied source code, dependency versions, directory listings, naming conventions, and prohibitions. There are no branch-triggered pointers telling an agent which detailed reference to load for strategy, migration, worker, API, or frontend work.

That creates three concrete problems:

- **Context load and sprawl:** all 534 lines are paid on every task, including frontend examples during backend work and migration examples during documentation work.
- **Stale caches:** source-discoverable facts copied into the document have already diverged from the repository.
- **Execution variance:** several absolute rules conflict with other rules or lack a checkable completion criterion.

A later edit should be surgical, not a rewrite: keep the durable domain knowledge, fix the false statements first, retain a short always-loaded set of invariants and task-routing pointers, and move branch-specific recipes behind those pointers.

## Concrete defects

### 1. The document has no progressive-disclosure routing

> `agents.md:167` — `## Code Patterns — Follow These Exactly`

The following 310 lines place strategy, route, database, ORM, migration, worker, WebSocket, event bus, logging, React, API-client, and Jotai material on the same always-loaded tier. An agent changing one route does not need full worker, WebSocket, state, and strategy recipes in context.

What it should be instead: keep one short routing section in `agents.md`, with one trigger per genuine branch. For example, strategy work should point to the current strategy contract; schema work should point to Alembic helpers and recent migrations; frontend data work should point to the shared API client; worker work should point to worker-state/control references. Each disclosed document should co-locate that branch's steps, rules, examples, and completion checks.

### 2. The architecture and exact-version cache is stale

> `agents.md:63` — `frontend/                   # React 18 + TypeScript + Vite`
>
> `agents.md:72-73` — `alembic/` at repository root
>
> `agents.md:90-107` — `## Tech Stack (exact versions matter)` with FastAPI `0.109.0`, Uvicorn `0.27.0`, Pydantic `2.5.0`, React `18.2.0`, and Recharts `3.7.0`

These are environment caches, and several are false in the current tree:

- migrations live under `backend/alembic/`, not root `alembic/`;
- `backend/requirements.txt` now declares FastAPI `>=0.115.0`, Uvicorn `>=0.31.1`, and Pydantic `>=2.7.0`;
- `frontend/package.json` declares React `^19.2.4` and Recharts `^2.15.4`;
- `frontend/package-lock.json` currently resolves React `19.2.4`, TypeScript `5.9.3`, Vite `5.4.21`, and React Query `5.90.20`, so the table also confuses declared ranges with resolved versions.

What it should be instead: delete the version table from the always-loaded document and point version-sensitive work to `backend/requirements.txt`, `frontend/package.json`, and the applicable lock/install evidence. Retain only non-obvious compatibility constraints that cannot be learned cheaply from those files.

### 3. The domain-type reference names removed types

> `agents.md:127` — `# Strategy types (StrategyType enum in models/opportunity.py)`
>
> `agents.md:147-148` — `CopyTradingMode: ALL_TRADES | ARB_ONLY`

Neither `StrategyType` nor `CopyTradingMode` is defined under the current `backend/models` or `backend/services` tree. `BaseStrategy.strategy_type` is now a string slug (`backend/services/strategies/base.py:564`), which is consistent with dynamic, DB-backed strategies.

What it should be instead: describe strategy identity as a string slug and point to the current catalog/loader for available strategies. Remove the obsolete copy-trading enum unless its current authoritative definition is located and cited.

### 4. The strategy contract is incomplete and partially stale

> `agents.md:173-176` — required implementation is `detect(...)` or `detect_async(...)`
>
> `agents.md:186-202` — copied `StrategyDecision` and `ExitDecision` dataclasses

`BaseStrategy` now exposes `detect_sync()` as the preferred synchronous entry point (`base.py:854-873`) while retaining `detect()` for backward compatibility. It also supports custom `on_event()` routing. The copied dataclasses omit live fields: `StrategyDecision.payload`, plus `ExitDecision.exit_policy` and `ExitDecision.payload` (`base.py:51-77`). A copied contract that omits execution-policy data is especially risky in a trading system.

What it should be instead: keep only the durable authoring decision in the always-loaded file: read `backend/services/strategies/base.py` before strategy changes; use a string slug; choose the appropriate current detection/event entry point; use `create_opportunity`; verify evaluate/exit behavior through shared infrastructure. Do not duplicate the dataclass definitions. If a compact contract remains, it must include a completion criterion such as accounting for every overridden lifecycle method and running shared, slug-agnostic strategy tests.

### 5. The ORM example directly contradicts the timezone rule

> `agents.md:282-283` — `default=datetime.utcnow` and `onupdate=datetime.utcnow`
>
> `agents.md:504` — `never bare datetime.utcnow()`

This is a co-location and correctness failure: an agent following the ORM recipe violates the later gotcha. The live model layer aliases `DateTime` to `UTCDateTime` and uses `_utcnow()`, which returns `datetime.now(timezone.utc)` (`backend/models/database.py:155-159`).

What it should be instead: the ORM recipe must use the repository's current UTC-aware type/default pattern, or preferably point to `backend/models/database.py` and state only the invariant: all persisted datetimes use the repository UTC type and aware UTC default.

### 6. The migration recipe no longer matches the repository

> `agents.md:291` — migrations are in `backend/alembic/versions/`
>
> `agents.md:297-314` — define a local `_column_names`, make `downgrade()` a no-op, and “Always check `_column_names` before adding columns”

The path here is correct but contradicts the root architecture tree. More importantly, current migrations use shared helpers from `backend/alembic_helpers.py`, including `safe_add_column`, `safe_create_index`, and `safe_create_table`. Only 26 of the 169 current migration files define a local `_column_names`; this is not a universal convention. Recent migrations `202608100001` and `202608100002` also implement real downgrade operations, so “downgrades are disabled” is false as a repository-wide rule.

What it should be instead: point schema work to `backend/alembic_helpers.py`, the current migration head, and recent migrations with the same operation type. State the actual invariant—make upgrade operations safe for the supported database states—and require a schema-specific verification criterion. Do not prescribe one helper or downgrade policy unless the repository enforces it centrally.

### 7. The frontend API-client section points to the wrong source of truth

> `agents.md:67` — `api.ts          # Main API service + type definitions`
>
> `agents.md:430-453` — the Axios instance, interceptor, interfaces, and endpoint functions are presented as the `api.ts` pattern

`frontend/src/services/api.ts` is now a five-line barrel export. The shared Axios client and timestamp interceptor live in `frontend/src/services/apiClient.ts`; domain interfaces and endpoint functions are split across modules such as `apiCore.ts`, `apiTraders.ts`, and `apiSettings.ts`. Some standalone clients (`apiDataset.ts`, `apiTopicCatalog.ts`, and others) create Axios instances directly, so the blanket statement “Always run `normalizeUtcTimestampsInPlace` on response data” is not an accurate description of all current services.

What it should be instead: point ordinary API work to `apiClient.ts` and the matching domain service module. State whether new services must use the shared client, and explicitly list the exceptional long-timeout/streaming clients if they are intentional. That rule should have one source of truth rather than a synthetic example.

### 8. The WebSocket envelope claim is too absolute

> `agents.md:354` — `All WebSocket messages are JSON with type and data fields`
>
> `agents.md:366` — common types include `ping`/`pong`

The server replies to ping with `{"type": "pong"}` and no `data` field (`backend/api/websocket.py:818-819`). Many current server messages also carry an optional `topic` field.

What it should be instead: document the envelope as `type` required, `data` optional, and `topic` optional; then name the no-data control messages. Preserve the current shared-connection rule separately.

### 9. Worker control and settings hot reload are overgeneralized

> `agents.md:350` — `Workers read control state from DB each iteration.`
>
> `agents.md:512` — `The 180+ settings in config.py can be changed at runtime ... Workers re-read configuration each loop iteration from DB.`

The worker implementations are heterogeneous. Some read `worker_control`, some use workflow-specific state, some periodically call `apply_runtime_settings_overrides()`, and `workers/host.py` applies runtime overrides during plane initialization. The settings API only invokes the in-process singleton reload for selected filter/event changes (`backend/api/routes_settings.py:2616-2622`). The current code does not support the blanket claim that every worker re-reads every setting on every iteration.

What it should be instead: distinguish worker control, workflow settings, and process-local `config.settings`. Require the agent to inspect the target worker's loop and reload path before assuming a setting is hot. A settings change is complete only after its persistence path and every consuming process's refresh behavior are verified.

### 10. The opening completion standard is impossible to check

> `agents.md:3` — `Every function must be complete and correct on first write.`

“Correct on first write” is not an observable completion criterion and discourages the inspect-test-iterate loop that the document needs in a financial codebase. “Production-quality” and “reference-grade” are also high-demand labels without a local done condition.

What it should be instead: keep the no-stub financial guardrail, but define completion with observable evidence: trace the affected money/data path, update all callers/contracts, run the smallest relevant tests or build, inspect the diff, and report anything not verified. For execution, ledger, settlement, credential, permission, or migration changes, require explicit failure-path and rollback evidence.

### 11. Compatibility/deletion guidance is duplicated and lacks boundary conditions

> `agents.md:11` — `Clean cut, not backwards compatible.`
>
> `agents.md:17` — `Delete, don't deprecate.`
>
> `agents.md:525` — `Do not re-export removed symbols for backwards compatibility`
>
> `agents.md:529` — `Do not preserve old function signatures ... update all callers`

These repeat one meaning in four places, violating the single-source-of-truth rule. The absolute “not backwards compatible” instruction also fails to distinguish an internal rename from a persisted database schema, API contract, order record, or externally consumed payload. That missing branch can cause destructive changes.

What it should be instead: keep one authoritative rule. For internal-only changes, update all callers and delete the old path in the same change. For persisted or external boundaries, require an explicit cutover/migration decision and verification before removing the old contract. If this project intentionally permits breaking those boundaries, state who authorizes the cutover and what proves it complete.

### 12. The negative rule list should be converted to positive targets where possible

> `agents.md:518-530` — `## What NOT to Do`

Several items repeat earlier rules and repeatedly activate the forbidden behavior. Examples that can be stated positively are: “change documentation only for code you touch,” “use direct code for one-off operations,” “log warnings/errors and material state transitions,” and “update all callers in the same change.”

What it should be instead: co-locate each positive rule with the branch where it applies. Retain explicit prohibitions only for hard guardrails that are clearer as bans, especially incomplete implementations and strategy-slug-specific tests.

### 13. Proactive cleanup has an unbounded scope

> `agents.md:534` — `If removing something would require changes across many files, do it; follow the dependency chain to completion.`

This can turn a narrow financial fix into an unrelated multi-file refactor and gives no completion boundary for “dead code you encounter.” It also competes with the document's anti-speculation intent.

What it should be instead: remove dead code made obsolete by the requested change and follow its direct dependency chain to a checked endpoint. Record unrelated cleanup candidates rather than expanding the task automatically. If broad cleanup is genuinely required for correctness, state the dependency evidence and verify the expanded path.

## Factual verification record

| Claim checked | Result | Current-tree evidence |
|---|---|---|
| Frontend uses React 18 and Recharts 3.7 | **Stale** | `frontend/package.json:55,60` declares React `^19.2.4` and Recharts `^2.15.4`; `package-lock.json` resolves the same major versions. |
| Backend versions are FastAPI 0.109, Uvicorn 0.27, Pydantic 2.5 | **Stale as dependency declarations** | `backend/requirements.txt:1-2,13` now requires FastAPI `>=0.115.0`, Uvicorn `>=0.31.1`, and Pydantic `>=2.7.0`. The exact installed versions were not established. |
| `StrategyType` is an enum in `models/opportunity.py` | **Stale** | `backend/models/opportunity.py` defines `MispricingType` and `ROIType`, but no `StrategyType`; `BaseStrategy.strategy_type` is a string slug at `base.py:564`. |
| The documented `BaseStrategy` methods exist | **Partly confirmed, incomplete** | `detect`, `detect_sync`, `detect_async`, `configure`, `evaluate`, `should_exit`, `calculate_risk_score`, and `create_opportunity` exist, but the document omits the preferred `detect_sync` branch and copies outdated decision fields. |
| Opportunity fields and stable/unique IDs work as described | **Confirmed by source** | The listed core fields exist in `backend/models/opportunity.py:95-174`; `stable_id` excludes the timestamp and `id` adds `detected_at.timestamp()` at lines 188-197. |
| ORM timestamps should be UTC-aware | **Confirmed; example is stale** | `backend/models/database.py:108-159` defines `UTCDateTime` and aware `_utcnow`; model columns use that pattern. |
| Every migration should define `_column_names` and have a no-op downgrade | **Stale** | 26/169 migration files define local `_column_names`; recent migrations use `backend/alembic_helpers.py` and implement downgrade operations. |
| WebSocket hook is a module-level singleton shared by consumers | **Confirmed** | `frontend/src/hooks/useWebSocket.ts` holds module-level connection/listener state and tears it down only after the last consumer unmounts. |
| Event bus is a singleton with wildcard subscribers and fire-and-forget callbacks | **Confirmed** | `backend/services/event_bus.py:15-102` implements `*`, schedules callbacks with `asyncio.create_task`, retains task references, and exports `event_bus`. |
| Every WebSocket message contains `type` and `data` | **Stale** | `backend/api/websocket.py:818-819` sends `{"type": "pong"}` without `data`; `topic` is also used by several messages. |
| `frontend/src/services/api.ts` owns the Axios client and types | **Stale** | `api.ts` is a barrel; `apiClient.ts` owns the shared client/interceptor, while types and calls are split by domain. |

## What to keep untouched

The following content is specific, behavior-changing, and either verified or valuable as a durable invariant. A later reorganization may move it behind a stronger pointer, but should not dilute its meaning:

- `Core Principles > No speculative abstractions` (`agents.md:13`).
- `Domain Model > What Homerun Does` (`agents.md:113-122`).
- The core `Opportunity` financial, risk, market, position, AI-analysis, and strategy-context field summary (`agents.md:151-163`), after avoiding the unverified phrase “every UI component.”
- The instruction to construct opportunities through `self.create_opportunity(...)` (`agents.md:184`), which is present in the live base class.
- `Event Bus` semantics (`agents.md:368-381`), verified against the current implementation.
- `Structured Logging`'s contextual-keyword and `exc_info` guidance (`agents.md:383-394`).
- `Common Gotchas` #1, #3, #4, #5, and #7 (`agents.md:502,506,508,510,514`).
- The UTC-aware intent of `Common Gotchas` #2 (`agents.md:504`); fix the contradictory ORM example rather than weakening this rule.
- The shared-infrastructure, slug-agnostic strategy-test rule (`agents.md:530`).
- The hard guardrail against stubs, TODO implementations, empty exception swallowing, and partial financial code, consolidated into one place.

## What could not be verified

- **Exact installed backend versions:** `backend/requirements.txt` contains ranges for several core packages, and the current `backend/uv.lock` contains only lock metadata rather than resolved packages. No Python environment or running container was queried, so only declared constraints were verified.
- **Committed baseline:** the repository already had a large dirty working tree, including modified `backend/config.py`, `backend/models/database.py`, several workers, and `backend/uv.lock`, plus untracked migrations. The findings describe the current working tree, not necessarily `HEAD` or the official upstream repository.
- **Migration runtime behavior:** no PostgreSQL instance or Alembic upgrade/downgrade was run. Helper usage and migration policy were checked statically only.
- **Runtime hot reload across all process planes:** representative control/reload paths were traced, but every worker and deployment topology was not executed. The blanket claim is unsupported; the exact behavior remains worker- and process-specific.
- **Deployment boundary:** the claim that Homerun is always single-user and locally deployed was not verified against Docker/Nginx/runtime bindings or production configuration.
- **Live trading and profitability behavior:** no external APIs, wallet, credentials, orders, fills, or settlement paths were exercised. Source presence is not operational or financial proof.
- **Volatile counts:** “30+ strategies,” “50+ components,” and “180+ settings” were not exhaustively recounted because these are cheap-to-query, high-drift facts that should not be cached in the always-loaded document.
- **Universal consumption claims:** “Every strategy produces” and “every UI component consumes” `Opportunity` were not exhaustively proven. The model is clearly central, but those universal quantifiers should be narrowed unless a complete dependency analysis establishes them.

Static verification was appropriate for this analysis-only task. No build or test result is claimed.
