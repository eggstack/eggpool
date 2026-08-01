# Plan 065 — Terminal Ownership, Recovery State, and Small Regression Closure

Date: 2026-08-01
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Corrective predecessors:

- `plans/060-database-recovery-admission-and-ambiguity.md`
- `plans/061-terminal-convergence-and-reconciliation.md`
- `plans/063-exact-version-update-command.md`
- `plans/064-quota-and-sqlite-hotpath-reduction.md`

Planning baseline: `94c6555eba6f2ebfcc86712b5aeabb041825fade`

## Purpose

Close the small but correctness-relevant defects found during review of the implementation of Plans 058–064. The preceding work materially improved dispatch persistence, recovery admission, terminal reconciliation, stale accounting, exact-version updates, and long-running quota/SQLite behavior. This plan does not reopen those designs or introduce another lifecycle framework.

The remaining work is bounded:

1. terminal jobs that exhaust their retry age remain in the active supervisor registry and retain operational references;
2. timer-driven retries are not reflected accurately in retry counters;
3. capacity saturation returns a detached rejected job rather than rejecting before ownership transfer;
4. `RequestFinalizer.finalize()` still returns one ambiguous boolean, causing the job layer to infer reservation and durable convergence facts;
5. the legacy `FinalizationRetryQueue` is still constructed and periodically drained in production despite the supervisor being the intended sole retry owner;
6. successful recovery reopens database admission without transitioning the `Database` lifecycle state back to `READY`;
7. the out-of-order quota-window rebuild prunes against the late observation timestamp instead of the newest observation timestamp;
8. bare `eggpool update` returns `Already up to date.` before printing the current/latest versions, changing the established operator output;
9. the parent roadmap and predecessor plan closure metadata overstate completion before these residuals are corrected.

The design center remains a private single-process SBC/LAN deployment. Correctness and bounded resource ownership matter; production-grade workflow orchestration does not.

## Scope

Primary runtime files:

- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/finalization_queue.py`, only to remove or isolate legacy production wiring
- `src/eggpool/generation_factory.py`
- `src/eggpool/runtime_tasks.py`
- runtime-generation dataclasses/builders only where the legacy queue is currently carried
- `src/eggpool/db/connection.py`
- `src/eggpool/db/recovery.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/cli_full.py`

Focused test files:

- `tests/unit/test_request_finalization_state_machine.py`
- existing finalizer/convergence tests
- `tests/unit/test_database_recovery_singleflight.py`
- `tests/unit/test_quota.py`
- `tests/unit/test_update.py`
- existing runtime-task or generation-factory tests only if queue wiring changes require an assertion update

Planning/documentation closure:

- `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
- brief corrective notes in Plans 061 and 064 when implementation completes
- architecture/agent documentation only where production ownership statements would otherwise remain inaccurate

## Explicitly out of scope

- a new finalization supervisor;
- a database-backed finalization queue;
- a durable workflow engine or saga framework;
- unbounded retry or retry across process loss;
- changing upstream retry policy;
- changing request, attempt, or reservation schema;
- new migrations;
- general database lifecycle refactoring outside recovery admission;
- replacing the quota deque with a sorted tree or third-party container;
- changing exact-version update installation semantics;
- new CI jobs, matrices, coverage thresholds, soak tests, timing gates, evidence bundles, or plan-numbered test suites;
- broad cleanup of every compatibility class unrelated to the active production path.

## Governing decisions

1. `RequestFinalizationSupervisor` remains the only automatic in-process terminal retry owner.
2. Retry exhaustion retires operational ownership. It does not leave a failed job occupying active capacity indefinitely.
3. Durable finalization returns explicit durable facts; the job layer must not fabricate reservation convergence from one transition boolean.
4. Capacity rejection happens before ownership transfer and is represented by an exception, not a detached pseudo-job.
5. The legacy queue is removed from production construction and scheduling when no real producer remains. Keeping an unused periodic compatibility service is not required for backward compatibility.
6. Database recovery admission and lifecycle state change together through one small database-owned method rather than repeated controller-side private-field mutation.
7. The quota slow path remains a bounded rebuild. Only its time anchor changes.
8. Bare update behavior retains the live latest lookup and restores its established current/latest reporting; no new updater abstraction is needed.
9. Verification stays focused and deterministic. No real-time retry sleeps longer than a few milliseconds and no live PyPI request is permitted in tests.

