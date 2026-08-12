# Task: Review and improve agents.md using the writing-for-agents skill

## Context

This project already has `agents.md` (534 lines) at the repo root. It is NOT empty and NOT a draft. It contains real, hard-won domain knowledge: architecture layout, exact dependency versions, strategy base-class contract, DB retry patterns, worker loop pattern, frontend conventions, naming rules, and 7 gotchas.

## Your task

1. Invoke the `writing-for-agents` skill first. Follow its guidance on how agent-facing docs should be written.
2. Read `agents.md` in full.
3. Evaluate it against the skill's criteria.
4. Write your findings to `.piercode/agents-md-findings.md`.

## Hard constraints

- DO NOT modify `agents.md` in this task. Analysis only.
- DO NOT rewrite it from scratch. The existing domain knowledge is the valuable part; preserve it.
- DO NOT create, delete, or modify any other file except `.piercode/agents-md-findings.md`.

## What the findings file must contain

- **Verdict**: does the current doc meet the skill's standard? Where does it fall short?
- **Concrete defects**: quote the specific line or section, say what is wrong, say what it should be instead. No vague advice.
- **What to keep untouched**: name the sections that are already good, so a later edit does not destroy them.
- **What you could not verify**: anything you were unsure about, including whether the codebase still matches what the doc claims.

## Verification before you finish

Spot-check at least 3 factual claims in `agents.md` against the actual code (e.g. does `BaseStrategy` really expose those methods? do the versions in the table match the real dependency files?). Report any claim that is now stale.
