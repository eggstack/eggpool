# Phase 9 — Readiness Probing and SQLite Contention Reduction

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phase 1; coordinate with Phases 7–8.

## Objective

Remove SQLite write activity from the `/readyz` request path. Replace per-request writable probes with a bounded process-owned background probe whose freshness-aware result is read cheaply by readiness handlers.

This phase reduces avoidable primary connection lock contention, especially under frequent orchestrator polling and on constrained SBC deployments.

## Problem statement

The current readiness route calls a writable database probe. The probe begins a transaction, writes a health-probe row, and rolls back. Even though no durable row remains, every readiness request enters the serialized SQLite write path and can contend with dispatch persistence, finalization, catalog maintenance, and checkpoint work.

A health endpoint should observe health, not create routine write pressure proportional to polling frequency.

## Non-goals

- Do not weaken readiness so database write failure remains undetected indefinitely.
- Do not replace the normal database connection architecture.
- Do not move all health checks into a new monitoring system.
- Do not perform remote provider probes from `/readyz`.
- Do not cache readiness forever or treat stale probe success as healthy.

## Process-owned probe service

Introduce a process-owned `DatabaseWritableProbe` or equivalent. It should start after database initialization and stop before database close.

State should include:

- current status: unknown, healthy, unhealthy, stale, stopped;
- last attempt timestamp;
- last success timestamp;
- last failure timestamp;
- last error class and bounded redacted message;
- last probe duration;
- consecutive failure count;
- configured interval;
- freshness deadline;
- worker task health.

The service must not be generation-owned and must not restart on rehash.

## Probe operation

Retain a real writable check rather than substituting a read-only query. The check may use the existing insert-and-rollback mechanism or a dedicated ephemeral probe table, but it should execute only on the configured cadence.

Requirements:

- use a short bounded timeout;
- avoid holding the primary writer lock longer than necessary;
- do not perform schema or maintenance work;
- classify lock timeout separately from corruption/readonly/closed errors;
- consume worker exceptions and continue according to backoff policy;
- support an immediate initial probe during startup.

Consider using a dedicated lightweight connection only if that does not violate existing single-writer coordination. The design must preserve SQLite correctness rather than bypass the repository lock unsafely.

## Scheduling and backoff

Suggested defaults:

- healthy interval: 5–15 seconds;
- failure retry: bounded exponential backoff capped near the healthy interval or a documented maximum;
- readiness freshness threshold: greater than one healthy interval but short enough to detect a stopped worker;
- optional jitter to avoid synchronized probes across multiple instances.

Use repository configuration conventions. Defaults should remain conservative for Raspberry Pi-class systems.

## Readiness semantics

`/readyz` should read the cached probe snapshot and combine it with active-generation/process lifecycle state.

Recommended outcomes:

- no initial probe yet: not ready or startup-pending;
- recent success: database writable;
- recent failure: not ready with stable reason;
- result older than freshness threshold: not ready/stale;
- probe worker stopped unexpectedly: not ready;
- process shutting down: not ready.

The route must not await a new write probe. It may read immutable state under a short lock.

Keep response fields stable where possible. Additional fields may include probe age, last success age, and stable failure class, but never raw SQL or secrets.

## Explicit diagnostic command

Retain or add a direct database writable probe for operator diagnostics, separate from routine HTTP readiness. This may be exposed through an internal CLI/status command with clear warning that it performs a write transaction.

Do not have dashboard polling invoke the direct probe.

## Startup and shutdown

Startup sequence:

1. initialize/migrate database;
2. construct probe service;
3. perform or schedule immediate probe;
4. only report ready after a fresh successful result and active generation publication.

Shutdown sequence:

1. mark readiness false;
2. stop/cancel the probe worker with bounded cleanup;
3. close database after worker exit;
4. leave no pending probe task.

Reload must not recreate or interrupt the process-owned probe.

## Metrics and diagnostics

Expose:

- probe attempts/successes/failures;
- probe latency;
- lock-timeout count;
- consecutive failures;
- result age;
- worker running state;
- readiness requests by outcome;
- SQLite primary lock-wait metrics before/after the change.

Avoid high-cardinality labels based on exception messages.

## Required tests

### No write in request path

Instrument the database/repository and issue repeated `/readyz` requests. Assert no probe transaction or write method is invoked by the route.

### Initial state

Before first probe completion, readiness reports startup-pending/unready. After successful probe, it reports ready.

### Failure and recovery

Inject readonly/closed/lock-timeout errors, assert unready state, then restore the database and assert recovery after a successful probe.

### Staleness

Pause the worker using a deterministic clock/barrier and advance beyond freshness threshold. Readiness must become stale/unready despite last success.

### Worker failure

Force the worker task to fail unexpectedly. Assert exception consumption, diagnostics, and unready state.

### Reload parity

Perform multiple rehashes and assert the same process-owned probe task remains active and readiness uses the active generation from Phase 7.

### Shutdown hygiene

Assert probe task exits before database close and no task remains pending.

### Contention benchmark

Under fixed dispatch load, compare frequent readiness polling before/after:

- write transaction count;
- primary SQLite lock wait p95/p99;
- dispatch overhead p95/p99;
- readiness latency;
- database consistency.

## Implementation sequence

1. Add a regression test proving `/readyz` currently invokes the write probe.
2. Define probe state and immutable snapshot types.
3. Implement process-owned worker and lifecycle.
4. Change `/readyz` to cached state.
5. Add freshness and worker-health semantics.
6. Add metrics and runtime diagnostics.
7. Preserve an explicit direct diagnostic probe.
8. Add reload/shutdown coverage.
9. Run the contention benchmark.
10. Document configuration defaults and failure interpretation.

## Acceptance criteria

- `/readyz` performs no SQLite write transaction.
- Database writability is still checked on a bounded cadence.
- Stale or failed probe state makes the service unready within the documented interval.
- Probe worker is process-owned and unaffected by rehash.
- Worker failures are consumed and visible.
- Startup does not report ready before a fresh successful probe and active generation.
- Shutdown leaves no probe task accessing a closed database.
- Frequent readiness polling no longer materially increases writer transaction count or lock wait.
- Fixed-load evidence shows improved or non-regressed dispatch latency and correct readiness behavior.

## Handoff evidence

Provide focused tests, configured interval/freshness values, readiness response examples for healthy/failure/stale states, task-lifecycle evidence, and before/after contention metrics.