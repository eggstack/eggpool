# Plan 066 — Terminal Runtime Ownership and Supervisor Closure

Date: 2026-08-01
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Corrective predecessor: `plans/065-terminal-recovery-and-small-regression-closure.md`
Planning baseline: `d504005e46625bb5d8df72f5306d6eafb11d43b8`

## Purpose

Close the remaining terminal-runtime ownership defects found after implementation of Plan 065. Plan 065 correctly tightened durable convergence, recovery admission, retry exhaustion, quota expiry, update output, and production retry ownership. It did not fully separate durable database convergence from resumable in-memory cleanup.

This pass is deliberately narrow. It must make the existing retained finalization job truthfully own the runtime work already attributed to it, enforce the configured retry-age deadline at execution time, define coordinator behavior when finalization capacity is unavailable, expose the existing supervisor through runtime diagnostics, and correct closure metadata.

The deployment target remains a private, single-process SBC or small LAN host. The solution should use the structures already present in EggPool rather than introduce another queue, worker, database table, lifecycle framework, or verification system.

## Confirmed residual defects

### 1. Production terminal jobs do not carry runtime ownership

`RequestCoordinator._finalize_terminal()` registers a `RequestFinalizationJob` without an `AttemptRuntimeLease`. It then binds the job with `router=None`, `quota_estimator=None`, and `health_manager=None`.

The job therefore reaches `_execute_runtime_release()` with no lease and records `runtime_cleanup_complete=True` without performing or proving any in-memory cleanup.

### 2. Runtime convergence remains embedded in the durable finalizer

`RequestFinalizer.finalize()` performs its durable transaction and then, only when `request_transitioned` is true, may:

- remove the in-memory quota reservation;
- decrement the router active-request count;
- record final usage into the live quota snapshot;
- update health/probe state;
- update account runtime state.

If the durable transaction commits and one of those awaited post-commit operations fails, the retained job retries. The second durable call observes an already-terminal request, returns `request_transitioned=False`, and skips the unfinished runtime work. Because the job has no runtime lease, it can then report runtime cleanup complete even though one or more components remain unrepaired.

### 3. Result fields conflate durable and runtime facts

`FinalizationResult.quota_reservation_removed` is populated from the durable reservation transition. A released SQLite reservation does not prove that the live `QuotaEstimator` reservation was removed. The same ambiguity affects the broad runtime completion flag.

### 4. Retry age is checked only when scheduling

`RequestFinalizationSupervisor._schedule_retry()` checks age before pushing work into the retry heap. The scheduler does not recheck the absolute age before executing a due retry. A job can therefore run beyond `max_retry_age_s` by up to the current backoff interval.

### 5. Capacity rejection is not handled explicitly by the coordinator

`register_or_get()` now correctly raises `FinalizationCapacityError` before returning detached work, but `_finalize_terminal()` does not define pre-handoff and post-handoff behavior for that exception. The exception can escape through completion, cancellation, or streaming terminal paths without a bounded operator diagnostic or an explicit repair contract.

### 6. Operator documentation describes supervisor diagnostics that are not exposed

The legacy retry-queue runtime snapshot was removed, but `RuntimeMetricsService` does not publish the active generation's `RequestFinalizationSupervisor.snapshot()`. Documentation directs operators to inspect active jobs, retry-pending work, failures, and saturation without providing that runtime field.

### 7. Closure metadata is inconsistent

Plans 058 and 065 are marked complete while their acceptance checklists remain unchecked and Plan 058 omits Plan 065 from its implementation-plan list. The remaining runtime defect means the parent roadmap must remain open until this pass lands.

## Scope

Primary runtime files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/runtime_metrics.py`
- runtime-generation accessors only where required to expose the existing supervisor

Focused test files:

- `tests/unit/test_request_finalization_state_machine.py`
- the existing request-finalizer reservation/convergence test file
- the existing coordinator terminal-path test file
- `tests/unit/test_runtime_metrics.py`

Planning and operator documentation:

- `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
- `plans/065-terminal-recovery-and-small-regression-closure.md`
- this plan
- `AGENTS.md`, architecture, or the OpenCode stability guide only where behavior changes

## Explicitly out of scope

- a second finalization supervisor;
- reintroducing `FinalizationRetryQueue`;
- a database-backed work queue;
- a workflow engine, saga abstraction, or generic cleanup framework;
- new request, attempt, or reservation tables or migrations;
- unbounded retries or retry persistence across process restart;
- changing upstream request retry policy;
- broad health-manager or quota-estimator redesign;
- adding a new runtime metrics subsystem;
- new CI jobs, matrices, coverage thresholds, soak tests, timing gates, benchmark gates, or evidence bundles;
- plan-numbered test suites;
- attempting to make every old direct test construction emulate the complete production generation.

