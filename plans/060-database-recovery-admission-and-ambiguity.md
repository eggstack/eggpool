# Plan 060 — Database Recovery Admission and Ambiguity

Date: 2026-07-31
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Predecessor: `plans/059-dispatch-persistence-contract-and-writer-boundary.md`
Planning baseline: `34ea5da2ad1b7674cce338db867cbff498085264`

## Purpose

Make database recovery genuinely fail closed and ensure ambiguous-operation metadata belongs to the transaction that can become ambiguous.

The current recovery implementation can open a replacement connection, mark reads and writes admitted, and set the ready event before schema verification, writable probing, and reconciliation complete. A later verification failure can leave the database object admitted while the recovery controller reports failure. Separately, ambiguous-operation ownership is stored in one shared mutable slot, allowing concurrent tasks waiting for the transaction lock to overwrite or clear each other's descriptors.

This plan corrects those ownership and admission boundaries using the existing `Database` and `DatabaseRecoveryController`. It does not add a durable workflow engine, external queue, second database, or public-internet availability architecture.

## Confirmed defects

### Shared ambiguous-operation slot

Callers set `Database._pending_ambiguous_op` before entering the transaction. Because lock ownership has not yet been acquired:

- Task A can set descriptor A and wait;
- Task B can overwrite it with descriptor B;
- Task A can commit or clear B's descriptor;
- Task B can later become indeterminate with no correct descriptor;
- stale descriptors can survive begin failure, body exception, successful rollback, or injected precommit failure.

The metadata therefore does not describe the transaction that owns the database lock.

### Early admission during recovery

`Database.connect()` currently transitions to ready and admits reads/writes immediately after opening/configuring the connection. The recovery controller then performs schema verification, a writable probe, and reconciliation. Background writers can resume in the gap.

If verification or reconciliation fails, the replacement connection can remain open, the admission event can remain set, and `Database` can disagree with the controller's failed-closed state.

### Reconciliation loses unresolved work

The ambiguous-operation deque is drained before convergence is proven. Conflicts, unknown strategies, exceptions, and boundary placeholders can be counted as resolved. The queue is bounded with silent eviction semantics, so an old correctness-critical ambiguity can disappear without forcing a failed-closed state.

## Scope

Primary files:

- `src/eggpool/db/connection.py`
- `src/eggpool/db/recovery.py`
- transaction call sites in request coordinator/finalizers
- existing database recovery and transaction tests

Potential small supporting changes:

- existing database error classes;
- existing readiness probe/state reporting;
- one small immutable ambiguous-operation dataclass adjustment.

## Explicitly out of scope

- multiple SQLite writer connections;
- high-availability failover;
- distributed recovery;
- an external durable queue;
- a new recovery database/table by default;
- general transaction middleware;
- automatic replay of arbitrary SQL;
- infinite retry loops;
- new readiness endpoints;
- CI fault matrices, soak campaigns, or retained evidence bundles.

## Design decisions

1. Ambiguous-operation metadata is supplied to `transaction()` and becomes active only after that transaction owns the lock.
2. Transaction-local metadata is always cleared in `finally`.
3. Opening a connection and admitting traffic are separate operations.
4. Recovery keeps reads and writes closed until connection setup, schema verification, writable probing, and reconciliation all succeed.
5. A failed recovery attempt closes and discards its replacement connection.
6. Conflicting, unknown, timed-out, or exception-producing reconciliation is unresolved, not successful.
7. The in-memory ambiguity buffer may remain bounded, but overflow must fail closed rather than silently evicting an older operation.
8. Reconciliation does not guess that a process boundary committed. It verifies a durable fact or leaves the operation unresolved.
9. No automatic operation replay is introduced unless the operation already has an existing idempotent convergence function.

## Phase A — Move ambiguity metadata into transaction ownership

### Target API

A narrow API is sufficient:

```python
async with db.transaction(ambiguous_operation=operation):
    ...
```

The argument may be optional for transactions that have no correctness-critical external operation identity.

### Required changes