## Phase A — Retire exhausted finalization jobs cleanly

### Confirmed defect

`RequestFinalizationSupervisor._schedule_retry()` marks an over-age job failed and appends a scalar record, but leaves the job in `_active_jobs`. The job remains non-complete, keeps its finalizer/selected/runtime references, counts against capacity, and can never be reconciled by the normal completed-job path.

### Required changes

1. Add one supervisor-owned retirement path for terminal jobs that cannot be retried further. A small private helper is preferred over duplicating cleanup logic.
2. On retry-age exhaustion:
   - mark the job failed with a bounded reason such as `retry_age_exhausted`;
   - remove its `(proxy_request_id, attempt_id)` key from `_active_jobs`;
   - ensure stale retry-heap entries become harmless no-ops when popped;
   - append exactly one scalar record to the existing bounded failed/history diagnostics;
   - release operational references through the existing `release_references()` method;
   - leave durable repair to the existing stale/startup reconciliation safety nets.
3. Do not mark the job `COMPLETED`; exhaustion and convergence are distinct states.
4. Prevent duplicate failed/history records if multiple callbacks or stale heap entries observe the same exhausted job.
5. Ensure active capacity becomes reusable immediately after retirement.
6. Preserve bounded shutdown behavior. Shutdown must not resurrect an exhausted job or wait indefinitely for it.
7. Increment `_retry_count` exactly once for every timer-driven retry invocation, before `job.run()` begins. The initial invocation is attempt count 1 and retry count 0.
8. Keep `attempt_count`, `failure_count`, and `retry_count` semantically distinct:
   - attempt count: every execution of the job state machine;
   - failure count: executions ending in an exception/incomplete failure;
   - retry count: executions initiated by the supervisor retry scheduler after the initial call.

### Acceptance criteria

- A job that exceeds `max_retry_age_s` no longer appears in `active_count`.
- Its operational references are released and only bounded scalar diagnostics remain.
- The vacated capacity accepts a subsequent job.
- A stale retry-heap entry cannot rerun or re-record the retired job.
- A fail-once-then-success job reports `attempt_count == 2`, `failure_count == 1`, and `retry_count == 1`.
- Shutdown remains bounded with an exhausted job.

## Phase B — Reject capacity before terminal ownership transfer

### Confirmed defect

At capacity, `register_or_get()` currently returns a detached `RequestFinalizationJob` marked `_capacity_rejected`. The object is not supervised and raises only when a caller later invokes `run()`. This contradicts the process-owned terminal boundary and makes ownership ambiguous.

### Required changes

1. When active capacity is exhausted, raise `FinalizationCapacityError` directly from `register_or_get()` before constructing or returning a job.
2. Increment the existing saturation counter once for the rejected registration.
3. Update coordinator call sites to handle this explicit pre-ownership rejection according to existing request error semantics.
4. Do not enqueue, detach, or retain terminal work after the exception.
5. Preserve deduplication: an existing matching job is returned even when the registry is otherwise full because no new slot is required.
6. Preserve terminal conflict behavior for an existing mismatched job/history record.
7. Remove `_capacity_rejected` from `RequestFinalizationJob` when no longer needed.

### Acceptance criteria

- A new job at capacity raises `FinalizationCapacityError` synchronously.
- No detached job object is returned.
- `active_count` and registry contents remain unchanged after rejection.
- Matching deduplication still succeeds at capacity.
- Saturation diagnostics increment exactly once.

## Phase C — Return truthful durable convergence facts

### Confirmed defect

`RequestFinalizer.finalize()` still returns `bool transitioned`. The job layer currently converts that boolean into a broad `FinalizationResult`, assuming request, attempt, reservation, and runtime facts share the same transition outcome. An already-terminal request can therefore be durable while its reservation or attempt remains unresolved, and a no-op cannot express which durable components are already converged.