## Governing decisions

1. `RequestFinalizationSupervisor` remains the sole automatic in-process terminal retry owner.
2. `RequestFinalizer` proves durable request, attempt, and reservation convergence. It must not be the only owner of retryable in-memory cleanup.
3. The existing `AttemptRuntimeLease` is the runtime ownership token. Extend it only as needed; do not add a parallel lease hierarchy.
4. Runtime ownership is derived from explicit publication facts, not from nonzero estimates or assumptions about the selected account.
5. Runtime convergence is per-component and idempotent. A partial failure must resume at the unfinished component without replaying completed components.
6. Durable reservation release and in-memory quota reservation removal are separate facts.
7. A coordinator capacity rejection is fail-closed and observable. It must not be silently converted into successful terminal convergence or handled by an untracked detached task.
8. Retry age is an absolute deadline from job creation, not an approximate scheduling hint.
9. Runtime diagnostics reuse the supervisor's existing bounded snapshot. Do not create a second metrics registry.
10. Verification remains focused and deterministic.

## Phase A — Carry explicit runtime ownership into the retained job

### Required changes

1. Preserve the successful `RuntimePublicationReceipt` or equivalent explicit acquisition facts through terminal finalization.
2. Use one of the existing narrow carriers:
   - an immutable runtime-ownership field on `SelectedAttempt`; or
   - a coordinator-owned receipt keyed by the selected request/attempt identity and consumed when the terminal job is registered.
3. Do not infer `active_count_acquired`, `quota_reservation_acquired`, or health/probe ownership solely from selected-account presence, estimated tokens, or estimated cost.
4. Construct one `AttemptRuntimeLease` before `register_or_get()` transfers terminal ownership to the supervisor.
5. Populate the lease with:
   - account identity;
   - estimated request/token/cost dimensions used during publication;
   - whether router active count was incremented;
   - whether quota reservation was added;
   - whether a health/probe slot was actually acquired;
   - the terminal data required to apply final usage and health/runtime outcome once.
6. Pass that lease to `register_or_get()` and bind the real router, quota estimator, health manager, and any required account runtime dependency to the job.
7. A deduplicated registration must retain the original lease and reject incompatible ownership facts rather than silently replacing them.
8. Synthetic or pre-selection terminal records that acquired no runtime resources must use an explicit empty lease or the established no-runtime path; they must not fabricate acquired components.

### Acceptance criteria

- Every production selected terminal job has an explicit runtime lease.
- Lease acquisition flags match the publication receipt.
- A selected request with a zero monetary reservation but acquired request/token pressure still owns quota cleanup.
- Deduplicated terminal callers share one lease.
- No production job declares runtime cleanup complete merely because its lease is absent.

## Phase B — Make runtime convergence resumable and exactly-once

### Required changes

1. Keep `RequestFinalizer.finalize()` responsible for:
   - cost calculation and durable cost fields;
   - the atomic request/attempt/reservation transaction;
   - truthful `DurableFinalizationResult` fields;
   - durable-only or best-effort analytics that cannot retain correctness ownership.
2. Move retryable in-memory convergence into the retained job's existing runtime-lease step. At minimum, track independently:
   - quota reservation removal;
   - router active-count decrement;
   - final live usage application when this terminal command performed the request transition;
   - health/probe outcome application or release;
   - account runtime success/failure update when currently required.
3. Extend `AttemptRuntimeLease` with only the fields and component markers needed for those operations. A small `set[str]` or explicit booleans are both acceptable.
4. The lease must mark a component complete only after that component succeeds.
5. If component N fails after components 1 through N-1 succeeded, the next supervisor retry must resume at component N. It must not:
   - rerun the durable transaction unnecessarily;
   - remove the quota reservation twice;
   - decrement active count twice;
   - apply final usage twice;
   - double-apply health or account runtime effects.
6. Preserve job progress at `RUNTIME_RELEASE_PENDING` when runtime convergence is incomplete. A runtime failure must not reset progress to durable finalization.
7. Preserve the direct/no-supervisor compatibility path by executing the same durable result plus the same runtime lease synchronously. Do not keep a second copy of the cleanup logic inside `RequestFinalizer`.
8. Preserve the existing stale/startup safety nets. They remain process-loss repair paths, not substitutes for in-process lease retry.
9. Ensure cancellation of the request waiter does not cancel the retained runtime-convergence attempt.
10. Keep analytics failures non-authoritative and outside retry ownership unless an existing correctness invariant explicitly requires otherwise.

### Result semantics

Update `FinalizationResult` so its fields mean what their names claim:

