# Process-Owned Request Finalization and Runtime Ownership Release

Date: 2026-07-25
Status: implementation handoff

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/025-failure-effects-and-model-quarantine.md`

## Objective

Make selected-attempt cleanup independent of the client request task. Once Eggpool has durably created a request, attempt, or reservation and claimed runtime ownership, one retained process-owned finalization job must own terminal reconciliation until every durable and in-memory obligation has either completed or entered a bounded, observable retry state.

This phase closes the class of failures where a harmless upstream validation response is followed by client cancellation, finalizer failure, or task teardown that strands active-request counts, quota reservations, health probe slots, or pending database rows and causes unrelated proxy requests to fail.

## Design principle

`asyncio.shield()` alone is not ownership. A shielded coroutine may continue after the outer task is cancelled, while the outer task skips subsequent cleanup. Eggpool must retain the finalization task in process-owned state, observe its completion independently of request waiters, reconcile completion exactly once, and keep bounded retry ownership when durable finalization cannot complete immediately.

## Scope

### In scope

- Process-owned finalization job for every selected attempt.
- Idempotent runtime ownership token.
- Durable and runtime finalization state machine.
- Retained task registry and completion reconciliation.
- Bounded retry queue with age, capacity, and backoff policy.
- Cancellation-safe non-streaming, streaming, capability-rejection, compatibility-retry, timeout, and transport-error paths.
- Startup stale-state reconciliation and shutdown drain/adoption.
- Metrics, diagnostics, and operator-visible backlog state.
- Fault-injection and concurrency tests.

### Out of scope

- Reload accepted-finalization architecture except reuse of its proven process-owned patterns.
- Database connection replacement; Plan 027 owns connection recovery.
- Failure classification policy; Plan 025 supplies immutable effects.
- Distributed/crash-persistent job execution beyond SQLite-backed idempotent reconciliation.
- Retrying upstream generation after response bytes.

## Mandatory ownership model

A selected attempt acquires two classes of ownership.

### Durable ownership

- request row;
- attempt row;
- reservation row;
- optional routing trace/audit rows.

### Runtime ownership

- router active-request count;
- quota estimator reservation;
- health/circuit half-open probe slot;
- stream observer/upstream response resources;
- generation lease where applicable.

Represent runtime ownership with one object, for example:

```python
@dataclass(slots=True)
class AttemptRuntimeLease:
    account_name: str
    estimated_tokens: int
    estimated_microdollars: int
    active_count_acquired: bool
    quota_reservation_acquired: bool
    health_probe_acquired: bool
    released: bool = False

    async def release_once(self, *, reason: str) -> RuntimeReleaseOutcome:
        ...
```

The exact implementation may use component tokens, but it must provide one idempotent release boundary. Boolean facts must reflect actual acquisition and release outcomes, not assumptions.

## Workstream A — Define finalization state and identity

Create an immutable identity containing all data needed to finalize without querying mutable request context:

```python
@dataclass(frozen=True, slots=True)
class FinalizationIdentity:
    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    provider_id: str
    model_id: str
    client_protocol: str
    upstream_protocol: str
    attempt_number: int
```

Create a progress state machine. Suggested states:

```text
created
  -> durable_finalization_pending
  -> durable_finalized
  -> runtime_release_pending
  -> runtime_released
  -> analytics_pending
  -> completed