### Required result

Introduce one small frozen result local to durable finalization. Exact naming may follow the existing code, but it must express at least:

```python
@dataclass(frozen=True, slots=True)
class DurableFinalizationResult:
    request_terminal: bool
    request_transitioned: bool
    attempt_terminal: bool
    reservation_terminal: bool
    reservation_transitioned: bool
    retryable: bool = False
    detail: str = ""
```

A computed `durable_converged` property is acceptable. Do not add a general workflow/result hierarchy.

### Required changes

1. Change `RequestFinalizer.finalize()` to return the durable result instead of a boolean.
2. When this call performs the request transition, populate attempt/reservation facts from the actual repository operation results.
3. When the request is already terminal:
   - inspect the associated attempt and reservation through existing repositories or one bounded query;
   - report their actual terminal state;
   - do not assume convergence solely because the request transition was a no-op.
4. Keep the correctness transaction atomic for transitions performed by this invocation.
5. Do not repeat runtime quota/router/health releases inside the durable result merely to fill fields. Runtime ownership remains the job/runtime-lease layer's responsibility.
6. Update `RequestFinalizationJob._execute_durable_finalization()` to map actual durable fields into `FinalizationResult`.
7. The job may advance to runtime release only when the durable components required for that operation are converged.
8. A transient database/recovery condition may be represented as retryable or raised into the existing retry path. Identity conflict and impossible durable state must not hot-loop.
9. Update all direct finalizer consumers and tests. Do not keep a second boolean-returning finalization method.
10. Keep attempt-specific finalization semantics distinct where the existing `AttemptFinalizer` already has its own result type.

### Acceptance criteria

- A newly finalized request reports the actual request, attempt, and reservation transition/terminal facts.
- An already-terminal request with a terminal attempt and released reservation reports durable convergence without being treated as failure.
- An already-terminal request with an active reservation or incomplete attempt does not report full durable convergence.
- Runtime cleanup still executes only through the retained job/runtime lease and remains idempotent.
- No caller interprets `False` as both successful convergence and retry failure.

## Phase D — Remove the legacy retry queue from production ownership

### Investigation checkpoint

Search all production producers and consumers of:

- `FinalizationRetryQueue`;
- `finalization_retry_queue` generation fields;
- `finalization_retry_drain` task registration;
- queue diagnostic exposure.

The current coordinator stores the queue reference, but review found no active production submission path. Confirm this before deletion.

### Required changes when no producer exists

1. Stop constructing `FinalizationRetryQueue` in `RuntimeGenerationFactory`.
2. Remove the queue from generation builder/dataclass plumbing where it serves no other purpose.
3. Remove the `finalization_retry_drain` periodic task from `runtime_tasks.py` and the runtime-task inventory/docs.
4. Remove coordinator assignment of `_finalization_retry_queue` when unused.
5. Delete the queue module if it has no supported public compatibility requirement. If tests or external imports require the symbol, retain a small deprecated compatibility module that is not constructed, scheduled, or described as production ownership.
6. Remove obsolete queue counters/configuration only where unused.
7. Route any discovered real producer directly into `RequestFinalizationSupervisor`; do not keep a second queue, timer, age policy, or retry counter.
8. Preserve stale request finalization and startup recovery as distinct safety nets. They discover/reconcile durable state and are not competing in-process retry schedulers.

### Acceptance criteria

- Production runtime creates one terminal retry owner: `RequestFinalizationSupervisor`.
- No `finalization_retry_drain` periodic task is registered.
- No generation-owned unused queue retains finalizer/router/quota references.
- Any genuine legacy producer submits to the supervisor without independent retry policy.
- Stale and startup recovery remain available.

## Phase E — Make database recovery state and admission coherent

### Confirmed defect

A replacement connection is opened with `admit=False`, which correctly leaves `Database.lifecycle_state == RECOVERING`. After schema verification, writable probing, and reconciliation succeed, the controller sets admission flags/events but does not transition the database object to `READY`. Transactions work, but controller state, database state, and diagnostics disagree.