- `reservation_released`: durable reservation state;
- `quota_reservation_removed`: actual live quota-estimator removal;
- `active_count_decremented`: actual router decrement;
- `health_released_or_recorded`: actual health/probe convergence;
- `runtime_cleanup_complete`: every acquired/required runtime component completed;
- `durable_terminal` and `reservation_converged`: durable database facts only.

Do not set runtime fields from `DurableFinalizationResult.reservation_transitioned`.

### Acceptance criteria

- A post-commit failure in one runtime component leaves the job retry-pending at the runtime step.
- The retry completes the unfinished component without replaying successful components.
- Durable finalization runs once for that in-process partial-runtime-failure scenario.
- Runtime result fields reflect actual component outcomes.
- Already-terminal durable state can still converge outstanding runtime ownership.
- The direct compatibility path and production supervisor path use the same runtime-convergence implementation.

## Phase C — Enforce retry age at execution time

### Required changes

1. Define one absolute expiry time for each job from `created_at + max_retry_age_s`.
2. Before executing a due heap entry, recheck that:
   - the heap key still maps to the same active job;
   - the job is not complete or failed;
   - the absolute retry deadline has not passed.
3. If the deadline has passed, retire the job through the existing exhausted-job path without incrementing retry count or calling `job.run()`.
4. Optionally cap the heap due time to the absolute deadline, but still retain the execution-time check.
5. Stale heap entries remain harmless no-ops.
6. Do not add one timer task per job; retain the single scheduler.

### Acceptance criteria

- No timer-driven retry begins after `max_retry_age_s`.
- A job whose backoff would cross the deadline is retired at or before the deadline.
- Retry count increments only for retries that actually begin.
- Exhaustion still releases active capacity and operational references.

## Phase D — Define coordinator behavior for capacity rejection

### Required changes

1. Catch `FinalizationCapacityError` at the canonical `_finalize_terminal()` boundary.
2. Record one bounded diagnostic containing only scalar identity/outcome data. Reuse supervisor counters or the existing stream/runtime diagnostic surface.
3. Do not spawn a detached cleanup task and do not reintroduce a queue.
4. Preserve different response constraints:
   - before downstream handoff, propagate a typed fail-closed terminal-invariant error through the existing API error mapping;
   - after streaming handoff, do not attempt to replace the already-started response status. Record the invariant failure, leave durable pending state discoverable by stale/startup repair, and avoid recursively invoking a second terminal finalizer.
5. Ensure the coordinator never reports successful terminal convergence after capacity rejection.
6. Keep the existing pending request, attempt, and reservation identities intact so the safety sweep can identify the work.
7. Document this as an overload invariant, not an ordinary upstream error and not a provider/account penalty.
8. Do not broaden this phase into admission control or dynamic supervisor resizing.

### Acceptance criteria

- Capacity rejection is caught and classified at one coordinator boundary.
- Pre-handoff rejection fails closed with a typed local error and no provider penalty.
- Post-handoff rejection records a bounded invariant diagnostic without recursive finalization.
- No detached task or second retry mechanism is created.
- Pending durable state remains discoverable by existing repair paths.

## Phase E — Expose supervisor diagnostics through runtime metrics

### Required changes

1. Add one `finalization_supervisor` field to `RuntimeMetricsService.snapshot()`.
2. Resolve the supervisor from the active runtime generation or the already-available process/runtime-manager path. Do not copy its counters into a second service.
3. Return the existing bounded `RequestFinalizationSupervisor.snapshot()` shape, including at least:
   - active count;
   - retry-pending count or equivalent active health breakdown;
   - bounded failed/history counts;
   - saturation and registration counters;
   - configured capacity and retry-age limits.
4. Return `None` when no production supervisor is available, as in lightweight tests or partial startup.
5. Remove the obsolete `finalization_retry_queue` constructor parameter if repository callers no longer use it. If compatibility requires retaining it temporarily, mark it ignored and do not expose a queue field.
6. Align operator documentation with the exact JSON field and names actually emitted.

### Acceptance criteria

- `/api/stats/runtime` exposes one bounded `finalization_supervisor` snapshot in production.
- The snapshot is read-only and does not acquire durable ownership or perform database I/O.
- Missing supervisors produce `None`, not an exception.
- No `finalization_retry_queue` runtime field returns.

## Phase F — Focused verification and metadata closure

### Automated test budget

Add or modify no more than five focused regression cases in existing capability-based files. Parameterize pre/post-handoff capacity behavior if it keeps the total small.

Required coverage:

