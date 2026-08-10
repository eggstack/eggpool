# Plan 095 — Database Rollback Ownership

Date: 2026-08-10
Status: planned
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Close the transaction-ownership ambiguity around `Database.safe_rollback()` without weakening EggPool's fail-closed database semantics or adding more transaction machinery.

The preferred outcome is deletion if the public helper is unused in production. If production callers exist, make the helper obey the same single-owner/single-connection contract as every other database operation so one asyncio task can never rollback another task's active transaction.

## Relevant code

Primary file:

- `src/eggpool/db/connection.py`
  - `Database._safe_rollback()`
  - `Database.safe_rollback()`
  - `Database.transaction()`
  - `Database._connection_access()`
  - `Database._current_task_owns_transaction()`
  - `Database._require_transaction_owner()`
  - transaction lifecycle/ownership ContextVars and failure diagnostics.

Also inspect all production/test call sites for:

- `.safe_rollback(`
- `._safe_rollback(`
- transaction cleanup paths that catch `DatabaseCommitError`, `DatabaseRollbackError`, `DatabaseConnectionInvalidatedError`, or `DatabaseTransactionOwnershipError`.

## Problem statement

EggPool intentionally serializes SQL through one shared aiosqlite connection. The outer transaction owns `_connection_lock`, and transaction ownership is tied to the asyncio task that issued `BEGIN IMMEDIATE`. This prevents child or unrelated tasks from executing SQL inside another task's commit boundary.

`safe_rollback()` currently exposes an exceptional cleanup path outside that normal discipline. A public cleanup helper that can call `conn.rollback()` must not bypass ownership or lock serialization. Otherwise an unrelated task can observe the shared connection as `in_transaction` and issue rollback against state owned by another task.

Even if no production caller currently triggers this, retaining an unsafe public helper is a future correctness footgun.

## Goals

1. Prove whether `safe_rollback()` is used in production.
2. Delete it if it is only historical/test scaffolding.
3. If retained, make rollback behavior ownership-safe and deadlock-safe.
4. Preserve the existing internal rollback path used by `transaction()` when commit or body execution fails.
5. Preserve fail-closed behavior when commit/rollback outcome is indeterminate.
6. Remove only transaction markers/state proven unused by the audit.

## Non-goals

- redesigning the `Database` state machine;
- introducing nested savepoints;
- adding connection pooling;
- adding automatic same-process reconnect after failed-closed state;
- changing WAL/synchronous defaults;
- weakening transaction ownership checks to accommodate tests;
- adding generic transaction middleware;
- changing startup crash reconciliation.

## Workstream A — Call-site proof

Search the repository for all `safe_rollback` and `_safe_rollback` references and classify each as:

- production runtime;
- startup/reload lifecycle;
- CLI/tooling;
- test-only;
- dead/unreachable.

Record the result in this plan when closing it.

Decision rule:

- if `Database.safe_rollback()` has no supported production caller, remove the public method and migrate/remove any tests that exist solely to exercise it;
- if a supported caller exists, retain a narrowly specified API and make its ownership semantics explicit.

Do not preserve an unsafe method solely for backwards compatibility unless it is part of an actual supported external Python API contract.

## Workstream B — Ownership-safe retained behavior

If retention is required, the public helper must satisfy all of these cases:

### Case 1: current task owns the active transaction

The owner may request rollback through the existing transaction-owned path. It must not reacquire `_connection_lock`, because it already owns it.

### Case 2: another task owns the active transaction

The helper must not call `conn.rollback()` and must not wait while pretending it can clean up the other task's transaction. It should either:

- raise `DatabaseTransactionOwnershipError`; or
- return an explicit failure result if the existing API contract requires boolean status.

Prefer a typed exception for misuse unless a real production caller requires the boolean contract.

### Case 3: no transaction is active

A no-op rollback may report success without issuing SQLite work, or the helper may be removed entirely if this is its only supported use.

### Case 4: no current owner exists but connection state is ambiguous

If the connection indicates an active transaction without a matching EggPool owner because a prior commit/rollback outcome is indeterminate, do not opportunistically rollback and continue. Follow the existing fail-closed ownership: invalidate the connection / surface the database lifecycle failure so supervised restart and startup reconciliation remain the repair boundary.

