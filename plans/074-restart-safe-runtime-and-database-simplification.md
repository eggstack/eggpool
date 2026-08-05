# Plan 074 — Restart-Safe Runtime and Database Simplification

Date: 2026-08-04
Status: complete
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Depends on:

- `plans/071-attempt-scoped-failure-classification-and-effects.md`
- `plans/072-upstream-dispatch-retry-and-response-isolation.md`
- `plans/073-bounded-backoff-and-router-self-healing.md`

Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`

## Purpose

Remove runtime and database recovery behavior that can falsely reclaim healthy work, continue after an untrustworthy SQLite state, or depend on bounded diagnostic history for correctness.

EggPool is a single-host LAN/SBC service normally supervised by systemd. For indeterminate database or process-local invariant failures, a clean fail-readiness and restart path is safer and simpler than preserving a partially understood process state through multiple overlapping recovery controllers.

This plan must simplify existing machinery only after preserving the durable request/attempt/reservation and runtime ownership invariants established by prior plans.

## Confirmed defects and risks

### 1. Age-only stale cleanup can reclaim healthy streams

The active-process stale request finalizer uses a pending-row age threshold related to the upstream read timeout. Read timeout is an idle/operation bound, not a maximum stream lifetime.

A healthy stream can remain active longer than the threshold and be marked interrupted, have its reservation released, and decrement active accounting while it is still delivering data.

### 2. Startup integrity checking can fail open

SQLite `PRAGMA quick_check` failures or exceptions are logged in some startup paths without necessarily preventing traffic admission. A process that cannot establish database integrity must not accept requests whose correctness depends on durable selection/finalization.

### 3. Transaction ownership can cross task boundaries

Transaction context is represented through task context and supports child-task inheritance/piggyback behavior. Context inheritance does not prove that the transaction-owning task waits for every child operation before commit.

A child database operation and parent commit can race. Transaction ownership should be one task, one lock, one commit/rollback boundary.

### 4. SQLite lock classification is too dependent on exception class name

SQLite lock/busy failures commonly arrive as `OperationalError` with a code or message such as `database is locked`. Class-name-only classification can treat them as generic operational faults and choose the wrong recovery behavior.

### 5. Startup reconciliation has overlapping or misleading entry points

`reconcile_startup_state()` implies that it repairs durable state but currently performs only limited observation/counting, while `_crash_recovery()` owns the actual repair path.

Parallel or misleading recovery APIs increase the chance that a future call site invokes the wrong one.

### 6. In-process database recovery is complex for the deployment model

Current machinery includes lifecycle/admission flags, connection epochs, ambiguous-operation descriptors, retained reconciliation records, bounded retries, replacement connection verification, and a recovery controller.

Some of this was added to prevent false readiness, but continuing the same process after an indeterminate SQLite commit/rollback or invalidated connection is not necessarily safer than exiting and allowing systemd plus startup reconciliation to restore a known state.

### 7. Finalization deduplication consults bounded history

Completed finalization history is capped at 64 entries. A duplicate terminal submission after eviction can start a new process-local lifecycle. A duplicate found in history returns a synthetic completed job with an empty/default structured result rather than the original convergence result.

Durable request/attempt/reservation status must provide permanent idempotency. History is diagnostic only.

### 8. Finalization and coordinator cleanup ownership overlap

Selected request terminal finalization, retryable attempt cleanup, post-commit claim compensation, supervisor retry, startup stale repair, runtime leases, and coordinator progress registries overlap.

The result is multiple resumable component trackers and several places that decide whether durable and runtime work is complete. This is more machinery than a local service needs and creates more failure states to reconcile.

## Scope

Primary files and subsystems:

- startup/lifespan recovery in `src/eggpool/app.py`;
- runtime task registration and stale finalizer;
- `src/eggpool/db/connection.py`;
- `src/eggpool/db/recovery.py` and reconciliation helpers;
- request/attempt/reservation repositories;
- `src/eggpool/request/finalizer.py`;
- `src/eggpool/request/attempt_finalizer.py`;
- `src/eggpool/request/finalization_job.py`;
- retained coordinator cleanup and claim compensation in `request/coordinator.py`;
- systemd/service exit behavior;
- readiness state and startup integrity checks.

## Explicitly out of scope

- automatic SQLite repair or salvage;
- restoring a corrupted database in place;
- distributed transactions;
- an external durable queue or workflow engine;
- a second database;
- multi-process shared-memory ownership;
- general cross-event-loop support;
- changing request accounting semantics;
- removing durable request/attempt/reservation rows;
- removing startup crash reconciliation;
- retrying a client request after process restart;
- adding watchdog-specific proprietary protocols;
- adding fault-injection CI, soak gates, or retained evidence bundles.

## Governing decisions

1. Active work is never reclaimed based only on wall-clock row age.
2. Startup crash recovery may repair rows left by a previous process because no live owner from that process can remain.
3. Database integrity failure is startup-fatal and readiness-fatal.
4. A transaction is owned by exactly one asyncio task.
5. Child tasks do not inherit permission to execute inside a parent's transaction.
6. SQLite error classification uses stable SQLite codes where available and bounded message fallback where necessary.
7. An indeterminate commit/rollback/connection state closes admission and terminates the worker after bounded cleanup.
8. systemd restart plus startup reconciliation is the final recovery layer.
9. Durable attempt identity is the permanent idempotency boundary.
10. Bounded history is diagnostics only.
11. One selected attempt has one retained terminal owner. Component progress lives on that owner.
12. Startup repair operates on durable pending state; normal request paths do not depend on a periodic stale sweep.
13. Simplification must delete mechanisms, not replace them with a new framework.

## Phase A — Remove age-only active-process stream reclamation

### Required changes

1. Identify all periodic calls to `finalize_stale_requests_once()` or equivalent active-process stale-request mutation.
2. Stop invoking the mutation as a normal periodic task.
3. Preserve startup crash recovery for rows left pending by a previous process instance.
4. If an in-process safety check remains, it may only report diagnostics unless it can prove abandonment using an explicit owner-generation/process fact.
5. Do not use:
   - request row age alone;
   - `upstream.read_timeout_s` as maximum request lifetime;
   - missing recent usage as proof of abandonment.
6. A retained finalization job that exhausts its bounded retry may leave durable pending work for startup repair and mark readiness degraded according to the existing contract; it must not race an age sweep.
7. Remove or rename configuration/documentation implying a maximum stream lifetime when none exists.
8. Preserve exact startup accounting repair for active counts and zero-cost reservations.

### Acceptance criteria

- A stream active longer than `read_timeout_s` is not finalized by a periodic task.
- Startup after simulated process death repairs the same durable pending row.
- No active-process task decrements runtime ownership without an explicit terminal owner or proven previous-process abandonment.
- Stale diagnostics remain bounded and non-mutating if retained.
- No absolute stream lifetime timer is introduced.

## Phase B — Make startup integrity and readiness fail closed

### Required changes

1. Run the existing SQLite integrity/schema checks before request admission.
2. Treat any non-`ok` `quick_check` result as fatal startup failure.
3. Treat exceptions while performing the integrity check as fatal unless the database is intentionally absent and the normal initialization path is creating it.
4. Do not automatically delete, replace, vacuum, or repair a suspect database.
5. Keep readiness false throughout migration, integrity check, startup crash recovery, backoff hydration, and required initial writable probe.
6. On failure:
   - log one clear bounded reason;
   - close opened database resources;
   - exit nonzero so systemd can apply its restart policy;
   - avoid starting background writers/tasks.
7. If an operator chooses to move/delete the database, that remains an explicit manual action.

### Acceptance criteria

- `quick_check != ok` prevents server readiness and request dispatch.
- an integrity-check exception cannot be logged and ignored.
- background tasks do not start after failed integrity validation.
- a healthy fresh database starts normally.
- no auto-repair or destructive fallback is added.

## Phase C — Enforce one-task transaction ownership

### Required changes

1. Record the owning asyncio task when entering the outermost transaction.
2. Nested repository calls from the same task may reuse the transaction according to the existing nested contract.
3. A different task, including a `create_task()` child with inherited context, must not execute as though it owns that transaction.
4. On cross-task access:
   - raise a typed local database invariant error before SQL execution;
   - do not wait on or piggyback the parent's transaction;
   - do not penalize a provider.
5. Code that currently shields only a child database coroutine must instead shield/retain the whole transaction-owning operation.
6. Parent commit/rollback must occur only after all same-task operations complete.
7. Remove compatibility behavior whose sole purpose is child-task transaction inheritance.
8. Keep the canonical single-loop model; do not generalize to multi-loop transactions.

### Acceptance criteria

- an inherited child task cannot issue SQL inside the parent's transaction;
- same-task nested repository calls continue to work;
- cancellation shields the transaction owner rather than spawning a hidden child transaction user;
- commit cannot race a child SQL operation;
- the error is local and does not affect account health.

## Phase D — Correct SQLite error classification and failover behavior

### Required changes

1. Prefer `sqlite_errorcode`/extended code when available.
2. Recognize lock/busy conditions by code and bounded message fallback.
3. Distinguish at least:
   - busy/locked, retryable within the current bounded transaction acquisition policy;
   - disk I/O/full/read-only, local service failure;
   - corruption/not-a-database, startup/process-fatal;
   - interrupted/cancelled, request cancellation/local failure;
   - indeterminate commit/rollback/connection invalidation, process-fatal after admission closes.
4. Do not classify by exception class name alone.
5. Busy retry remains bounded by the existing SQLite timeout; do not add another exponential retry controller.
6. A database failure never creates provider backoff.
7. Expose one stable local error category in bounded diagnostics.

### Acceptance criteria

- `OperationalError: database is locked` is recognized as lock/busy.
- corruption and disk failures cannot be treated as provider errors.
- busy handling does not exceed the configured SQLite timeout through nested retry layers.
- indeterminate connection state closes admission.

## Phase E — Replace in-process indeterminate recovery with restart-safe failure

### Required changes

Review the current `DatabaseRecoveryController` and retain only behavior needed for clean closure and diagnostics.

For an indeterminate commit, rollback, invalidated connection, or failed connection replacement:

1. atomically close read/write admission;
2. set readiness false;
3. stop accepting new dispatch persistence;
4. allow already-retained terminal owners a short bounded drain only when their database connection remains trustworthy;
5. close writers and the database connection;
6. terminate the worker nonzero;
7. rely on systemd to restart;
8. run startup integrity and crash reconciliation before reopening readiness.

Remove process-local machinery that attempts to reopen admission after ambiguous state if it is no longer needed:

- replacement connection publication while requests remain in process;
- multiple connection epochs used only for same-process recovery;
- pending ambiguity buffers whose only consumer is same-process recovery;
- controller states that cannot produce a stronger guarantee than restart;
- duplicate readiness/admission flags.

Retain a compact durable ambiguity descriptor only if startup reconciliation requires it to decide whether an operation committed. Prefer existing durable request/attempt/reservation identities and idempotent queries over a new table.

### Bounded retry exception

A simple connection-open failure before any request admission may retain a small bounded startup retry if it helps transient filesystem readiness. This must occur before the server is ready and must not preserve partial request state.

### Acceptance criteria

- an indeterminate runtime transaction cannot return the same process to ready state.
- admission and readiness close before process exit.
- systemd restart reaches startup reconciliation and can restore service on a healthy database.
- ordinary SQLite busy errors do not unnecessarily terminate the process.
- same-process recovery state and buffers are materially reduced.
- no external queue or new durable recovery schema is added.

## Phase F — Make durable identity the finalization idempotency boundary

### Required changes

1. `RequestFinalizationSupervisor.register_or_get()` may deduplicate active jobs in memory by `(proxy_request_id, attempt_id)`.
2. After active job retirement, a duplicate submission must query or rely on the durable attempt/request/reservation terminal state rather than bounded history.
3. Bounded history remains available for diagnostics but is not consulted to decide whether durable/runtime work is complete.
4. Remove the synthetic completed job that returns default/empty `FinalizationResult` from history.
5. When durable state is already terminal:
   - return a truthful durable result;
   - converge only still-owned runtime components from the current retained lease;
   - do not replay usage, health, account state, active count, quota, or probe components already marked complete.
6. Durable idempotency must remain correct after history eviction, history clearing, or process restart.
7. Terminal outcome conflicts must be checked against durable status/attempt facts, not only an in-memory prior payload representation.
8. Keep history bounded and scalar-only.

### Acceptance criteria

- clearing the 64-entry history does not change duplicate terminal behavior.
- a late duplicate cannot start a contradictory second durable transition.
- returned finalization results reflect actual durable/runtime convergence.
- already-terminal durable state can still finish an explicitly owned runtime lease without replay.
- no unbounded completed-job cache is introduced.

## Phase G — Collapse overlapping retained cleanup ownership

### Target contract

For one selected attempt:

1. durable selection creates request/attempt/reservation identity;
2. runtime publication creates one `AttemptRuntimeLease`;
3. one retained terminal owner is registered by attempt identity;
4. a retryable pre-handoff failure submits a terminal attempt outcome and converges the same lease before reselection;
5. request terminal completion/cancellation/error uses the same attempt owner where compatible;
6. one idempotent durable terminal transaction records request/attempt/reservation/usage facts;
7. one component loop converges quota, active count, health/account state, and probe;
8. startup reconciliation repairs durable pending rows after process death.

### Required simplification

Evaluate and remove overlap among:

- `AttemptCleanupProgress`;
- `ClaimCompensationProgress`;
- coordinator attempt-cleanup task registry;
- coordinator claim-compensation task registry;
- `RequestFinalizationJob` runtime progress;
- supervisor retry progress;
- stale active-process finalization.

Preferred result:

- one bounded retained registry for selected-attempt terminal ownership;
- one component-progress record per active attempt;
- one bounded retry scheduler, only for component convergence while the process remains trustworthy;
- one shutdown drain;
- one startup durable repair path.

Post-commit publication failure may remain a distinct pre-dispatch compensation command if it lacks a fully published runtime lease, but it should use the same component marker conventions and be adopted by the same process supervisor rather than a parallel coordinator registry.

### Acceptance criteria

- there is one authoritative process-owned terminal registry.
- selected retry cleanup and request terminal cleanup do not maintain competing progress records.
- post-commit compensation is either folded into that registry or remains one narrowly justified distinct state.
- component completion is represented once.
- capacity rejection remains fail closed before handoff and cannot create detached work.
- startup repair remains available after process exit.
- lines of ownership/state code and number of task registries are materially reduced.

## Phase H — Remove misleading APIs and update operational contract

### Required changes

- Delete or rename `reconcile_startup_state()` if it does not perform the documented repair.
- Keep one startup recovery entry point with explicit return/result semantics.
- Remove duplicate `app.state` mirrors for recovery/finalization authority where active generation/process managers already own the object.
- Update architecture docs and `AGENTS.md` to state:
  - no age-only active sweep;
  - startup integrity is fail closed;
  - runtime indeterminate DB state exits for systemd restart;
  - durable attempt identity owns finalization idempotency;
  - one retained terminal supervisor owns selected attempts.
- Document the expected systemd restart policy without making CI depend on systemd.

## Focused verification

Required representative regressions:

1. healthy stream exceeds read-timeout age and remains active;
2. startup crash recovery repairs a previous-process pending stream row;
3. non-ok quick check prevents admission;
4. integrity exception prevents task startup;
5. cross-task inherited transaction access fails before SQL;
6. same-task nested transaction access succeeds;
7. SQLite locked error follows bounded busy handling;
8. corruption/indeterminate commit closes readiness and requests process exit;
9. restart startup reconciliation restores a simulated ambiguous/pending operation;
10. finalization duplicate remains idempotent after history eviction;
11. duplicate returns truthful result, not a default synthetic result;
12. consolidated retained owner resumes one failed component without replaying completed components;
13. capacity saturation remains bounded/fail closed.

Use direct function/process-boundary tests and real temporary SQLite where required. Do not require an actual systemd host in CI; assert the worker exit/admission contract and keep one optional manual service smoke.

## Verification commands

```bash
uv run ruff format src/eggpool/db src/eggpool/request src/eggpool/app.py tests/
uv run ruff check src/eggpool/db src/eggpool/request src/eggpool/app.py tests/
uv run pyright src/eggpool/db src/eggpool/request src/eggpool/app.py
uv run pytest <affected database/recovery/finalization/stream tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Run the normal repository gate after focused tests. No privileged environment, rootless namespace, long soak, or CI service harness is required.