1. A durable commit followed by a runtime-component failure retries from `RUNTIME_RELEASE_PENDING`, completes the missing component, and does not double-release earlier components.
2. Runtime result fields distinguish durable reservation release from live quota/router/health convergence.
3. A due retry whose absolute age has expired is retired without invoking the job or incrementing retry count.
4. Coordinator capacity rejection has explicit pre-handoff and post-handoff behavior without detached work.
5. Runtime metrics expose the active supervisor snapshot and return `None` when absent.

Where practical, extend existing tests instead of creating new files. Use mocked clocks or direct due-heap manipulation rather than wall-clock sleeps.

### Required local checks

Focused checks:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest tests/unit/test_request_finalization_state_machine.py -q --tb=short --maxfail=1
uv run pytest <affected finalizer/coordinator/runtime-metrics tests> -q --tb=short --maxfail=1
```

Existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add CI jobs, fault matrices, repeated cancellation campaigns, live-provider tests, benchmark assertions, soak tests, or retained evidence artifacts.

### Planning and documentation closure

After runtime implementation and verification:

1. Mark Plan 066 complete and check each satisfied acceptance item.
2. Add a short post-implementation review note to Plan 065 linking this corrective follow-up; do not erase its historical implementation record.
3. Update Plan 058 to `completed` only after runtime ownership, retry deadlines, capacity handling, and diagnostics are verified.
4. Register Plans 065 and 066 in Plan 058's implementation list.
5. Check completed roadmap criteria rather than leaving a completed roadmap with an entirely unchecked checklist.
6. Keep architecture and operator documentation limited to behavior actually exposed.

## Recommended implementation sequence

1. carry publication facts into `AttemptRuntimeLease`;
2. move retryable runtime convergence from `RequestFinalizer` to the retained lease step;
3. correct `FinalizationResult` runtime semantics;
4. enforce the absolute retry deadline in the scheduler;
5. handle coordinator capacity rejection explicitly;
6. expose the supervisor runtime snapshot;
7. run focused checks, the existing smoke gate, and close metadata.

Prefer one or two coherent runtime commits plus one documentation closure commit. Do not split every component flag into a separate ceremonial commit.

## Plan acceptance criteria

- [x] Every production selected terminal job carries explicit runtime ownership derived from publication facts.
- [x] `AttemptRuntimeLease` owns retryable quota, router, usage, health/probe, and account-runtime convergence required by the terminal path.
- [x] A partial runtime failure resumes at the unfinished component without replaying durable finalization or completed runtime components.
- [x] Durable reservation release is not reported as live quota reservation removal.
- [x] Runtime result fields reflect actual component outcomes.
- [x] Direct/no-supervisor finalization uses the same runtime-convergence implementation.
- [x] No timer-driven retry begins after the configured absolute retry age.
- [x] Retry exhaustion still frees capacity and releases operational references.
- [x] Coordinator capacity rejection is explicitly handled before and after downstream handoff.
- [x] Capacity rejection creates no detached work, second queue, or provider penalty.
- [x] Runtime metrics expose the existing supervisor's bounded snapshot.
- [x] Operator documentation matches the emitted runtime field.
- [x] Plans 058, 065, and 066 have coherent status and checked acceptance metadata.
- [x] Focused regressions and the existing smoke suite pass.
- [x] No migration, runtime dependency, durable queue, workflow framework, CI job, test matrix, soak gate, benchmark gate, or evidence format is introduced.

## Rejection conditions

Do not close this plan if:

- a production terminal job still has no runtime lease;
- the durable finalizer remains the only owner of retryable post-commit runtime work;
- a retry after durable commit can skip unfinished runtime cleanup;
- runtime cleanup is marked complete merely because no lease was supplied;
- quota/router/usage/health effects can be applied twice after partial failure;
- a retry begins after the absolute retry deadline;
- capacity rejection escapes the canonical coordinator boundary without explicit semantics;
- runtime documentation references supervisor diagnostics that the API does not expose;
- completed planning documents retain contradictory status/checklist state;
- implementation adds a second retry mechanism or disproportionate verification infrastructure.

## Implementation closure

Implemented on 2026-08-01. Publication receipts now create explicit runtime
leases carried by selected terminal jobs. Durable and runtime convergence facts
are separate, runtime cleanup resumes per component, retry execution enforces
the absolute age deadline, capacity rejection is classified at the coordinator
boundary, and the active supervisor snapshot is exposed as
`finalization_supervisor` in `/api/stats/runtime`.

## Definition of done

This corrective pass is complete when the existing retained finalization job carries and resumes all required runtime ownership after durable convergence, each runtime result field is truthful, retry execution respects the absolute age limit, saturation has explicit fail-closed coordinator semantics, operators can inspect the supervisor through the existing runtime endpoint, focused regressions and the smoke gate pass, and the parent roadmap can be closed without introducing new infrastructure.