```

Failure/retry is health metadata, not a terminal progress state. Only `completed` is fully terminal. Analytics and diagnostic emission must remain non-authoritative: failure there cannot retain correctness ownership indefinitely.

## Workstream B — Create retained finalization jobs

Introduce `RequestFinalizationJob` or equivalent with:

- immutable identity;
- immutable terminal outcome data;
- immutable `FailureEffects` from Plan 025;
- runtime ownership token;
- progress cursor;
- attempt/failure/retry counters;
- latest error class and bounded message category;
- created/updated timestamps;
- retained `asyncio.Task`;
- idempotent `run()` or `resume()`.

Requirements:

- Register synchronously before the first await that can be cancelled after a terminal outcome is known.
- One retained task per current attempt.
- Concurrent callers share the same retained task.
- Callers await through shield or equivalent, but cancellation of every caller does not cancel the retained task.
- Completion callback or process-owned observer schedules reconciliation.
- Reconciliation removes completed jobs from active registry and stores only bounded scalar history.
- Operational references become collectible after completion.

## Workstream C — Unify terminal paths

Route all selected-attempt terminal outcomes through the same job:

- successful non-stream response;
- non-retryable upstream 4xx;
- retryable upstream failure before selecting another account;
- local/provider capability rejection after selection;
- compatibility adaptation retry first-attempt closure;
- client cancellation before first byte;
- client cancellation midstream;
- upstream midstream failure;
- timeout;
- unexpected coordinator exception after selection;
- response rendering failure after upstream completion.

The terminal job may use specialized outcome data, but no path may manually duplicate durable finalization plus runtime counter release.

Pre-selection validation remains outside this machinery because it acquired no selected-attempt ownership. Tests must prove that distinction.

## Workstream D — Define durable finalization atomicity

The correctness transaction must:

1. Finalize request if pending.
2. Finalize the current attempt if incomplete.
3. Release the durable reservation if active.
4. Record only correctness-critical account/quarantine evidence required by Plan 025.
5. Commit atomically.

Move best-effort work outside the transaction:

- account-event enrichment that can be reconstructed;
- metrics/coalescer events;
- routing trace writes;
- verbose diagnostic summaries;
- dashboard-only denormalization not required for correctness.

If an effect must be atomic with terminal status, document why and test the invariant.

The job must inspect idempotent transition results. “Already finalized” is success only if the existing durable state is compatible with the intended terminal identity and outcome.

## Workstream E — Define runtime release semantics

After durable finalization is known committed or reconciled, call `release_once()`.

Required ordering:

- Do not decrement active count before durable selection ownership has been compensated or finalized.
- Do not remove quota reservation twice when attempt finalizer and request finalizer race.
- Release health probe even for client errors and cancellation.
- Apply immutable failure effects at most once.
- Record final usage/cost separately from removing speculative reservation.
- Runtime release failure remains retryable and does not rerun durable state transitions unnecessarily.

Each component release must return a structured outcome. Silent best-effort cleanup is insufficient for correctness ownership.

## Workstream F — Bounded retry and reconciliation

Replace ad hoc finalization retry entries with one bounded process-owned supervisor, or adapt the existing queue to own the new job type.

Required policy:

- Configurable bounded queue capacity.
- Deduplication by finalization identity and generation.
- Exponential backoff with cap and optional jitter.
- Maximum active retry task count.
- Maximum retained age before operator escalation; age expiry does not silently discard unresolved ownership.
- Queue saturation marks readiness/degraded diagnostics according to policy and returns a precise result.
- Oldest job age, queue depth, retries, failures, completion rate, and saturation count exposed.
- Repeated retry does not reapply failure effects or usage.

When a durable outcome is ambiguous, hand off to Plan 027 reconciliation rather than assuming failure or success.

## Workstream G — Cancellation and task-lifetime hardening

At every terminal path:

```python
job = supervisor.register_or_get(...)
task = job.ensure_task()
try:
    outcome = await asyncio.shield(task)
except asyncio.CancelledError:
    supervisor.retain(job)
    raise
