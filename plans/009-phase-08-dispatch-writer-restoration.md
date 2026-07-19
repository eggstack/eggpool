# Phase 8 — Dispatch Writer Restoration and Persistence Parity

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1, 5, 6, and 7.

## Objective

Restore the intended dispatch-persistence microbatch path, ensure it is selected whenever enabled, preserve its wiring across reloads, and add enough instrumentation to verify that it reduces SQLite lock contention without changing request-accounting semantics.

## Problem statement

The process can construct and start a `DispatchPersistenceWriter`, then pass the object into the request coordinator while leaving the coordinator’s `use_dispatch_writer` selection false. The writer exists and consumes process resources, but request dispatches continue through direct database transactions. Reload candidate construction can also omit the writer entirely.

This defeats the intended batching optimization and likely contributes to elevated dispatch overhead and primary SQLite lock wait under concurrency.

## Non-goals

- Do not replace SQLite.
- Do not change request/reservation/attempt accounting semantics.
- Do not make the writer silently drop persistence under pressure.
- Do not introduce an unbounded queue.
- Do not claim performance improvement without before/after measurements.
- Do not make writer configuration live-reloadable unless Phase 6 provides a safe process transition.

## Configuration contract

Define one authoritative enablement rule:

- writer disabled: process worker is not started and coordinators use direct persistence;
- writer enabled: process worker is started, supplied to every generation, and selected by every coordinator;
- invalid partial state, such as enabled with no writer object, fails startup/candidate preparation rather than silently falling back.

Prefer deriving coordinator selection from a typed writer capability or non-null writer plus explicit config. Avoid maintaining two independent booleans that can drift.

If direct fallback remains supported for writer failure, it must be an explicit policy with metrics, bounded behavior, and atomicity tests. It must not be the accidental default.

## Service-graph integration

Update Phase 5’s shared runtime factory so every coordinator receives:

- the process-owned writer object;
- the authoritative enabled state;
- enqueue timeout/backpressure policy;
- any request-finalization callback required by the writer path.

The generation must not close the process-owned writer during retirement.

Startup and reload parity tests should compare writer object identity and selected mode.

## Persistence semantics

Document the dispatch bundle committed by the writer. At minimum verify atomic treatment of related rows such as:

- request dispatch/update state;
- reservation state;
- attempt record;
- routing/selection metadata;
- any quota or accounting rows currently written in the direct path.

The writer path and direct path must produce equivalent database state for:

- success;
- upstream error;
- cancellation before dispatch;
- cancellation after provider selection;
- retry/fallback attempt;
- finalization failure;
- duplicate/idempotent submission behavior.

If the direct path exists as a disabled-mode implementation, consolidate shared SQL/repository logic so semantics cannot diverge.

## Queue and backpressure policy

Specify:

- bounded maximum queue size;
- enqueue timeout or non-blocking behavior;
- batch size;
- maximum batch delay;
- shutdown drain timeout;
- behavior when the writer task is unhealthy;
- whether callers await durable persistence before proceeding.

Recommended policy:

- bounded queue;
- short, measured enqueue wait;
- no silent drop;
- writer-health failure becomes a bounded request/persistence failure or explicit direct fallback;
- shutdown stops admission, drains accepted entries, then closes the worker.

For SBC targets, defaults should avoid large memory spikes while still reducing transaction count.

## Writer health and observability

Expose:

- enabled and selected state;
- worker running/failed state;
- queue capacity, depth, and utilization;
- enqueue count and enqueue wait histogram;
- batch count and batch-size histogram;
- flush latency;
- primary SQLite lock-wait contribution if available;
- direct-path count;
- fallback count and reason;
- rejected/timed-out enqueue count;
- last writer error;
- shutdown-drain outcome.

Dashboard exposure is optional in this phase, but metrics and runtime diagnostics must be available.

## Live configuration handling

Classify writer fields explicitly:

- safe live fields only if they can be changed through a Phase 6 typed transition;
- queue capacity, worker count, or ownership-changing settings may remain restart-required;
- timing thresholds may be live only when the worker supports atomic reconfiguration.

Do not mutate the active writer during candidate preparation.

## Required tests

### Selection regression

With writer enabled, assert coordinator uses the writer path and the direct repository transaction path is not invoked.

With writer disabled, assert no worker is started and direct persistence remains correct.

### Startup/reload parity

After one and multiple reloads, assert:

- writer object identity remains process-owned and stable;
- coordinator selection remains enabled;
- writer queue/worker remains healthy;
- generation retirement does not close the writer.

### Semantic equivalence

Run identical dispatch scenarios through writer-enabled and direct modes. Compare normalized database rows and accounting outcomes.

### Backpressure

Fill the queue deterministically with a barrier-controlled writer. Assert configured enqueue behavior, no silent loss, and useful metrics.

### Writer failure

Inject worker failure before and during a batch. Assert explicit policy, task exception consumption, database consistency, and diagnostic state.

### Shutdown

Stop admission, enqueue accepted bundles, initiate shutdown, and assert accepted work drains or reports a bounded failure according to policy. No worker task remains pending.

### Performance contract

Under a fixed concurrent workload, collect before/after:

- dispatch overhead p50/p95/p99;
- SQLite primary lock-wait p50/p95/p99;
- transaction count;
- writer batch-size distribution;
- request/accounting consistency.

Use generous non-flaky thresholds but require evidence that batching is active, such as materially fewer transactions than dispatch bundles.

## Implementation sequence

1. Add a failing test showing enabled writer is not selected.
2. Define authoritative writer enablement and invalid states.
3. Update coordinator construction and shared factory.
4. Consolidate writer/direct persistence semantics.
5. Add health state and metrics.
6. Add deterministic queue/backpressure tests.
7. Add startup/reload parity tests.
8. Add failure and shutdown coverage.
9. Run fixed-load benchmark before/after.
10. Classify writer config fields for live vs restart-required handling.

## Acceptance criteria

- Enabling the dispatch writer causes all production coordinators to use it.
- Reload does not disable, replace, or omit the process-owned writer.
- Writer and direct modes produce equivalent persisted state.
- The queue is bounded and never silently drops accepted work.
- Writer task failures are consumed and operator-visible.
- Shutdown handles accepted queue entries according to a documented bounded policy.
- Metrics prove batching is active and identify fallback/direct writes.
- Fixed-load evidence shows reduced transaction count and improved or non-regressed lock-wait/dispatch latency.
- No request, reservation, attempt, or accounting row is lost or duplicated.

## Handoff evidence

Provide focused selection and equivalence tests, runtime diagnostic output, before/after transaction and latency metrics, queue-pressure results, and confirmation that writer ownership remains process-level across generation retirement.