### Required changes

1. Add one small database-owned method for successful recovery admission, for example `admit_recovered_connection()`.
2. The method must be called only after verification/probing/reconciliation have succeeded.
3. It must update as one logical transition:
   - `_writes_admitted = True`;
   - `_reads_admitted = True`;
   - `_writes_admitted_event.set()`;
   - lifecycle state to `READY`;
   - recovery count/generation timestamp if those remain database-owned.
4. Replace controller-side scattered private-field writes with this method where practical.
5. Add or reuse a corresponding fail-closed helper only if it reduces duplicated state mutation; do not build a general lifecycle transition API.
6. On any recovery failure, the replacement connection remains closed, admission flags remain false, the event remains clear, and lifecycle state remains `FAILED_CLOSED`.
7. Controller `state`, `admission_admitted`, and `db.lifecycle_state` must agree after success and failure.

### Acceptance criteria

- Successful recovery leaves both controller and database lifecycle state at `READY`.
- Reads/writes and the admission event become available only after reconciliation succeeds.
- Failed recovery leaves both controller and database at `FAILED_CLOSED` with no live admitted candidate.
- Recovery count increments once per successful recovery.
- No public request observes `READY` while any ambiguity remains unresolved.

## Phase F — Correct the quota slow-path time anchor

### Confirmed defect

For an out-of-order observation, `QuotaWindow.add_observation()` updates `_last_observation_timestamp` to the maximum timestamp but calls `_rebuild_totals_and_prune(timestamp)` using the older late timestamp. An observation already expired relative to the newest event can therefore remain temporarily counted.

### Required changes

1. After sorting the bounded observation set, prune against the newest known timestamp, not the inserted late timestamp.
2. The anchor should be `_last_observation_timestamp` or the maximum retained/input timestamp in the same clock domain.
3. Preserve the existing cutoff boundary semantics (`timestamp == cutoff` remains included if that is the current contract).
4. Preserve cached total reconstruction and non-negative totals.
5. Do not alter the ordered O(1)-amortized path.
6. Do not add a new data structure or timestamp abstraction.

### Acceptance criteria

- With a 60-second window and newest timestamp 200, a late observation at 120 is excluded immediately.
- A late observation inside the window remains included.
- Cached token/cost totals match the retained deque after rebuild.
- Ordered observations still avoid the rebuild path.

## Phase G — Restore bare update operator output

### Confirmed regression

The latest-update path now checks `is_newer_version()` before printing the current and latest versions. An already-current installation prints only `Already up to date.`, whereas the established command displayed the resolved versions before the conclusion.

### Required changes

1. In the bare `eggpool update` path, print:
   - `Current version: ...`;
   - `Latest version: ...`;
   before deciding whether to install or return already up to date.
2. Preserve the freshness-aware live PyPI lookup and second fetch behavior.
3. Preserve exact-version behavior and output.
4. Preserve `--check` semantics.
5. Do not introduce a shared output renderer or updater class for this change.
6. Tests must mock lookup/install behavior; no live PyPI call.

### Acceptance criteria

- Bare already-current output includes current version, latest version, and `Already up to date.` in that order.
- Bare newer-version output remains unchanged apart from the restored ordering.
- Exact-version update behavior is unchanged.
- No installer or restart occurs for an already-current bare update.

## Phase H — Focused verification and closure metadata

### Automated test budget

Add or modify no more than six focused cases across existing capability-based files. Parameterize where it keeps intent clear.

Required coverage:

1. Retry exhaustion retires the active job, releases references, and frees capacity; include retry-counter semantics in the same test where practical.
2. Capacity saturation raises before returning terminal work.
3. Durable finalization distinguishes already-converged state from incomplete attempt/reservation state.
4. Successful recovery leaves controller and database in `READY`; extend an existing recovery test rather than creating a new file.
5. Out-of-order quota insertion prunes against the newest timestamp.
6. Bare already-current update prints current/latest versions before the conclusion.

