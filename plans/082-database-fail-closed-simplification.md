# Plan 082 — Database Fail-Closed Simplification

Date: 2026-08-05
Status: ready for implementation
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Depends on:

- `plans/078-runtime-invariant-and-request-boundary-corrections.md`
- `plans/081-terminal-ownership-consolidation.md`

Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Reduce SQLite lifecycle and recovery behavior to the deployment contract EggPool actually supports: one event loop, one supervised worker, SQLite WAL, bounded ordinary lock handling, fail-closed termination after indeterminate or fatal database state, and deterministic startup reconciliation.

The planning baseline contains an explicit database lifecycle state machine, connection epochs, an ambiguous-operation queue, recovery-controller wiring, write-admission events, loop-rebinding compatibility, same-process reconnection states, test fault-injection seams, and startup repair. Some of this is necessary; some is residual from attempts to continue within a process after state became uncertain.

This plan must remove unreachable or contradictory recovery machinery without weakening request durability. It must not replace SQLite, add another database, or create a new repair service.

## Supported database failure policy

### Ordinary recoverable local failures

The following remain bounded request/task-local failures:

- `SQLITE_BUSY`;
- `SQLITE_LOCKED`;
- a configured busy timeout;
- an ordinary statement error with a known rolled-back transaction;
- read-only analytics failure that does not invalidate the primary connection.

These may return a local error, defer nonessential maintenance, or use one existing bounded retry where already documented. They must not penalize providers.

### Fatal or indeterminate failures

The following close write admission and terminate the worker through the existing supervisor policy:

- commit outcome cannot be determined;
- rollback fails or connection remains in an unknown transaction state;
- corruption/not-a-database;
- disk I/O/full/read-only failure affecting correctness writes;
- connection invalidation during a correctness-critical transaction;
- reconciliation identity is ambiguous or internally inconsistent.

The process must not reopen admission on a replacement connection after these failures. Systemd/process supervision restarts EggPool, then startup integrity and crash reconciliation run before readiness.

### Startup failures

Startup migration, integrity, or reconciliation failure keeps readiness closed and exits. Do not add an endless startup retry loop.

## Governing decisions

1. System supervisor restart is part of the supported recovery architecture.
2. Same-process recovery after an indeterminate commit/rollback is removed.
3. Startup reconciliation owns work abandoned by the prior process.
4. Live terminal ownership from Plan 081 owns work in the current process; no age-based live reclamation is introduced.
5. Transaction ownership remains task-explicit and fail closed.
6. A single event loop removes the need to support lock objects crossing production loops.
7. Database diagnostics remain bounded and useful, but no state is retained solely for a recovery path that no longer exists.
8. Test fault injection should remain only where it protects the supported fail-closed/startup behavior.
9. Read-only stats isolation may remain when configured with two worker connections; minimum-footprint mode may use one.
10. No ORM, connection pool, database daemon, or generalized transaction journal is added.

## Workstream A — Inventory and classify current recovery machinery

Before editing, map every production call site for:

- `DatabaseLifecycleState` values;
- `_invalidated`, lifecycle transitions, and admission flags;
- `_recovery_controller` and `DatabaseRecoveryController`;
- `_recovering_lock`;
- `_ambiguous_operations` and `AmbiguousDatabaseOperation`;
- `wait_for_writes_admitted()` and `_writes_admitted_event`;
- replacement `connect(admit=False)` paths;
- connection epoch/recovery count consumers;
- fatal handler wiring;
- startup integrity/reconciliation;
- background writer handling during invalidation;
- `_refresh_idle_connection_lock()` and private `asyncio.Lock._loop` access.

Classify each item as:

1. required for ordinary transaction ownership;
2. required for fail-closed diagnostics;
3. required for startup reconciliation;
4. obsolete same-process recovery;
5. test-only compatibility residue.

Record the inventory in the implementation commit or plan completion notes. Do not delete until all consumers are identified.

## Workstream B — Collapse the runtime lifecycle