```

The actual structure may differ, but these facts are mandatory:

- Job registration precedes cancellation-sensitive waits.
- Outer cancellation cannot cancel the retained task.
- Cleanup after the shield is not required for correctness; the retained task owns it.
- Completion reconciliation occurs without request waiter participation.
- No raw task is left unreferenced.

Add source/AST guard tests where useful to prevent later reintroduction of `await shield(...); then critical cleanup` patterns.

## Workstream H — Streaming integration

Streaming response ownership is more complex because the terminal outcome is not known when headers are returned.

Requirements:

- Stream wrapper owns the upstream response, observer, generation lease, and terminal job creation.
- Exactly one terminal job is created on normal completion, cancellation, midstream error, or generator close.
- Async generator finalization and ASGI disconnect paths converge.
- No second call to `request.stream()` or duplicate iterator consumption.
- First-byte and usage data remain accurate.
- Cancellation is not classified as provider failure.
- Finalization timeout queues the retained job rather than merely logging that the request may leak.

## Workstream I — Startup and shutdown

### Startup

After migrations and before readiness:

- Find stale pending requests, incomplete attempts, and active reservations.
- Reconstruct bounded reconciliation jobs using durable identity.
- Do not fabricate runtime ownership that cannot survive process restart; reconcile durable state and reset process-local counters from authoritative startup reconstruction.
- Clear or repair orphaned active facts under existing crash-recovery policy.
- Record startup reconciliation counts and failures.

### Shutdown

- Stop accepting new proxy requests.
- Allow active streams a bounded drain.
- Stop creating new finalization jobs.
- Drain retained jobs for configured duration.
- Persist/reconcile unresolved durable jobs or adopt them for startup repair.
- Release process-local runtime ownership exactly once.
- Close clients/database only after the supervisor reaches a safe ownership boundary.

Shutdown timeout must not silently drop jobs.

## Workstream J — Diagnostics and history

Active registry entries may retain operational references. Completed history must contain scalar-only records in a bounded deque.

Expose:

- active job count;
- oldest age;
- progress by state;
- retry/failure counts;
- durable-finalized but runtime-release-pending count;
- ambiguous database outcome count;
- completion latency p50/p95/p99;
- saturation and shutdown-adoption counts;
- recent bounded terminal categories.

Do not expose API keys, headers, prompt data, response bodies, or reasoning content.

## Workstream K — Tests

Suggested files:

- `tests/unit/test_plan_026_runtime_ownership_token.py`
- `tests/unit/test_plan_026_finalization_state_machine.py`
- `tests/unit/test_plan_026_finalization_supervisor.py`
- `tests/integration/test_plan_026_terminal_path_matrix.py`
- `tests/integration/test_plan_026_cancellation_matrix.py`
- `tests/integration/test_plan_026_stream_finalization.py`
- `tests/unit/test_plan_026_startup_reconciliation.py`
- `tests/unit/test_plan_026_shutdown_drain.py`
- `tests/soak/test_plan_026_finalization_plateau.py`

Required fault matrix:

- Cancellation at every Plan 023 seam.
- Durable write failure before commit.
- Commit failure with successful rollback.
- Ambiguous commit delegated to Plan 027 seam.
- Runtime active-count decrement failure.
- Quota reservation removal failure.
- Health probe release failure.
- Analytics/coalescer failure.
- Retry queue saturation.
- Completion callback/request waiter race.
- Shutdown during durable finalization.
- Shutdown during runtime release.

## Acceptance criteria

### Ownership

- [ ] Every selected attempt has one finalization identity and runtime ownership token.
- [ ] Runtime component acquisition facts are explicit.
- [ ] `release_once()` is idempotent under concurrent and repeated callers.
- [ ] Durable and runtime ownership cannot be silently discarded.
- [ ] Pre-selection failures create no finalization job.

### Process-owned execution

- [ ] Job registration occurs before the first cancellation-sensitive terminal await.
- [ ] Cancelling all request waiters does not cancel retained finalization.
- [ ] Completion reconciliation is process-owned.
- [ ] Completed jobs leave the active registry promptly.
- [ ] Completed history is bounded and scalar-only.
- [ ] Retained request/provider/runtime objects become collectible after completion.

### Terminal paths

- [ ] Success, client error, capability rejection, compatibility retry, timeout, cancellation, transport failure, and midstream error all use the common job.
- [ ] Streaming creates exactly one terminal job.
- [ ] No terminal path duplicates quota removal or active-count decrement.
- [ ] Client cancellation applies zero provider-health effect.
- [ ] Failure effects are applied exactly once.

### Retry and recovery

- [ ] Retry queue is bounded, deduplicated, and observable.
- [ ] Queue saturation has deterministic behavior.
- [ ] Repeated retry does not duplicate usage, cost, health, or quarantine evidence.
- [ ] Ambiguous database outcome is delegated to reconciliation rather than guessed.
- [ ] Startup repairs stale durable state before readiness.
- [ ] Shutdown drains or adopts unresolved ownership without silent loss.

### Leak closure

- [ ] After bounded cleanup, every terminal request has no pending reservation.
- [ ] Router active counts return to baseline.
- [ ] Quota reservations return to baseline.
- [ ] Health/circuit probe slots return to baseline.
- [ ] Pending request and incomplete attempt counts return to zero except explicitly retained fault fixtures.
- [ ] A failed request cannot block the next request on a concurrency-one account.
- [ ] Repeated cancellation/failure soak has a stable active-job plateau and RSS plateau.

### Verification

- [ ] Plans 023–025 focused suites remain green.
- [ ] Plan 026 focused tests pass on Python 3.11 and 3.12.
- [ ] High-concurrency streaming suite passes.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.

## Closure evidence

The implementation record must provide the terminal-path matrix with exact durable/runtime final states, a 100-iteration cancellation race result for every critical boundary, weak-reference collection evidence, and a soak comparison showing no growth in active finalization jobs, pending rows, reservations, active counts, or RSS after warm-up.
