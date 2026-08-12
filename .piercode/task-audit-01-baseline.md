# Task: Settlement chain audit — Batch 1: independent baseline verification

## Why this task exists

This is a financial system heading for live trading. A prior agent (you, in earlier sessions) built an online settlement ledger across 11 phases and recorded extensive self-reported evidence in `progress.md`.

**Self-reported evidence is not evidence.** This task independently re-verifies those claims against the real system. You are auditing your own past work — treat every claim as unproven until you re-run it yourself.

## Hard constraints

- **READ-ONLY on the application.** Do not modify any source file, config, migration, or database row.
- Do not start, stop, or restart any container or worker.
- Do not commit, stash, checkout, or otherwise alter git state.
- The ONLY file you may create is `.piercode/audit-01-baseline.md`.
- Do not read anything outside this project directory.
- If a check requires a write to perform, DO NOT perform it. Record it as unverifiable and say why.

## Claims to verify (each independently)

For each claim below: re-run the check yourself, record the actual observed output, and mark VERIFIED / REFUTED / UNVERIFIABLE.

1. **Test suite state.** `progress.md` claims `83 passed, 1 warning in 169.16s` for the extended trading-chain regression and `7 passed` for the manual-execution specifics. Re-run those exact suites. Report the real counts. If any test fails, quote the failure.

2. **Migration head.** Claimed head is `202608100002`, applied additively, with legacy accounts defaulting to v1/legacy. Verify the actual head in the migration scripts and that only one head exists.

3. **Main shadow account untouched.** Claimed balance `945.550990`, PnL `3.054009`, 16 trades. Verify against the live database, read-only.

4. **Isolated proof account.** Claimed to end at `1042.308725` after the proof run, 3 trades. Verify.

5. **Two historical stuck orders remain untouched.** Claimed still `submitted`, empty token, non-canonical direction, no `simulation_ledger` entry. Verify.

6. **Idempotency.** Claimed a fixed request id replays to identical IDs with zero economic change. Do NOT re-run the replay against live data. Instead, verify by reading the idempotency implementation and its tests, and state whether the tests actually prove the claim or only prove a narrower property.

7. **No traceback / no failures in runtime logs** since deployment. Verify read-only.

## Output

Write `.piercode/audit-01-baseline.md` using this structure:

### Verdict
One line: does the independent check support the self-reported baseline, or not.

### Per-claim results
For each of the 7 claims:

> **Claim N**: <restate briefly>
> **Method**: exact command or query you ran
> **Observed**: real output (trim to what matters, do not paraphrase numbers)
> **Result**: VERIFIED / REFUTED / UNVERIFIABLE
> **Note**: any discrepancy, however small

### Discrepancies
Anything where observed reality differs from `progress.md`, even trivially. Do not smooth these over.

### What this batch does NOT establish
Be explicit. Passing tests do not prove correctness of design. State what remains unaudited.

### Unverifiable
Checks you could not perform read-only, and what would be required.

## Standard

You are auditing yourself. The failure mode to avoid is confirming your own prior work because you remember writing it. Re-run everything. If a number differs even in the last decimal, report it.
