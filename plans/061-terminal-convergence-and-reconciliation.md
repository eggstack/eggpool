# Plan 061 — Terminal Convergence and Reconciliation

Date: 2026-07-31
Status: implemented
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Predecessor: `plans/060-database-recovery-admission-and-ambiguity.md`
Planning baseline: `8d9595aaf1ca761f2daea1671a09675e4bfe7ce9`

## Purpose

Make request and attempt finalization converge through one explicit result model, make the existing process-owned supervisor actually retry bounded failures, and ensure database recovery reconciles the correct durable rows using the status vocabulary written by production code.

EggPool currently has several overlapping mechanisms:

- direct request finalization;
- direct attempt finalization;
- `RequestFinalizationSupervisor`;
- `FinalizationRetryQueue`;
- coordinator-retained cleanup registries;
- periodic stale finalization;
- startup ambiguous-operation reconciliation.

These mechanisms do not agree on operation identity, terminal statuses, or what a boolean return means. The result is not a need for another framework. The correct response is to make one existing supervisor the process owner, give finalizers structured convergence results, and route recovery/stale paths through the same semantics.

## Confirmed defects

### Status and row-shape mismatch

- Request finalization writes statuses such as `completed`, `client_error`, `cancelled`, and `error`.
- Recovery checks a different set including `failed` and `client_disconnected`.
- Reconciliation probes an `aiosqlite.Row` using membership logic that is not a reliable column-name check.
- A request can therefore be durably terminal and still be reported as conflicting.

### Request and attempt identity collision

- Request finalization ambiguity uses a request-row identity.
- Attempt finalization records the attempt ID as the operation ID while using the same broad `finalization` strategy.
- The shared reconciler interprets that ID as a request ID and does not inspect `request_attempts` or the reservation.

### Retry ownership is incomplete

- `RequestFinalizationSupervisor` records retry-pending health and re-raises, but no scheduler automatically reruns the job.
- Capacity saturation can return a detached untracked job, contradicting process-owned terminal ownership.
- Configured retry age/backoff values are not used to drive execution.

### Boolean result ambiguity

- `RequestFinalizer.finalize()` can return `False` when the request is already terminal.
- The older retry queue treats `False` as failure, retries it, then drops it.
- A durable commit can succeed before cancellation interrupts runtime quota/active/health cleanup. A later call then sees an already-terminal row but cannot express that runtime cleanup still needs repair.

## Scope

Primary files:

- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalization_queue.py`
- `src/eggpool/db/recovery.py`
- coordinator retained-cleanup call sites only where they consume finalizer results
- existing terminal lifecycle, recovery, and finalization tests

Potential small supporting changes:

- existing status enums/constants;
- existing repository methods for request, attempt, and reservation state;
- shutdown drain wiring.

## Explicitly out of scope

- a new database-backed work queue;
- arbitrary workflow orchestration;
- exactly-once guarantees across machine loss;
- a second finalization supervisor;
- new terminal tables or migrations by default;
- generalized saga/compensation infrastructure;
- unbounded retry;
- provider retry-policy changes;
- changing upstream response semantics;
- a broad rewrite of `RequestCoordinator`;
- new CI jobs, cancellation matrices, or soak tests.

## Design decisions

1. Request finalization and attempt finalization have distinct operation strategies and identity shapes.
2. Durable convergence and runtime cleanup are separate facts in one structured result.
3. Already-terminal durable state is converged, not failed.
4. The existing `RequestFinalizationSupervisor` becomes the one process-owned retry scheduler for terminal commands.
5. The older `FinalizationRetryQueue` is removed from active ownership or reduced to a thin compatibility adapter that submits to the supervisor. It must not maintain a second retry policy.
6. Retry uses one bounded scheduler/timer task and existing backoff configuration.
7. Capacity saturation backpressures/rejects before ownership transfer; it never returns detached terminal work.
8. Recovery and stale sweeping use the same idempotent convergence functions as normal runtime finalization.
9. No new persistent queue is added for an SBC-local process. Startup stale reconciliation remains the process-restart safety net.

## Phase A — Define canonical terminal statuses

### Required changes

1. Identify the authoritative durable status values written to:
   - `requests.status`;
   - `request_attempts.status`;
   - reservation status/state.
2. Move the request terminal status set into one existing enum/module or a small shared constant near the finalizers.
3. Include the exact production values, including at minimum the currently written equivalents of:
   - completed;
   - client-side terminal/error;
   - cancelled;
   - internal/upstream error.
4. Define attempt terminal statuses separately from request statuses.
5. Update recovery to import/use the canonical sets instead of spelling a divergent list.
6. Access `aiosqlite.Row` columns directly by known name after selecting the required columns. Do not use row membership to infer column existence.
7. Treat an unknown durable status as unresolved, not terminal and not automatically conflicting-resolved.
8. Avoid a migration merely to rename historical statuses. Map existing values explicitly if compatibility is needed.

### Acceptance criteria

- Every status currently written by `RequestFinalizer` is recognized by request reconciliation.
- Attempt reconciliation uses the attempt status set.
- A real `aiosqlite.Row` with a terminal request status is recognized correctly.
- Unknown status remains unresolved and prevents false recovery readiness.
- No historical data rewrite is required for ordinary startup.

## Phase B — Use distinct operation identities

### Preferred identity shapes

Request finalization:

```text
strategy = request_finalization
request_id = durable requests.id
attempt_id = optional associated attempt
reservation_id = optional associated reservation
```

Attempt finalization:

```text
strategy = attempt_finalization
request_id = durable requests.id
attempt_id = durable request_attempts.id
reservation_id = durable reservation identity
```

Exact dataclass field names may follow existing structures, but operation ID must not change meaning by strategy without explicit typed fields.

### Required changes

1. Replace the shared generic `finalization` strategy with distinct request/attempt strategies.
2. Stop encoding an attempt ID into a field the reconciler interprets as a request ID.
3. Require all correctness-critical identities needed for reconciliation when constructing the ambiguous operation.
4. Request reconciliation inspects the request row and, when required, associated attempt/reservation state.
5. Attempt reconciliation inspects:
   - `request_attempts` by attempt ID;
   - the owning request ID for consistency;
   - reservation terminal state by reservation ID.
6. Reject mismatched identity tuples as unresolved conflict.
7. Do not infer reservation identity from account/model fields.
8. Remove legacy strategy handling once all call sites are migrated, or keep one bounded compatibility branch only for already-buffered in-process records during a rolling code path. A long-lived compatibility layer is unnecessary for process-local memory.

### Acceptance criteria

- Request finalization reconciliation queries the request row identified by `request_id`.
- Attempt finalization reconciliation queries the attempt row identified by `attempt_id`.
- Attempt ownership is cross-checked against the request ID.
- Reservation state is inspected using the supplied reservation ID.
- A mismatched tuple is unresolved and not counted as committed.

## Phase C — Introduce one structured convergence result

### Preferred internal result

A small frozen dataclass is sufficient:

```python
@dataclass(frozen=True, slots=True)
class FinalizationConvergenceResult:
    durable_terminal: bool
    durable_transitioned: bool
    reservation_converged: bool
    runtime_cleanup_complete: bool
    retryable: bool
    detail: str = ""