1. Remove or retire caller-facing mutation of `_pending_ambiguous_op`.
2. Add an optional `ambiguous_operation` parameter to the transaction context manager.
3. Acquire the database transaction lock before installing the descriptor as active transaction state.
4. Store the descriptor in a transaction-owned field that cannot be modified by a waiting task.
5. Clear it in `finally` for all exits:
   - begin failure;
   - body success;
   - body exception;
   - rollback success;
   - rollback failure;
   - commit success;
   - commit ambiguity;
   - cancellation.
6. Record the descriptor only when the commit result is genuinely indeterminate according to the existing database error classification.
7. Do not record ambiguity for a statement failure followed by a confirmed rollback.
8. Update coordinator, request finalizer, and attempt finalizer call sites to pass the descriptor directly.
9. Make strategy and identity construction explicit at each call site. Do not reuse one generic operation ID with different meanings.

### Acceptance criteria

- Two concurrent transactions cannot overwrite each other's descriptors.
- A waiting task cannot clear the active owner's descriptor.
- Confirmed rollback leaves no stale active descriptor.
- Commit ambiguity records the descriptor belonging to that exact transaction.
- Cancellation before lock acquisition records nothing.
- Cancellation after an indeterminate commit records the correct operation once.

## Phase B — Separate connection opening from admission

### Required lifecycle

Use a small state sequence such as:

```text
DISCONNECTED -> CONNECTING -> RECOVERING/CHECKING -> READY
                                      |
                                      +-> FAILED_CLOSED
```

Exact enum names may follow existing code. The important distinction is that a configured open connection is not yet admitted.

### Required changes

1. Refactor `connect()` or add a private open method so recovery can create/configure a connection without setting:
   - `_writes_admitted=True`;
   - `_reads_admitted=True`;
   - the admission/ready event;
   - final ready state.
2. Startup may use the same staged path:
   - open/configure;
   - run required migrations/verification through the existing startup owner;
   - admit once ready.
3. Recovery controller sequence:
   - close/discard invalid connection;
   - open replacement with admission closed;
   - verify schema/version invariants;
   - run one bounded writable probe;
   - reconcile ambiguous operations;
   - atomically mark ready and set the event.
4. Enforce admission inside `transaction()` immediately before database work, not only at higher-level callers.
5. Read paths that require ready state must use the same authoritative state/event. Do not let a stale boolean bypass the controller.
6. A recovery probe may use an explicitly privileged internal transaction path while public writes remain closed. Keep this private to recovery.
7. Do not let periodic background tasks restart merely because a connection object exists.

### Acceptance criteria

- The ready/admission event remains clear through open, schema check, writable probe, and reconciliation.
- Public transactions fail closed while recovery is in progress.
- Recovery's internal writable probe can run without admitting public writes.
- Ready is published once, after all required checks succeed.
- Existing normal startup behavior remains functional.

## Phase C — Close and reset on failed recovery

### Required changes

1. Put replacement-connection ownership inside `_attempt_recovery()` until admission succeeds.
2. On any failure after open:
   - close the replacement connection;
   - clear the database connection reference if it points to the failed replacement;
   - clear read/write admission flags;
   - clear the event;
   - set database and controller state to the same failed-closed state;
   - retain the error for diagnostics without credentials or SQL payloads.
3. Do not return `FAILED_CLOSED` while leaving a usable/admitted connection behind.
4. Ensure a later bounded retry starts from a clean disconnected/failed-closed state.
5. Preserve single-flight recovery ownership. Do not start overlapping replacement attempts.
6. Shutdown must cancel recovery and close any not-yet-admitted replacement connection.

### Acceptance criteria

- Schema verification failure leaves no open admitted connection.
- Writable probe failure leaves the ready event clear.
- Reconciliation failure leaves database and controller states consistent.
- A later recovery attempt can succeed without inheriting stale flags or connection references.
- Shutdown during recovery closes the candidate connection.

## Phase D — Retain unresolved ambiguity

### Required changes

1. Replace destructive `drain_ambiguous_operations()` semantics with one of:
   - snapshot then acknowledge individually after convergence; or
   - pop one, reconcile, and requeue on unresolved result.