Use the smallest state representation that preserves supported behavior.

Preferred runtime states:

- `disconnected`;
- `connecting`;
- `ready`;
- `failed_closed`;
- `shutting_down`.

Intermediate `invalidating` may remain as a transient implementation detail only if required for atomic close. `invalidated`, `recovering`, and `reconciling` should not represent states from which ordinary production admission can later return to `ready` in the same process.

Required behavior on fatal failure:

1. atomically stop admitting new reads/writes that depend on the primary connection;
2. detach/close the connection best effort;
3. retain bounded failure class/stage diagnostics;
4. invoke the existing fatal worker handler exactly once;
5. reject subsequent database operations immediately;
6. do not start a same-process reconnect task.

If read-only stats can remain safe after primary failure, prefer simplicity: close general admission and restart the worker rather than maintaining a partial-service mode.

## Workstream C — Remove same-process recovery controller paths

After call-site proof, remove or reduce:

- recovery attempt/backoff configuration that no longer affects supported behavior;
- recovery-controller task/single-flight logic;
- replacement-connection reconciliation in the live process;
- recovery-only transaction context;
- write-admission wait event used to pause writers for recovery;
- recovery count/epoch fields used only by same-process reopening;
- ambiguous-operation queue used only to reconcile into a replacement live connection.

Do not remove durable identities from request/attempt/reservation rows. Startup repair still needs them.

If `database.recovery.*` is already shipped configuration, preserve parsing for one release only if needed:

- accept old fields;
- emit one bounded deprecation warning during config validation;
- ignore/remove semantics that claim same-process recovery;
- document the fail-closed restart policy;
- avoid a general config migration subsystem.

If backwards compatibility is not required by release policy, remove the nested config in the next intentional breaking release and document it in the plan completion notes.

## Workstream D — Simplify transaction ambiguity handling

`db.transaction()` must still distinguish:

- work raised before commit, followed by confirmed rollback;
- commit succeeded;
- commit failed with known no-commit/confirmed rollback;
- commit/rollback outcome indeterminate.

Required outcomes:

- confirmed rollback: raise a local database error; connection may remain ready if SQLite state is known clean;
- indeterminate outcome: transition to `failed_closed`, invoke fatal handler, and raise a typed fatal database error;
- no caller continues request routing or runtime publication after an indeterminate outcome.

The runtime no longer needs to retain an in-memory ambiguous operation for same-process reconciliation. The caller must ensure durable rows carry stable identities before the commit boundary so startup reconciliation can inspect them after restart.

Audit dispatch, finalization, backoff, rehash, and maintenance transactions. Any operation whose startup repair cannot identify its durable intent must either:

- become idempotent with an existing stable identity; or
- fail before creating non-reconstructable runtime ownership.

Do not add a new journal table solely to preserve the removed in-memory queue.

## Workstream E — Remove cross-loop lock compatibility

After Plan 078 enforces one runtime thread, remove production reliance on private `asyncio.Lock._loop` and lock recreation.

Required behavior:

- database and long-lived locks are created and used on the canonical event loop;
- tests that reuse one `Database` instance across multiple event loops must construct a fresh instance per loop or use a supported async fixture scope;
- a true foreign-loop access fails clearly rather than replacing a lock behind active code.

Do not retain multi-loop complexity for `TestClient` convenience. Update tests instead.

## Workstream F — Background writers and readiness

Audit routing trace, metrics coalescer, dispatch writer, backup, maintenance, and readiness probe behavior on fatal database state.

Required behavior:

- correctness-critical writers receive immediate typed failure once admission closes;
- nonessential writers stop/drop bounded diagnostic work and do not spin;
- readiness becomes false from cached process state without attempting a write;
- the fatal handler initiates worker termination;
- no background task waits indefinitely for writes to become re-admitted.

The optional readiness writable probe may remain, but it must detect failure and trigger the same fail-closed policy rather than own recovery.