```

Field names may be adjusted to align with Plan 057's attempt result, but the semantics must remain explicit.

### Required semantics

1. `durable_terminal=True` when the relevant request/attempt is already terminal or this invocation transitions it.
2. `durable_transitioned=True` only when this invocation performs the durable transition.
3. `reservation_converged=True` when the reservation is known terminal/released, whether released now or previously.
4. `runtime_cleanup_complete=True` only when every owned in-process component has been released or proven already released.
5. `retryable=True` only for a bounded transient condition. Identity conflict, invalid status, or invariant failure is not blindly retried.
6. `detail` is bounded, non-secret, and diagnostic only.
7. Request and attempt finalizers may use specialized result types if needed, but callers must be able to derive the same convergence facts without boolean ambiguity.
8. A previously terminal row returns `durable_terminal=True`; it is not a failure merely because `durable_transitioned=False`.
9. Runtime cleanup may still run after discovering durable terminal state, using existing component progress/ownership to avoid double release.

### Acceptance criteria

- Already-terminal durable state is represented as converged.
- A successful durable transition followed by interrupted runtime cleanup is represented as incomplete/retryable cleanup, not total failure.
- Callers no longer interpret `False` as both idempotent no-op and retry failure.
- Runtime cleanup does not repeat components already marked complete.

## Phase D — Make the existing supervisor schedule retries

### Required changes

1. Keep `RequestFinalizationSupervisor` as the process owner for accepted terminal jobs.
2. Use the existing retry age and backoff base/cap settings.
3. Add one bounded scheduler mechanism:
   - a min-heap or ordered deque of next-attempt times; and
   - one supervisor-owned timer/wakeup task.
4. Do not create one sleeping task per failed job if a single timer can own the schedule.
5. On retryable incomplete result:
   - compute capped exponential backoff with no unnecessary randomization;
   - retain the job in the supervisor registry;
   - schedule the next run;
   - expose `retry_pending` health.
6. On convergence:
   - mark complete;
   - remove from active/retry structures;
   - retain only existing bounded history.
7. On non-retryable invariant conflict:
   - mark failed/unresolved;
   - retain bounded diagnostic state;
   - do not hot-loop.
8. On retry-age exhaustion:
   - stop automatic retry;
   - leave the durable/runtime state visible for stale/startup reconciliation;
   - record a bounded failure reason.
9. Shutdown drain:
   - wake due retries immediately only within the existing bounded drain budget;
   - do not extend shutdown indefinitely;
   - leave remaining durable work for startup reconciliation.
10. Capacity saturation:
    - reject/backpressure before claiming ownership; or
    - synchronously await one existing slot according to current call semantics.
    - Never return a detached untracked job.

### Acceptance criteria

- A retryable first failure is automatically invoked again without an external caller calling `run()`.
- Backoff base/cap and max retry age affect scheduling.
- Converged jobs leave active structures.
- Non-retryable conflicts do not loop.
- Capacity saturation creates no detached job.
- Shutdown remains bounded.

## Phase E — Retire contradictory retry ownership

### Required changes

1. Find all active call sites of `FinalizationRetryQueue`.
2. Prefer deleting those call paths and submitting the same command to `RequestFinalizationSupervisor`.
3. If immediate deletion is too disruptive, make the queue a thin adapter with no independent retry count/backoff/drop semantics.
4. Remove the behavior that retries an already-terminal `False` result and drops it after four attempts.
5. Coordinator-retained cleanup registries remain for in-flight attempt/claim component ownership established by Plans 056–057, but they must consume the structured convergence result and hand process-owned terminal retries to the supervisor.
6. Periodic stale finalization discovers identities and submits convergence work; it does not implement a parallel terminal state machine.
7. Startup reconciliation calls the same finalizer/convergence function directly under recovery ownership, rather than duplicating status interpretation.
8. Delete obsolete counters/configuration only when no active path uses them.

### Acceptance criteria

- There is one automatic retry policy for terminal jobs.
- The legacy queue cannot drop an already-terminal operation as failed.
- Stale and startup paths reuse canonical convergence logic.
- Coordinator-retained component progress is not replaced by another generic registry.
- No second supervisor or durable queue is introduced.

## Phase F — Reconcile through canonical convergence

### Required changes

1. Request finalization reconciler:
   - construct the request finalization command from explicit identities;
   - inspect canonical status;
   - invoke idempotent convergence when runtime/durable components remain incomplete.
2. Attempt finalization reconciler:
   - inspect attempt/request/reservation tuple;
   - invoke attempt convergence using the same result semantics.
3. Return `converged` only when required durable components are terminal.
4. During startup, runtime-only cleanup that cannot exist after process restart should be treated carefully:
   - process-local active counts are rebuilt/reset by startup ownership;
   - durable reservations/attempt/request rows still require convergence;
   - do not fabricate historical in-process ownership.
5. Conflicts remain unresolved under Plan 060 and prevent recovery ready.
6. Recovery does not enqueue work into a supervisor that is not yet running unless startup ordering explicitly owns it; direct bounded convergence is preferred during recovery.

### Acceptance criteria

- Real request-terminal rows reconcile successfully.
- Real attempt-terminal rows and released reservations reconcile successfully.
- Active reservation or pending attempt remains unresolved/retryable.
- Identity mismatch remains conflict.
- Recovery only acknowledges the ambiguous operation after canonical convergence.

## Focused verification

Test budget: normally no more than seven focused cases because request and attempt paths are materially distinct. Use existing capability-based files.

Required coverage:

1. Real `aiosqlite.Row` request status recognition for all canonical terminal status categories, preferably parameterized.
2. Attempt reconciliation reads `request_attempts` and reservation state using explicit IDs.
3. Already-terminal request returns converged and does not enter legacy retry/drop behavior.
4. Durable terminal plus incomplete runtime cleanup repairs only outstanding components.
5. Supervisor automatically retries one transient failure and then converges.
6. Capacity saturation does not create detached work.
7. Recovery conflict/identity mismatch remains unresolved.

Use deterministic clock injection or a directly advanced scheduler seam for backoff; do not sleep real seconds. Do not add a long-running supervisor soak.

## Implementation sequence

Recommended commits:

1. canonical statuses and identity strategies;
2. structured convergence results in finalizers;
3. supervisor retry scheduler and saturation behavior;
4. legacy queue retirement/adapter and call-site migration;
5. recovery reconciliation reuse and focused tests;
6. plan/documentation closure.

## Plan acceptance criteria

- [x] Request and attempt terminal statuses are canonical and shared with recovery.
- [x] `aiosqlite.Row` access uses direct named columns.
- [x] Request and attempt ambiguity strategies have distinct explicit identities.
- [x] Attempt reconciliation inspects attempt, owning request, and reservation.
- [x] Finalizers return structured durable/runtime convergence facts.
- [x] Already-terminal durable state is success/convergence, not retry failure.
- [x] The existing supervisor automatically performs bounded retries.
- [x] Retry age and backoff configuration are used.
- [x] Capacity saturation returns no detached untracked work.
- [x] The legacy retry queue has no contradictory retry/drop semantics.
- [x] Stale and startup reconciliation reuse canonical convergence.
- [x] No new durable queue, workflow engine, supervisor, migration, CI job, or soak harness is introduced.

## Definition of done

The plan is complete when request and attempt finalization use explicit identities and statuses, one structured result distinguishes durable transition from convergence and runtime cleanup, the existing supervisor owns bounded retries, recovery uses the same convergence logic, and focused regressions plus the existing smoke suite pass.
