# Database Connection Recovery and Transaction Reconciliation

Date: 2026-07-25
Status: implementation handoff

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/026-process-owned-request-finalization.md`

## Objective

Allow Eggpool to recover safely from an invalidated or indeterminate SQLite connection without requiring a process restart or database deletion. The process must detach a suspect connection, open a replacement connection, reconcile ambiguous idempotent request operations, restore readiness only when safe, and preserve all committed history.

This phase must also close the transaction-body rollback-failure branch that can leave a connected but poisoned SQLite transaction state.

## Safety principle

Fail closed on uncertain transaction outcome, but recover the process automatically. Never reuse an indeterminate connection. Never blindly replay a transaction whose commit may have succeeded. Reconcile using durable identities and state predicates.

## Scope

### In scope

- Process-owned database recovery controller.
- Explicit database connection lifecycle state.
- Single-flight replacement connection creation.
- Readiness integration.
- Ambiguous dispatch/finalization reconciliation.
- Rollback-failure invalidation.
- Bounded retries and escalation.
- Startup and runtime recovery.
- Read-only stats connection coordination.
- Fault injection and consistency audits.
- Operator diagnostics and runbook.

### Out of scope

- Replacing SQLite or aiosqlite.
- Multi-process writer coordination.
- Distributed transactions.
- Automatic database file deletion or restore from backup.
- Replaying arbitrary SQL logs.
- Hiding persistent corruption or schema mismatch behind reconnect loops.

## Workstream A — Define database lifecycle state

Replace the implicit `_conn`/`_invalidated` combination with an explicit state model. Suggested states:

```text
disconnected
  -> connecting
  -> ready
  -> invalidating
  -> invalidated
  -> recovering
  -> reconciling
  -> ready
  -> failed_closed
  -> shutting_down