2. Define result categories clearly:
   - `committed/converged`;
   - `absent/converged` only when absence is an explicitly valid terminal fact for that strategy;
   - `unresolved_conflict`;
   - `unresolved_unknown_strategy`;
   - `unresolved_error`;
   - `unresolved_timeout`.
3. Unknown strategy is never counted as resolved.
4. Reconciler exception is never counted as resolved.
5. Conflict is never counted as resolved merely because it was observed.
6. Recovery cannot transition to ready while correctness-critical operations remain unresolved.
7. Preserve bounded memory:
   - keep a hard capacity;
   - before adding beyond capacity, set failed closed and reject the new write path;
   - do not silently evict the oldest operation.
8. Include operation strategy and stable non-secret identity in diagnostics. Do not store request bodies, API keys, or provider responses.
9. If process restart loses in-memory ambiguity, rely on existing startup stale-row reconciliation; do not add a new table in this phase unless implementation proves a specific unrecoverable gap.

### Acceptance criteria

- A conflict remains in the unresolved set.
- An unknown strategy blocks ready and remains observable.
- A reconciler exception does not delete the operation.
- Successfully converged operations are acknowledged and removed exactly once.
- Capacity overflow fails closed instead of evicting an older record.

## Phase E — Replace unconditional boundary success

### Required changes

1. Review `_reconcile_boundary()` and every strategy routed to it.
2. Remove unconditional `"committed"` results.
3. For each retained boundary strategy, choose one:
   - verify a durable row/state using an existing repository;
   - invoke an existing idempotent convergence command;
   - classify it as unresolved and keep recovery closed.
4. Delete boundary strategies that no longer have callers.
5. Do not implement generic SQL replay or arbitrary callback serialization.
6. Keep reconciliation bounded by the existing recovery timeout/retry policy.

### Acceptance criteria

- No strategy reports committed without inspecting a durable fact or completing an idempotent convergence operation.
- Obsolete strategies are removed rather than supported indefinitely.
- Unverifiable boundaries remain unresolved and prevent false readiness.

## Focused verification

Test budget: normally no more than six focused cases in existing database/recovery test files.

Required coverage:

1. Two concurrent transactions with different descriptors preserve the descriptor of the transaction that becomes ambiguous.
2. Body exception plus confirmed rollback clears the descriptor and records no ambiguity.
3. Recovery schema-check failure leaves connection closed, admission flags false, and event clear.
4. Public transaction during recovery is rejected while the private writable probe still runs.
5. Conflict/unknown strategy remains unresolved and prevents ready.
6. Ambiguity-buffer capacity does not silently evict; overflow fails closed.

Use deterministic barriers/events rather than timing sleeps for concurrency. One temporary SQLite database is sufficient. Do not add a repeated crash campaign or process-level soak.

## Implementation sequence

Recommended commits:

1. transaction-owned ambiguity API and call-site migration;
2. staged connection/admission lifecycle;
3. unresolved reconciliation retention and capacity behavior;
4. focused regression coverage and documentation closure.

## Plan acceptance criteria

- [x] Ambiguous-operation descriptors are transaction-scoped after lock acquisition.
- [x] Descriptors are cleared for every non-ambiguous exit.
- [x] Recovery opens a candidate connection without admitting public reads/writes.
- [x] Admission occurs only after schema verification, writable probe, and reconciliation succeed.
- [x] Failed recovery closes/discards the candidate and clears the event.
- [x] Database and controller state cannot disagree about readiness.
- [x] Conflicts, unknown strategies, errors, and timeouts remain unresolved.
- [x] Unresolved correctness-critical operations prevent ready.
- [x] Buffer overflow fails closed rather than silently evicting.
- [x] No strategy reports committed without durable verification or idempotent convergence.
- [x] No durable workflow table, second database, multi-node recovery system, CI matrix, or soak harness is introduced.

## Definition of done

The plan is complete when ambiguity metadata cannot cross transaction ownership, recovery remains closed until all checks and reconciliation succeed, failed attempts leave no admitted replacement connection, unresolved operations are retained, and focused regressions plus the existing smoke suite pass.
