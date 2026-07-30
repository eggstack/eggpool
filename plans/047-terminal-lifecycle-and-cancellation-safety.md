# Plan 047 — Single Terminal Owner and Cancellation-Safe Cleanup

Date: 2026-07-30
Status: implementation handoff
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plan 046 only for canonical capability-error fixtures; implementation may begin independently
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Establish exactly one owner for every terminal request transition and make the complete post-selection cleanup sequence survive caller cancellation, database delay, and duplicate invocation.

The end state must prevent a local capability rejection or upstream 4xx from leaving durable reservations, pending requests, in-memory quota reservations, active-request counts, or half-open circuit probe ownership behind. It must also eliminate the streaming non-retryable double-finalization path.

## Confirmed defects to close

1. Streaming HTTP 4xx handling calls `_finalize_non_retryable()` and then raises `_NonRetryableUpstreamError`; `_handle_exhausted()` invokes the finalizer again.
2. Post-selection capability cleanup shields only `AttemptFinalizer.finalize_failed_attempt()`. Cancellation of the request task can skip subsequent quota removal, active-count decrement, health-slot release, metrics, and request finalization.
3. Durable and in-memory cleanup is split across coordinator branches, attempt finalizer, request finalizer, and cancellation handlers with implicit ordering assumptions.
4. Idempotent database transitions are being used as tolerance for duplicate lifecycle entry rather than as a defensive last line.
5. Some error branches decide terminal outcome before all ownership tokens are represented in one retained job.

## Ownership boundary