## Workstream G — Startup reconciliation remains authoritative

Preserve and simplify startup order:

1. open SQLite;
2. configure pragmas;
3. run migrations;
4. perform integrity/read-write validation as currently required;
5. reconcile prior-process pending requests/attempts/reservations using durable identities;
6. hydrate routing/backoff/quarantine state;
7. install the initial generation;
8. admit readiness.

Startup repair must be idempotent. Unknown terminal status, missing identity, or contradictory rows remains fail closed.

Remove documentation that suggests runtime recovery reopens the database in process.

## Focused verification

Required representative cases:

1. ordinary `SQLITE_BUSY`/`LOCKED` remains a bounded local failure and does not invoke fatal shutdown;
2. confirmed rollback leaves the connection usable;
3. indeterminate commit closes admission and invokes the fatal handler once;
4. rollback failure closes admission and invokes the fatal handler once;
5. subsequent operations fail immediately after `failed_closed`;
6. no same-process reconnect/recovery task starts;
7. background writers stop/drop bounded work rather than waiting for re-admission;
8. readiness becomes false without a write on the endpoint path;
9. process restart plus startup reconciliation repairs a representative pending dispatch/finalization;
10. contradictory startup state fails readiness;
11. foreign-loop database reuse fails clearly or tests construct a fresh instance;
12. current request/stream smoke paths remain green.

Use existing fault-injection seams only where they remain relevant. Delete tests that solely verify removed recovery states.

Suggested commands:

```bash
uv run ruff format src/eggpool/db src/eggpool/database_recovery.py src/eggpool/app.py src/eggpool/background tests/unit tests/integration
uv run ruff check src/eggpool/db src/eggpool/database_recovery.py src/eggpool/app.py src/eggpool/background tests/unit tests/integration
uv run pyright src/eggpool/db src/eggpool/database_recovery.py src/eggpool/app.py src/eggpool/background
uv run pytest <affected database/recovery/startup/background tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Adjust paths if the recovery controller has a different module name. Delete the module only after all imports are removed.

## Acceptance criteria

- [ ] Ordinary lock/busy failures remain bounded and local.
- [ ] Indeterminate commit/rollback state transitions the worker to fail closed.
- [ ] No production path reopens database admission in the same process after fatal uncertainty.
- [ ] System supervisor restart plus startup reconciliation is the documented recovery path.
- [ ] Same-process recovery controller/backoff/wait machinery is removed or reduced to compatibility parsing only.
- [ ] Transaction ownership remains task-explicit.
- [ ] Cross-loop lock recreation and private `_loop` inspection are removed from production behavior.
- [ ] Background writers stop cleanly on failed-closed state and do not spin/wait indefinitely.
- [ ] Startup reconciliation remains idempotent and fail closed on contradiction.
- [ ] Tests for removed states are deleted, while focused fail-closed/startup tests pass.
- [ ] Smoke passes.
- [ ] No new database, journal, ORM, worker, or CI job is introduced.

## Rejection conditions

Do not close this plan if:

- a fatal/indeterminate transaction can return the database to ready in the same process;
- callers continue runtime publication after commit ambiguity;
- background tasks wait for an admission event that can never be set;
- startup repair loses stable durable identities;
- cross-loop support remains through private asyncio internals;
- code complexity is moved into a new recovery abstraction rather than deleted;
- provider health is affected by local SQLite failure.

## Implementation sequence for GPT-5.6 Luna

1. Complete the recovery-mechanism inventory before deleting code.
2. Write/adjust focused tests for the supported failure policy.
3. Collapse runtime lifecycle and fatal transition.
4. Remove same-process recovery controller and admission-wait paths.
5. Simplify transaction ambiguity handling around startup-repair identities.
6. Remove cross-loop lock compatibility and fix tests.
7. Reconcile background writers/readiness.
8. Verify startup reconciliation.
9. Update configuration/docs and delete obsolete tests/modules.
10. Run focused checks, then smoke, and record exact outcomes.