```

The exact representation may be an enum plus immutable snapshot.

Required facts:

- current state;
- connection generation/epoch;
- invalidation reason class;
- invalidation timestamp;
- recovery attempt count;
- last recovery error class;
- last successful writable probe;
- number of ambiguous operations awaiting reconciliation;
- whether new write transactions are admitted;
- whether read-only stats remain safe.

State transitions must occur under one process-owned synchronization boundary. Arbitrary callers must not call `connect()` concurrently.

## Workstream B — Introduce a recovery controller

Create `DatabaseRecoveryController` or equivalent owned by the process/runtime generation factory, not by individual requests.

Responsibilities:

1. Receive invalidation notification with structured reason.
2. Stop admission of new correctness-critical writes.
3. Mark readiness false.
4. Detach and close the suspect write connection with a bounded timeout.
5. Open a fresh write connection using the same validated configuration.
6. Reapply pragmas and verify schema/migration compatibility.
7. Run read and rollback-only writable probes.
8. Reconcile ambiguous operations.
9. Rebuild or refresh repositories bound to the connection if necessary.
10. Restore write admission and readiness.

The controller must be single-flight. Concurrent requests observing invalidation must join the same recovery attempt rather than each reconnecting.

## Workstream C — Make connection objects generation-aware

Every connection replacement increments an epoch. Long-lived components must not retain an unusable raw `aiosqlite.Connection` across epochs.

Preferred architecture:

- Repositories retain a stable `Database` facade.
- The facade resolves the current connection internally.
- Cursors never escape a connection-access or transaction boundary.
- A transaction captures the connection epoch at `BEGIN` and verifies it has not changed before commit handling.

If any component stores `db.connection` or raw cursors, remove that retention.

Tests must prove that a replaced connection is used by all request repositories, maintenance tasks, readiness probes, background jobs, and dashboard reads that share the write facade.

## Workstream D — Invalidate on rollback failure

The transaction-body exception branch currently attempts rollback and re-raises. Harden it to:

- capture `in_transaction` before rollback;
- attempt rollback with bounded diagnostics;
- verify `in_transaction=False` afterward;
- if rollback raises or state remains true/unknown, invalidate and detach the connection;
- raise a typed `DatabaseRollbackError` or extended commit/recovery error carrying actual facts;
- notify the recovery controller.

The original application exception must remain available as cause/context without being mistaken for a provider-health error.

No caller may continue issuing SQL on a connection after rollback uncertainty.

## Workstream E — Represent ambiguous operations

When commit raises and outcome is indeterminate, create a bounded immutable `AmbiguousDatabaseOperation` before handing control to recovery.

Required fields:

- operation kind: dispatch selection, attempt finalization, request finalization, backoff/quarantine transition, maintenance, or other;
- connection epoch;
- request/attempt/reservation idempotency keys where applicable;
- intended terminal status or state transition;
- precondition facts;
- timestamp;
- safe reconciliation strategy identifier;
- no raw SQL parameters containing secrets or prompt data.

Correctness-critical request operations must supply reconciliation metadata before the commit attempt. Generic maintenance may use a simpler restart/retry policy only when idempotent and explicitly documented.

## Workstream F — Reconcile dispatch selection ambiguity

For an ambiguous dispatch-bundle commit, inspect the replacement connection for:

- request row existence and status;
- attempt row identity;
- reservation row identity and active state;
- matching account/model/provider facts;
- duplicate or partial rows.

Required outcomes:

- `committed_complete`: all expected rows exist consistently; publish/reconstruct selected attempt exactly once.
- `rolled_back_absent`: no bundle rows exist; caller may retry selection under a new intent identity according to policy.
- `partial_invariant_violation`: some but not all correctness rows exist; fail closed and invoke bounded repair/escalation.
- `conflicting_identity`: rows exist but do not match intended facts; fail closed.

Do not insert missing rows piecemeal unless a separately specified repair transaction proves that doing so is safe and idempotent.

Runtime publication must follow the reconciled durable fact. A committed bundle may require runtime ownership reconstruction or compensation, coordinated with Plan 026.

## Workstream G — Reconcile finalization ambiguity

For ambiguous request/attempt/reservation finalization:

- If request is terminal with compatible status and the attempt/reservation are terminal/released, treat durable finalization as complete and continue runtime release.
- If all rows remain pending/active, rerun the idempotent finalization transaction.
- If request is terminal but attempt/reservation remain incomplete, run one repair transaction that completes dependent rows only after validating the request terminal identity.
- If terminal state conflicts with intended identity, fail closed and expose an invariant error.

Usage, cost, failure effects, and quarantine evidence must not be double-applied. Reconciliation must distinguish durable row completion from process-local post-commit updates.

## Workstream H — Coordinate read-only stats and background tasks

When the write connection is invalidated:

- Read-only dashboard/stat connections may remain available if the database file is readable and snapshot semantics are safe.
- `/readyz` must report write unready.
- `/healthz` may report process alive with degraded database status according to existing conventions.
- Background writers must pause or join recovery.
- Read-only tasks must not trigger migrations or write probes.
- Maintenance, backup, model-info persistence, checkpointing, routing traces, metrics flushes, and finalization retries must use a common admission gate.

After replacement, background tasks resume without duplicate startup instances.

## Workstream I — Recovery retry and escalation policy

Configuration should include bounded recovery controls, for example:

```toml
[database.recovery]
enabled = true
max_attempts = 5
initial_backoff_ms = 100
max_backoff_ms = 5000
reconciliation_timeout_s = 30
fail_process_on_exhaustion = false
```

Requirements:

- Backoff bounded and optionally jittered.
- One active recovery attempt.
- Persistent schema mismatch, corruption, permission failure, or disk-full condition does not spin indefinitely.
- Exhaustion leaves state `failed_closed`, readiness false, and precise diagnostics.
- Optional configured process exit may be supported, but restart is an operator policy, not required for normal recoverable invalidation.
- No automatic database deletion.

## Workstream J — Startup recovery

At startup:

- Detect stale invalidation/ambiguous-operation records if persisted.
- Open a fresh connection normally.
- Run migrations before request admission.
- Reconcile stale pending requests/reservations through Plan 026 startup reconciliation.
- Validate database integrity using bounded checks appropriate for startup policy.
- Do not retain process-local connection epochs across restart as authoritative.

If ambiguity metadata is process-local only, the durable state scan must still repair known request invariants.

## Workstream K — Observability and operations

Expose a sanitized snapshot containing:

- lifecycle state and epoch;
- invalidation count by reason;
- reconnect attempts/successes/failures;
- time to recover;
- ambiguous operations by kind/outcome;
- rollback failures;
- last writable probe;
- admission-gate status;
- active waiters;
- failed-closed reason category.

Add an operator runbook covering:

- normal automatic recovery;
- disk full;
- permissions changed;
- database locked by external process;
- corruption/integrity failure;
- failed migration;
- backup/restore path;
- why deleting the database is not a normal recovery step.

No diagnostic may expose filesystem contents beyond configured database path policy, SQL parameters, API keys, prompts, or response bodies.

## Workstream L — Tests

Suggested files:

- `tests/unit/test_plan_027_database_lifecycle.py`
- `tests/unit/test_plan_027_recovery_singleflight.py`
- `tests/unit/test_plan_027_rollback_failure_invalidation.py`
- `tests/unit/test_plan_027_dispatch_reconciliation.py`
- `tests/unit/test_plan_027_finalization_reconciliation.py`
- `tests/integration/test_plan_027_runtime_reconnect.py`
- `tests/integration/test_plan_027_background_task_gate.py`
- `tests/integration/test_plan_027_readiness_recovery.py`
- `tests/soak/test_plan_027_repeated_connection_recovery.py`

Deterministic fault cases:

1. Begin failure with connection still usable.
2. Body write failure and successful rollback.
3. Body write failure and rollback exception.
4. Commit exception with `in_transaction=True`, successful rollback.
5. Commit exception with `in_transaction=False` and ambiguous outcome.
6. Commit exception with unknown transaction state.
7. Invalidation close timeout/failure.
8. Replacement connect failure then success.
9. Replacement connect exhaustion.
10. Schema/migration mismatch.
11. Writable probe failure.
12. Ambiguous dispatch committed complete.
13. Ambiguous dispatch absent.
14. Ambiguous dispatch partial rows.
15. Ambiguous finalization complete.
16. Ambiguous finalization absent/pending.
17. Ambiguous finalization conflicting terminal state.
18. Concurrent requests joining single recovery.
19. Shutdown during recovery.
20. Rehash/runtime generation interaction during recovery.

Every test must assert exact lifecycle state and readiness.

## Acceptance criteria

### Connection safety

- [ ] Indeterminate connections are detached and never reused.
- [ ] Rollback failure invalidates the connection.
- [ ] All repositories resolve the replacement connection through a stable facade.
- [ ] Raw cursors/connections do not escape their ownership boundary.
- [ ] Connection epoch changes are observable and test-pinned.

### Automatic recovery

- [ ] Recovery is process-owned and single-flight.
- [ ] Concurrent request waiters join one recovery attempt.
- [ ] Readiness becomes false before new correctness writes are admitted.
- [ ] A replacement connection is configured and probed before readiness returns.
- [ ] Recoverable invalidation returns to ready without process restart.
- [ ] No database file deletion is performed.
- [ ] Exhausted recovery remains safely failed closed with precise diagnostics.

### Reconciliation

- [ ] Ambiguous dispatch complete/absent/partial/conflicting outcomes are distinguished exactly.
- [ ] Ambiguous finalization complete/pending/partial/conflicting outcomes are distinguished exactly.
- [ ] No ambiguous transaction is blindly replayed.
- [ ] Usage, cost, health, quarantine, reservations, and active counts are not double-applied.
- [ ] Partial invariant violations do not silently pass.
- [ ] Plan 026 runtime ownership follows reconciled durable state.

### Service integration

- [ ] Background writers pause or join recovery.
- [ ] Read-only stats may continue only under documented safe conditions.
- [ ] Readiness and health expose distinct database states.
- [ ] Rehash/shutdown cannot create a second recovery controller or lose ownership.
- [ ] Startup repairs stale request invariants before readiness.

### Fault and soak verification

- [ ] All twenty deterministic fault cases pass on Python 3.11 and 3.12.
- [ ] Repeated invalidation/recovery soak completes without thread, task, connection, or file-descriptor growth.
- [ ] Database consistency audit passes after every recovery cycle.
- [ ] An unsupported-thinking request followed by induced commit ambiguity recovers and unrelated traffic resumes automatically.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.

## Closure evidence

The exact-head artifact must show at least 100 repeated recoverable invalidation cycles, deterministic outcomes for each commit/rollback branch, consistency-audit results, readiness transition history, and proof that subsequent successful requests require neither restart nor database deletion. Update this plan to completed only after that artifact is committed.
