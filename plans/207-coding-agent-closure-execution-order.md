# Plan 207: Coding-agent closure execution order

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 203
>
> **Priority:** Coordination only
>
> **Scope:** define the minimal execution order for Plans 203–206 so the closure pass stays qualification-first and avoids redundant implementation work.

## Execution order

Use the following order:

```text
1. Plan 204 — run the live/manual matrix and capture evidence
2. Plan 206 — only if Plan 204 finds a real Eggpool defect
3. Plan 203 — verify all closure acceptance criteria are satisfied
4. Plan 205 — update plan statuses/evidence and close Plan 198 last
```

Plan 206 is conditional. If Plan 204 is clean, skip corrective implementation and proceed directly to Plan 203 closure verification and Plan 205 documentation closure.

Do not begin by refactoring the codebase.

---

# Recommended handoff batching

## Batch 1 — qualification

- record exact client/Eggpool versions;
- exercise Codex managed config/catalog;
- exercise Codex text/tool loop;
- exercise OpenCode config/text/tool loop;
- identify actual compaction behavior;
- exercise managed config lifecycle fixtures;
- exercise `eggpool status` states;
- commit one concise secret-free qualification record if repository convention supports it.

## Batch 2 — conditional correction

Only if needed:

- add deterministic reproduction;
- implement narrow fix;
- rerun affected live case;
- rerun focused tests.

## Batch 3 — final gates

Run full formatting, clippy, no-default-feature checks, workspace tests, release build, and `git diff --check`.

Confirm current CI succeeds on final implementation commit.

## Batch 4 — evidence closure

- update Plans 199–202 with implementation/qualification evidence;
- close Plan 198 last;
- update only genuinely stale user/developer docs;
- leave intentional deferrals explicit.

---

# Stop conditions

Stop and create a new roadmap instead of stretching this closure pass if qualification shows a current supported client fundamentally requires any of:

- Responses WebSocket proxying;
- persistent server conversation state;
- broad OpenCodex-private backend APIs;
- server-side execution of coding-agent tools;
- a major routing redesign;
- a new production runtime/dependency stack.

Those would be new architectural work, not closure corrections.

---

# Completion

This coordination plan is complete when Plans 203–205 are closed, Plan 206 is either explicitly not needed or completed, and no remaining closure TODO is ambiguous about ownership or next action.