When legacy queue production wiring is removed, update one existing runtime task/factory assertion instead of adding a dedicated test suite.

### Required local checks

Run only the affected tests during iteration, then the repository's existing before-push checks:

```bash
uv run pytest tests/unit/test_request_finalization_state_machine.py -q --tb=short --maxfail=1
uv run pytest tests/unit/test_database_recovery_singleflight.py -q --tb=short --maxfail=1
uv run pytest tests/unit/test_quota.py tests/unit/test_update.py -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add a CI job, cancellation matrix, soak test, live-network test, benchmark assertion, or retained evidence artifact.

### Documentation closure

After implementation and verification:

1. Mark Plan 065 complete with a concise implementation closure.
2. Update Plan 058 to `completed` only when every remaining criterion is satisfied.
3. Add a short corrective-follow-up note to Plans 061 and 064 rather than rewriting their historical implementation records.
4. Correct architecture/agent wording so it states only behavior actually present after this pass.
5. Do not create a plan registry or evidence index unless the repository independently adopts one.

## Recommended implementation sequence

1. finalization retry retirement, retry counters, and capacity rejection;
2. durable finalizer result and job mapping;
3. legacy queue production unwiring;
4. recovery admission/lifecycle transition;
5. quota slow-path anchor;
6. bare update output;
7. focused tests and documentation closure.

Keep commits small enough for review, but do not split each one-line correction into a separate ceremonial commit.

## Plan acceptance criteria

- [x] Retry-age exhaustion removes the job from active ownership and releases operational references.
- [x] Retry diagnostics accurately distinguish initial attempts, failures, and timer-driven retries.
- [x] Finalization capacity rejects before returning a detached job.
- [x] Durable finalization reports actual request/attempt/reservation convergence facts.
- [x] Already-terminal durable state is success only when all required durable components are terminal.
- [x] Runtime cleanup remains separately tracked and idempotent.
- [x] The legacy finalization queue and drain task no longer participate in production ownership.
- [x] `RequestFinalizationSupervisor` is the only automatic in-process terminal retry owner.
- [x] Successful recovery leaves controller state, database lifecycle state, and admission flags coherently `READY`.
- [x] Failed or unresolved recovery remains coherently fail-closed.
- [x] Out-of-order quota observations are pruned against the newest observation timestamp.
- [x] Bare update restores current/latest version output without changing exact-version semantics.
- [x] No new migration, runtime dependency, durable queue, workflow framework, CI job, test matrix, soak gate, benchmark gate, or evidence format is introduced.
- [x] Focused regressions and the existing smoke suite pass.

## Corrective follow-up

Plan 066 completed the remaining runtime-ownership and supervisor-diagnostic
corrections without changing Plan 065's historical implementation record.

## Rejection conditions

Do not close this plan if:

- exhausted jobs remain in active capacity or retain operational references;
- capacity saturation still returns untracked terminal work;
- a boolean still carries multiple durable convergence meanings;
- production constructs or schedules a second terminal retry mechanism;
- database diagnostics report `RECOVERING` while public writes are admitted;
- a late expired quota observation remains counted until a later read/write;
- bare update silently changes latest lookup, install, downgrade, or restart behavior;
- verification adds permanent infrastructure disproportionate to these fixes.

## Definition of done

This corrective pass is complete when terminal work has one bounded owner with truthful durable/runtime results and clean exhaustion behavior, recovery state agrees with admission, the quota late-event path expires against the correct time anchor, bare update restores its established output, the parent roadmap accurately reflects closure, and the repository retains its lean SBC/LAN testing and operational posture.

## Implementation closure

Implemented and verified locally on 2026-08-01. The supervisor now retires
exhausted jobs, counts timer retries accurately, rejects capacity before
ownership transfer, and consumes explicit durable convergence facts. The
legacy queue is no longer constructed or scheduled, recovery admission uses a
database-owned `READY` transition, quota rebuilds use the newest timestamp,
and bare updates print current/latest versions before the conclusion.
