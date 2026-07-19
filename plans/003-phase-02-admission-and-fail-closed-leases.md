# Phase 2 — Atomic Reload Admission and Fail-Closed Runtime Leases

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisite: Phase 1 deterministic barriers and state snapshots.

## Objective

Close two immediate high-severity lifecycle defects without waiting for the full transactional reload redesign:

1. Reload admission currently uses a check-then-await-then-lock sequence, allowing two callers to observe an unlocked state and causing the second caller to queue instead of receiving an immediate busy response.
2. Request handling can fall back to a legacy `app.state` coordinator when runtime-generation lease acquisition fails, potentially using a stale or retired generation.

After this phase, admission is atomic and request handling fails closed whenever the installed runtime manager cannot provide a safe generation lease.

## Non-goals

- Do not redesign candidate construction or persistence ordering.
- Do not make generation retirement asynchronous in this phase.
- Do not remove all `app.state` mirrors yet; Phase 7 handles active-generation authority.
- Do not broaden HTTP retry policy or provider-routing behavior.
- Do not hide unexpected runtime-manager failures by converting every exception to a generic success path.

## Workstream A — Atomic reload admission

### Required behavior

A reload operation must atomically claim the single reload slot before performing any awaitable work. A competing operation must return `reload_in_progress` immediately and must not wait for the active reload lock.

The following sequence is prohibited:

1. check `lock.locked()`;
2. await event persistence or logging;
3. acquire the lock.

The claim must cover the complete operation from admission through terminal finalization.

### Recommended design

Introduce a small admission primitive owned by `ReloadManager`. Acceptable forms include:

- an admission lock protecting a boolean claim;
- a dedicated `ReloadAdmission` object with synchronous/atomic claim semantics under the event loop;
- a one-slot semaphore wrapper exposing `try_acquire()` without waiting.

A clear implementation shape is:

- acquire a short internal mutex;
- inspect and set `_reload_claimed` while holding that mutex;
- release the mutex;
- if already claimed, return or raise `ReloadInProgressError`;
- run the reload body;
- clear `_reload_claimed` in a `finally` block under the same mutex.

The short mutex must never cover validation, database work, candidate construction, publication, or retirement. It protects only claim state.

### Event and audit ordering

Persist `reload_requested` only after the claim succeeds. For rejected competing operations, record a separate bounded `reload_rejected_busy` event if desired, but event persistence must not delay or alter the busy decision.

Audit-write failures must not release the claim or admit a second operation. Existing policy for best-effort operational events should be retained and tested.

### Cancellation and shutdown

The claim must be released when:

- validation fails;
- candidate construction fails;
- the operation is cancelled before commit;
- publication fails;
- an unexpected exception occurs;
- process shutdown begins.

If cancellation is intentionally shielded during a future commit stage, that policy belongs to Phase 6. For this phase, ensure every current terminal path clears admission state.

### Diagnostics

Expose enough internal state for tests and diagnostics:

- whether a reload claim is active;
- admitted request ID;
- claim timestamp;
- optional current stage.

Do not expose secrets or entire config payloads.

## Workstream B — Fail-closed runtime lease acquisition

### Required behavior

Once `request.app.state.runtime_manager` exists, every request path that uses generation-owned services must acquire a lease from it. Failure to acquire a lease must not fall back to `request.app.state.coordinator` or other startup mirrors.

Expected lifecycle errors such as exhaustion, shutdown, or no active generation should produce a bounded service-unavailable response. Unexpected acquisition errors should be logged with structured context and should also fail the request rather than invoking stale state.

### Error mapping

Map the runtime manager’s explicit lease-unavailable exception to HTTP 503 using the same protocol-specific response envelope already used for upstream availability errors where appropriate.

Include:

- stable error type/code;
- concise message such as `runtime generation unavailable`;
- request correlation ID if available;
- retryability consistent with existing API semantics.

Do not expose internal object state or exception traces in the client response.

### Compatibility for tests and minimal applications

Some unit tests may construct a request application without a runtime manager. Preserve a narrowly defined compatibility path only when the runtime manager attribute is absent by construction.

The distinction must be explicit:

- runtime manager absent: legacy test/minimal-app coordinator path may be used;
- runtime manager present but acquisition fails: return 503, never fall back.

Add a comment or helper making this boundary clear so future code does not reintroduce a broad exception fallback.

### Lease lifetime audit

Review request paths to ensure the lease remains held through all awaits that use generation-owned objects, including:

- routing and provider selection;
- request dispatch;
- streaming iteration;
- finalization work using generation-owned calculators or services;
- cancellation cleanup.

Do not release the lease immediately after selecting the coordinator if the stream continues using generation-owned clients.

## Required tests

### Admission tests

Using Phase 1 barriers:

1. Release two reload callers simultaneously.
2. Hold the accepted operation at a deterministic post-admission barrier.
3. Assert one caller enters validation.
4. Assert the second returns busy before the first is released.
5. Assert the second never constructs a candidate or mutates persistence.
6. Repeat at least 100 times in a focused test.

Add terminal-path tests proving the claim is released after:

- validation rejection;
- restart-required rejection;
- semantic no-op;
- candidate failure;
- publication failure;
- cancellation;
- unexpected exception.

### Lease tests

Cover:

- explicit lease exhaustion returns 503;
- manager shutdown returns 503;
- unexpected acquisition exception returns bounded 503 and logs the error;
- legacy path works only when no runtime manager is installed;
- stale coordinator fake is never invoked after manager acquisition failure;
- long-lived streaming request retains its lease until stream completion/cancellation;
- request cancellation releases exactly one lease.

### Regression tests

- Existing successful request and streaming behavior remains unchanged.
- Existing control protocol busy response remains compatible.
- Reload operational events remain bounded even if event persistence fails.

## Implementation sequence

1. Add or finalize `ReloadAdmission` tests against current behavior.
2. Implement the atomic claim primitive.
3. Move event persistence after successful claim.
4. Route all reload exits through claim release in `finally`.
5. Add claim state to internal diagnostics.
6. Narrow request runtime-manager exception handling.
7. Add explicit 503 mapping for lease-unavailable errors.
8. Audit streaming lease lifetime.
9. Remove broad fallback code when the manager exists.
10. Run repeated concurrency tests and full suite.

## Acceptance criteria

- Exactly one of two simultaneous reload calls is admitted.
- The rejected call returns busy without waiting for completion of the accepted call.
- Rejected calls perform no candidate, database, process-supervisor, or writer work.
- Admission state is released on every terminal path.
- Runtime-manager lease failure returns a bounded HTTP 503.
- No request uses a legacy coordinator when a runtime manager is installed.
- Streaming requests hold and release generation leases correctly.
- No stale or closed instrumented resource is invoked in lease-failure tests.
- Focused concurrency test passes for at least 100 repeated runs.
- No new sleeps, non-strict xfails, or broad exception swallowing are introduced.

## Handoff evidence

Record:

- the new admission primitive and its ownership contract;
- focused admission and lease test commands;
- repeated-run results;
- before/after control response behavior for concurrent rehash;
- any protocol-visible 503 envelope change;
- confirmation that no production path falls back to `app.state.coordinator` after manager installation.