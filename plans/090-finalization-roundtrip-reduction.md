# Plan 090 — Finalization Round-Trip Reduction

Date: 2026-08-07
Status: ready for implementation
Parent roadmap: `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
Depends on: none
Planning baseline: `d6c49dea5ed800bfcd22d95fe8c7943a29590125`

## Purpose

Reduce aiosqlite/SQLite calls on the common request-finalization path without weakening idempotency, retained terminal ownership, crash repair, or fail-closed database behavior.

The current finalizer performs correctness mutations and then reads request, attempt, and reservation state back inside the same transaction to prove convergence. Those reads are useful for duplicate/replayed finalization, but they are redundant when the current call just transitioned the relevant row and SQLite can return the resulting state directly.

## Required reading

- `plans/086-sbc-routing-and-storage-efficiency-roadmap.md`
- `AGENTS.md`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/finalization_job.py`
- request/attempt/reservation repositories in `src/eggpool/db/`
- `src/eggpool/db/connection.py`
- migrations defining request, attempt, and reservation terminal columns
- existing finalizer, idempotency, duplicate-submission, cancellation, startup-repair, and database-failure tests

## Current common-path shape

The baseline finalizer approximately performs:

1. conditional request finalization;
2. request SELECT to prove terminal state;
3. conditional attempt finalization;
4. reservation release;
5. attempt SELECT to prove terminal state;
6. reservation-status SELECT to prove terminal state;
7. commit;
8. post-commit runtime convergence/analytics.

The goal is to make the normal first terminalization prove steps 2, 5, and 6 from the mutation results themselves. Reads remain valid and required on no-transition/idempotent paths.

## Governing design

Repository mutation methods should return enough bounded state to answer whether the durable component is terminal after the mutation.

Preferred implementation:

- `UPDATE ... WHERE <nonterminal condition> ... RETURNING <minimal terminal fields>`;
- return a small typed result or existing primitive that distinguishes `transitioned` from `no row transitioned`;
- when a row transitioned, derive terminal convergence from the returned row with no follow-up SELECT;
- when no row transitioned, issue the existing focused SELECT to determine whether the component was already terminal or remains incomplete;
- keep all three correctness components in the existing transaction.

Do not use `changes()` or connection-global rowcount state if it is less explicit than `RETURNING`.

## Workstream A — Define minimal repository return contracts

Inspect:

- `RequestRepository.finalize_if_pending()`;
- `AttemptRepository.finalize_if_incomplete()`;
- `ReservationRepository.release()`;
- their callers outside `RequestFinalizer`.

Choose the smallest backwards-compatible migration of return values.

Preferred return information:

- request: whether transitioned and resulting terminal status;
- attempt: whether transitioned and enough information to prove `completed_at IS NOT NULL`;
- reservation: whether transitioned and resulting status.

If changing a widely used method would cause broad churn, add a narrowly named `..._returning()` variant in the repository rather than forcing unrelated callers to consume a larger result. Do not introduce a generic repository result framework.

## Workstream B — Request finalization fast path

Refactor the request mutation so:

1. if this call transitions `pending -> terminal`, set `request_terminal = True` directly from the mutation result;
2. do not call `get_by_id()` on that path;
3. if no row transitions, perform the existing read and preserve `DurableTerminalConflictError` / duplicate identity semantics.

Do not change cost precedence, usage normalization, compression/cache diagnostic preparation, or error redaction in this plan.

## Workstream C — Attempt finalization fast path

Refactor the attempt mutation similarly:

- transitioned attempt => `attempt_terminal = True` without `get_by_id()`;
- no transition => perform the existing read to determine whether the attempt was already completed or remains incomplete.

Preserve first-terminal-data/idempotency behavior.

## Workstream D — Reservation release fast path

Refactor reservation release so:

- successful transition to `released` proves `reservation_terminal = True` without a status SELECT;
- no transition falls back to the existing status read and accepts the same existing terminal statuses (`released`, `expired`, or whatever the repository currently defines canonically).