Do not create a second recovery path.

## Workstream C — Internal transaction rollback remains authoritative

`Database.transaction()` owns the canonical body/commit failure cleanup sequence. Verify that the plan does not alter:

- pre-commit failure injection behavior;
- commit exception classification;
- `in_transaction` observation around rollback;
- rollback success/failure diagnostics;
- connection invalidation after indeterminate outcomes;
- fatal handler notification;
- nested same-task transaction semantics, if currently supported;
- child-task transaction ownership rejection.

If `safe_rollback()` is removed, `_safe_rollback()` may remain as a private transaction implementation detail where required.

## Workstream D — Prune proven-unused ownership state only

The audit may reveal historical state such as `_transaction_depth` or compatibility markers that are initialized but not read by production logic.

For each candidate:

1. search all code/tests/docs;
2. confirm no behavior or diagnostic endpoint consumes it;
3. delete only if its removal does not obscure the authoritative owner/lifecycle model;
4. update stale comments that describe removed ownership mechanisms.

Do not combine this plan with broad `connection.py` refactoring. Plan 099 owns general archaeology cleanup.

## Workstream E — Focused regression tests

Tests must cover the actual concurrency invariant, preferably with deterministic events rather than sleeps:

1. task A opens a transaction and blocks before completion;
2. task B attempts the public rollback path, if retained;
3. task B cannot rollback task A's transaction;
4. task A can still commit/rollback according to its own path;
5. child task inheriting ContextVars cannot gain ownership;
6. owner rollback failure still transitions to the existing failed-closed behavior when correctness is unprovable;
7. if `safe_rollback()` is deleted, existing production flows and smoke tests prove no supported caller depended on it.

Do not add a stress loop with hundreds/thousands of iterations; one deterministic ownership schedule is enough.

## Documentation

Update only if public/internal contracts change:

- `AGENTS.md` database invariants/gotchas;
- database architecture deep dive;
- comments/docstrings in `connection.py`.

Do not expose test fault-injection details in user documentation.

## Verification

Run focused database ownership/lifecycle tests, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

No live provider access is required.

## Acceptance criteria

- [ ] Every production/test call site of `Database.safe_rollback()` and `_safe_rollback()` is classified before code changes are made.
- [ ] If `Database.safe_rollback()` is unused in supported production paths, it is deleted rather than retained as historical API surface.
- [ ] If retained, an unrelated asyncio task cannot execute `conn.rollback()` against another task's active transaction.
- [ ] A child task inheriting transaction ContextVars does not acquire transaction ownership.
- [ ] The transaction owner can still complete its canonical rollback path without deadlocking on `_connection_lock`.
- [ ] An ambiguous SQLite transaction state is not repaired opportunistically in-process; existing failed-closed/restart ownership remains authoritative.
- [ ] Commit/rollback failure diagnostics and fatal notification semantics remain intact.
- [ ] Any removed transaction marker/state is proven unused by repository-wide search and focused tests.
- [ ] No new connection pool, savepoint layer, retry loop, or database recovery service is introduced.
- [ ] Focused deterministic ownership tests pass.
- [ ] Existing smoke gate passes.

## Rejection conditions

Reject the implementation if:

- a non-owner task can issue rollback while another task owns the transaction;
- ownership safety is implemented by simply serializing all rollback calls behind a new global lock while preserving ambiguous recovery semantics;
- the code silently rolls back an indeterminate transaction and continues serving;
- tests weaken `_require_transaction_owner()` or ContextVar inheritance protection;
- transaction cleanup becomes dependent on timing sleeps;
- the patch refactors unrelated repository/query code or changes SQLite durability settings.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, this plan, `AGENTS.md`, `connection.py`, and database ownership tests.
2. Search all `safe_rollback` and `_safe_rollback` call sites and record the classification.
3. Choose delete-vs-retain from actual production use, not hypothetical compatibility.
4. Implement the smallest ownership-safe change.
5. Remove only ownership state proven dead as a direct consequence.
6. Add deterministic two-task regression coverage.
7. Run focused database tests.
8. Run the ordinary smoke/lint/type gate.
9. Update this plan with the implementation commit, call-site result, and exact verification commands/results.
10. Stop; leave broader `connection.py` cleanup for Plan 099.