Primary modules:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalization_queue.py`
- narrow failure-effects and diagnostics call sites required for exactly-once behavior
- focused lifecycle/fault-injection tests

Do not change stream terminal-marker semantics, timeout configuration, SSE framing, or selection hot-path architecture in this phase.

## Required lifecycle model

### 1. One terminal command per selected attempt

After durable selection, every terminal path must construct one immutable terminal command/job containing:

- request identity;
- attempt identity;
- reservation identity;
- account/provider/model identity;
- terminal outcome;
- HTTP status and error class where applicable;
- usage/cost data when available;
- bytes emitted and response-started state;
- health/effects observation;
- cleanup ownership flags or expected transitions.

The job is the sole owner of:

1. attempt terminal transition;
2. durable reservation release;
3. request terminal transition;
4. in-memory quota reservation removal;
5. router active-request decrement;
6. circuit/health probe release or success/failure record;
7. typed failure-effects application;
8. terminal diagnostics emission.

Callers may build and submit the job, but must not perform an overlapping subset before raising to another terminal handler.

### 2. Retained execution

Once a request has been selected and a terminal outcome is known, cleanup must run in a process-owned retained task/job that is not cancelled with the ASGI request task.

Required behavior:

- caller cancellation may stop awaiting the result;
- the retained task continues to convergence;
- application shutdown drains retained jobs within a bounded timeout and reports any residue;
- repeated submission of the same attempt identity returns or joins the existing job rather than starting a second cleanup sequence.

### 3. Idempotency key

Use a stable attempt-level key containing at least the durable attempt ID and request ID. A terminal job must reject conflicting terminal payloads for the same key or retain the first authoritative transition and record a conflict diagnostic.

Do not silently let `completed` and `client_error` race and treat whichever database transition wins as acceptable.

### 4. Explicit transition result

Terminal execution must return a structured result indicating which transitions actually occurred:

- attempt transitioned;
- request transitioned;
- reservation released;
- quota reservation removed;
- active count decremented;
- health slot released or health outcome recorded;
- effects applied or already applied;
- retry queued because database work did not converge;
- terminal conflict detected.

The exact type may extend existing finalization results, but callers and tests must not infer cleanup from a single boolean.

### 5. Failure behavior

If durable finalization fails:

- preserve the retained job for bounded retry;
- do not apply provider health penalties merely because SQLite failed;
- avoid decrementing ownership twice on retry;
- expose a bounded diagnostic and readiness/recovery interaction consistent with existing database recovery policy;
- never require deleting the database to restore routing.

If in-memory cleanup fails after durable transition:

- continue remaining independent cleanup steps;
- retain enough state to retry only incomplete ownership releases;
- record the exact incomplete component;
- do not replay durable effects already transitioned.

## Required path consolidation

### Local capability rejection

The coordinator must submit one client-error terminal job. It must not manually finalize the attempt and then call the request finalizer separately.

No health penalty, model quarantine, or persistent backoff is permitted.

### Streaming/non-streaming upstream 4xx

Both paths must create the same typed non-retryable terminal outcome. Streaming code must not eagerly finalize and then raise to `_handle_exhausted()`.

A 400 validation response must remain request-local unless typed response evidence identifies a different category already supported by the failure classifier.

### Retryable pre-body failure

A failed attempt may be terminally finalized before selecting another account, but the attempt-finalization command must remain single-owner and exclude the failed account. Request finalization occurs only when retries are exhausted or a later attempt completes.

### Midstream failure

After bytes are emitted, no retry occurs. Submit one midstream terminal job with the appropriate typed transport/stream observation.

### Client cancellation

Submit or populate the retained cancellation job exactly once. Cancellation must not penalize provider health. Cleanup must continue after the client task exits.

### Normal completion

Completion must also use the same terminal ownership abstraction so success cannot race a cancellation/error finalizer.

## Implementation sequence

### Workstream A — Lifecycle map and characterization

Document every current call to:

- `RequestFinalizer.finalize()`;
- `AttemptFinalizer.finalize_failed_attempt()`;
- quota reservation removal;
- active count decrement;
- `HealthManager.release_request()`;
- failure-effects application.

Add tests proving the current double-entry and cancellation gap before restructuring.

### Workstream B — Extend retained finalization job

Make the retained job capable of owning all required transitions. Reuse existing repositories and effects applier; do not introduce a second persistence framework.

### Workstream C — Convert terminal paths

Convert one path at a time in this order:

1. local capability rejection;
2. streaming/non-streaming 4xx;
3. retryable attempt failure;
4. client cancellation;
5. midstream error;
6. normal completion.

After each conversion, remove the superseded direct cleanup calls.

### Workstream D — Conflict and duplicate handling

Add duplicate submission and conflicting outcome tests. The normal runtime should produce zero terminal conflicts.

### Workstream E — Shutdown/recovery behavior

Verify retained jobs drain on generation/application shutdown and cooperate with the existing finalization retry queue and database recovery controller.

Do not create an unbounded queue or a second background supervisor.

## Deterministic fault seams

Tests need explicit barriers around:

- before attempt transition;
- after attempt transition/before reservation release;
- after durable reservation release/before request transition;
- after request transition/before in-memory quota removal;
- before active-count decrement;
- before health-slot release;
- before/after effects application;
- before job registry removal.

Use events/hooks injected in tests. Do not depend on arbitrary sleeps.

## Required tests

### Capability rejection matrix

For streaming and non-streaming requests, cancel the caller at every seam above. Eventually assert:

- request terminal status is client error;
- attempt terminal status is error/rejected;
- durable reservation is released;
- in-memory quota reservation is absent;
- active request count returns to baseline;
- half-open probe is not held;
- no health failure/backoff/quarantine is applied;
- the next unrelated request succeeds without restart.

### Upstream 4xx matrix

- exactly one request finalization invocation/transition;
- exactly one attempt terminal transition;
- exactly one reservation release;
- zero duplicate account event/effect records;
- pass-through response body/status remain correct;
- streaming and non-streaming use the same terminal classification.

### Duplicate/conflict tests

- duplicate identical terminal job joins or returns the original result;
- conflicting terminal outcome is diagnosed and does not apply a second effect;
- job registry is bounded and removes completed entries;
- retry queue does not duplicate completed jobs.

### Database-failure tests

Inject failure/ambiguity at each durable step and assert bounded retry/recovery convergence. Database/finalization errors must not create provider cooldown or disablement.

### Shutdown tests

- process/generation shutdown drains completed jobs;
- a bounded timeout reports unfinished jobs truthfully;
- no coroutine/task is leaked;
- shutdown does not discard a job merely because the client disconnected.

## Acceptance criteria

- [ ] Every selected attempt has exactly one terminal owner.
- [ ] Streaming 4xx handling no longer finalizes before raising into a second terminal path.
- [ ] Local capability rejection performs one retained cleanup sequence.
- [ ] Caller cancellation cannot prevent durable reservation release, request finalization, quota removal, active-count decrement, or probe release.
- [ ] Duplicate identical terminal submissions are idempotent and do not repeat effects.
- [ ] Conflicting terminal outcomes are detected rather than silently accepted.
- [ ] Failure effects are applied at most once per attempt identity.
- [ ] Database/finalization failures create no provider health penalty.
- [ ] No test requires restart or database deletion to restore subsequent requests.
- [ ] The next unrelated request succeeds after every injected failure/cancellation case.
- [ ] Finalization registry and retry queue return to baseline after test convergence.
- [ ] Normal completion, cancellation, capability rejection, 4xx, retry exhaustion, and midstream errors all use the canonical terminal mechanism.
- [ ] Focused lifecycle tests pass without sleep-based race assumptions.
- [ ] Repository lint, formatting, typing, and affected integration tests pass.

## Explicit rejection conditions

Do not close Plan 047 if:

- a terminal path still calls both attempt and request finalizers independently outside the retained owner;
- duplicate finalization is tolerated only because SQL transitions are idempotent;
- only the database portion is shielded while in-memory releases remain cancellation-sensitive;
- cleanup completion is inferred from request status alone;
- a cancellation test omits active count, quota reservation, durable reservation, and probe assertions;
- the finalization job can grow without a bound or completed-entry removal;
- a database exception changes provider health;
- stream terminal-marker behavior is redesigned here instead of Plan 048.

## Handoff record

Record:

- implementation commit SHA;
- terminal call-site inventory before and after;
- canonical terminal job/result types;
- cancellation seam matrix and repetition count;
- duplicate/conflict results;
- registry/queue bounds;
- focused and repository test commands;
- any unresolved database recovery issue assigned explicitly to a follow-up.

## Definition of done

Plan 047 is complete when every terminal request path delegates to one retained, attempt-keyed lifecycle owner; the full durable and in-memory cleanup sequence converges despite caller cancellation or database delay; duplicate calls cannot repeat effects; conflicting outcomes are visible; and subsequent proxy traffic remains healthy without restart or database repair.