Do not broaden accepted terminal statuses merely to simplify the query.

## Workstream E — Transaction and error semantics

Keep:

- the existing one correctness transaction;
- all diagnostic serialization outside `BEGIN IMMEDIATE` where already implemented;
- fail-closed handling of ambiguous commit/rollback states;
- best-effort account event enrichment after correctness commit;
- metrics coalescer emission after successful transition;
- retained finalization job retry semantics.

Do not split request/attempt/reservation finalization into separate commits.

## Workstream F — Focused regression tests

Required deterministic cases:

1. first successful finalization transitions all three durable components with no convergence SELECT for components just transitioned;
2. duplicate same-outcome finalization performs fallback read(s), reports durable convergence, and does not double-count usage/analytics;
3. conflicting already-terminal request still raises the existing conflict error;
4. request already terminal but attempt incomplete causes the attempt to converge correctly;
5. request/attempt terminal but reservation still active causes reservation convergence correctly;
6. expired reservation is still recognized as terminal on the fallback path;
7. cancellation/interrupted/upstream-error outcomes preserve current terminal status mapping;
8. a repository/SQLite failure still leaves `retryable`/retained-owner semantics truthful;
9. no runtime active/quota/health convergence occurs before durable commit;
10. database commit ambiguity still takes the existing fail-closed path.

Instrument repository methods or database call counters in the focused test to prove the common first-finalization path no longer executes the three redundant SELECTs. Keep this test local; do not create a global performance counter.

## Workstream G — Optional adjacent micro-cleanups

Only while touching the same code:

- reuse already-known terminal values rather than re-deriving them;
- keep repository SQL parameter construction straightforward;
- remove comments that describe SELECT-after-write as mandatory once it is no longer true.

Do not redesign `FinalizationData`, cost accounting, runtime leases, failure effects, or the database wrapper.

## Verification

Run focused finalizer/repository/database/terminal-owner tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

No benchmark gate is required. The focused test should prove the reduced application-level DB call count deterministically.

## Acceptance criteria

- [ ] First request transition proves `request_terminal` from the mutation result without a follow-up request SELECT.
- [ ] First attempt transition proves `attempt_terminal` from the mutation result without a follow-up attempt SELECT.
- [ ] First reservation release proves `reservation_terminal` from the mutation result without a follow-up status SELECT.
- [ ] No-transition/idempotent paths still read durable state as needed.
- [ ] Conflicting terminal identity detection remains intact.
- [ ] First-terminal data remains authoritative and cannot be overwritten by duplicate finalizers.
- [ ] Request/attempt/reservation changes remain in one correctness transaction.
- [ ] Runtime convergence still occurs only after durable convergence/commit.
- [ ] Ambiguous/fatal database failures retain existing fail-closed behavior.
- [ ] Focused tests prove the common-path DB call reduction.
- [ ] Standard smoke gate passes.

## Rejection conditions

Do not close this plan if:

- idempotent duplicates are assumed terminal without reading durable state when no mutation occurred;
- request/attempt/reservation are split into independent commits;
- database fail-closed behavior is weakened;
- `RETURNING` results are ignored and redundant SELECTs remain on the common path;
- repository API changes cause broad unrelated refactors;
- a benchmark/metrics subsystem is added to demonstrate the optimization;
- cost or failure-classification semantics change.

## Implementation sequence for GPT-5.6 Luna

1. Inventory finalizer repository calls and every external caller of the three mutation methods.
2. Add a focused test that counts/observes common-path convergence reads.
3. Implement minimal `RETURNING`-based repository results, preserving compatibility where useful.
4. Change request convergence to fast-path on transition and fallback-read on no transition.
5. Do the same for attempt and reservation.
6. Run duplicate/conflict/partial-convergence tests.
7. Run database ambiguity/fail-closed tests touched by these methods.
8. Run lint/type/smoke checks.
9. Record exact commands/results and mark complete only after both call reduction and idempotency are proven.