## Recommended implementation sequence

1. Remove mutating active-process age sweep and add the long-stream regression.
2. make startup integrity fail closed.
3. enforce one-task transaction ownership and correct SQLite error categories.
4. define the runtime fatal database boundary and process-exit contract.
5. simplify/delete same-process recovery layers.
6. make durable identity independent of finalization history.
7. consolidate retained terminal/compensation ownership.
8. remove misleading APIs and authority mirrors.
9. run focused tests and smoke.
10. perform one optional manual systemd restart smoke on representative hardware.

## Plan acceptance criteria

- [x] Healthy active streams cannot be reclaimed by row age/read-timeout threshold.
- [x] Previous-process pending work is repaired at startup.
- [x] Database integrity failure prevents readiness and background task startup.
- [x] Transactions are owned by one asyncio task.
- [x] SQLite lock/corruption/indeterminate errors are classified correctly.
- [x] Runtime indeterminate DB state closes admission and exits for restart.
- [x] Same-process recovery machinery is materially reduced.
- [x] Durable attempt identity remains idempotent without diagnostic history.
- [x] Synthetic completed-history finalization results are removed.
- [x] One authoritative retained terminal registry owns selected attempts.
- [x] Component progress is represented once and resumes without replay.
- [x] Capacity remains bounded and fail closed.
- [x] Startup repair and normal systemd restart restore service on a healthy database.
- [x] Focused tests and smoke pass.
- [x] No auto-repair, external queue, workflow engine, distributed transaction, new database, generalized multi-loop layer, or CI expansion is introduced.

## Rejection conditions

Do not close this plan if:

- request age alone can still mutate an active stream's terminal state;
- quick check failure can log and continue;
- a child task can execute inside a parent's transaction;
- an indeterminate runtime database state can return the process to ready;
- ordinary busy errors always force restart;
- finalization correctness depends on bounded history;
- a duplicate returns a fabricated empty result;
- coordinator and supervisor still own competing selected-attempt component progress without narrow justification;
- simplification introduces another recovery framework;
- CI requires systemd, privileged namespaces, fault campaigns, or long-running tests.

## Definition of done

Plan 074 is complete when normal long streams are never reclaimed by age, startup integrity is fail closed, transaction ownership is single-task, SQLite failures choose bounded busy handling or clean process restart correctly, durable attempt identity provides permanent finalization idempotency, overlapping retained cleanup registries are collapsed, and systemd plus startup reconciliation form the simple final recovery boundary for a local EggPool deployment.
