# Long-Running Dispatch Overhead Stability Roadmap

Date: 2026-07-15
Status: handoff roadmap
Scope: EggPool dispatch-path latency stability, SQLite contention, background scheduling, observability write pressure, long-running process behavior, and SBC-safe operational validation.

## Executive summary

EggPool's dispatch-overhead recorder is bounded, so the observed increase over process lifetime is not explained by an ever-growing telemetry window. The current code instead contains a credible contention-amplification path:

1. `RequestCoordinator._select_and_persist_attempt()` acquires the coordinator-wide `_select_lock`.
2. While holding `_select_lock`, it waits for the process-owned primary `Database` connection and enters a `BEGIN IMMEDIATE` transaction.
3. The transaction persists the pending request, reservation, and request attempt, commits, and then publishes in-memory active-request and quota-reservation state before `_select_lock` is released.
4. Any finalizer, cleanup task, metrics flush, catalog/model-info write, backoff write, or other primary-database operation can therefore make one selector wait for SQLite while still holding the global selection lock.
5. Every later request queues behind that selector, turning ordinary single-writer SQLite contention into process-wide head-of-line blocking.

There is also a concrete periodic scheduler defect in `SupervisedTask._run_periodic_loop()`: `initial_delay_s` is recomputed on every iteration rather than consumed once. Tasks configured with a short startup offset can consequently run at that short offset forever instead of their configured interval. This increases primary-database pressure and makes the lock convoy more frequent.

The roadmap below addresses the problem in seven ordered milestones. The first milestone fixes confirmed defects and establishes a reliable baseline. The second removes the lock-order amplification without weakening durability-before-dispatch semantics. The third introduces a bounded, batched dispatch persistence pipeline. The fourth removes lossy observability writes from the synchronous data plane. The fifth bounds maintenance transaction size and improves SQLite operational hygiene. The sixth hardens multi-loop/thread behavior and remaining hot paths. The final milestone supplies long-running soak proof, rollout guardrails, and operator-facing diagnostics.

## Architectural invariants

The implementation must preserve the following invariants throughout the roadmap:

- A request must not be sent upstream until its correctness-critical pending request, reservation, and attempt state is durably committed.
- A selected account's health/circuit slot, active-request count, and quota reservation must not be leaked on cancellation or persistence failure.
- No retry may double-create or double-release a reservation.
- Request finalization remains idempotent.
- Billing, quota, health, retry, crash recovery, and routing fairness must not depend on routing trace or dashboard observability rows.
- Granian remains a single worker process. This roadmap may validate runtime thread counts but does not introduce multi-process application state.
- SQLite remains the durable store. Replacing SQLite or aiosqlite is not required for this line of work.
- Default operation remains suitable for Raspberry Pi and other SBC-class systems.
- Live rehash must not leak generation-owned supervisors, clients, queues, or metrics objects.

## Goals

1. Keep dispatch p50, p95, and p99 stable over multi-hour and multi-day operation under a representative mix of native, transcoded, streaming, retrying, and cancelled requests.
2. Eliminate the coordinator lock convoy in which a selector holds `_select_lock` while waiting for SQLite.
3. Reduce SQLite transaction and commit overhead for correctness-critical dispatch persistence without weakening durability.
4. Ensure background tasks run at their configured cadence and cannot monopolize the primary database.
5. Move diagnostic routing traces and other lossy observability writes off the synchronous dispatch path.
6. Bound retention, reconciliation, rollup, and cleanup work by row count and wall-clock budget.
7. Add diagnostics that identify whether latency is caused by routing computation, selection lock wait, database queueing, transaction execution, event-loop lag, filesystem/WAL behavior, or upstream connection establishment.
8. Provide automated soak gates that detect time-dependent regressions rather than only instantaneous benchmark regressions.

## Non-goals

- Do not weaken durability-before-upstream-dispatch.
- Do not make correctness-critical writes lossy.
- Do not add multiple Granian workers.
- Do not replace routing policy, quota semantics, or provider retry behavior.
- Do not make dashboard aggregates authoritative for billing or request lifecycle state.
- Do not use a detached external helper process for request persistence.
- Do not optimize by silently dropping request, reservation, attempt, or finalization records.
- Do not require Rust extensions in the first implementation. Native acceleration may be reconsidered only after Python/SQLite contention is corrected and measured.

## Current evidence and target surfaces

Primary code surfaces:

- `src/eggpool/background/__init__.py`
  - `SupervisedTask._run_periodic_loop()`
  - task cadence and heartbeat diagnostics
- `src/eggpool/runtime_tasks.py`
  - registration intervals and startup offsets
- `src/eggpool/request/coordinator.py`
  - `RequestCoordinator._select_and_persist_attempt()`
  - `_select_lock`
  - routing-trace persistence
  - upstream dispatch timing
- `src/eggpool/db/connection.py`
  - single connection lock
  - `BEGIN IMMEDIATE` transactions
  - lock-wait telemetry
- `src/eggpool/request/finalizer.py`
  - large request finalization transaction
  - interaction with metrics coalescing and quota/health release
- `src/eggpool/request/finalization_queue.py`
  - retry queue behavior under SQLite contention
- `src/eggpool/metrics/buffer.py`
  - rollup buffering and flush behavior
- `src/eggpool/background/cleanup.py`
  - retention, reservation reconciliation, and checkpoint work
- `src/eggpool/runtime_dispatch.py`
  - coarse dispatch and named span recorders
- `src/eggpool/runtime_metrics.py`
  - process/runtime diagnostics
- `src/eggpool/runtime_manager.py`
  - generation retirement and resource closure
- `src/eggpool/providers/client_pool.py`
  - client ownership and connection-pool diagnostics
- `src/eggpool/providers/dns_cache.py`
  - bounded DNS state and resolver diagnostics
- `src/eggpool/api/proxy_request.py`
  - request preprocessing and dispatch timing boundary
- `src/eggpool/models/config.py`
  - operational knobs and defaults

## Milestone sequence

### Milestone A — Scheduler correctness and stable baseline

Plan file: `plans/2026-07-15-dispatch-stability-milestone-a-scheduler-and-baseline.md`

Fix one-time `initial_delay_s` consumption, verify all periodic task cadences, improve timing-boundary documentation, and establish a repeatable dispatch/database/background-task baseline. This milestone must land first because later performance measurements are invalid while tasks run at unintended frequencies.

Deliverables:

- periodic scheduler regression fix;
- cadence tests covering first and subsequent ticks;
- runtime diagnostics for actual interval and scheduling drift;
- dispatch timing boundary clarification;
- baseline benchmark/soak harness and captured fixtures;
- no change to request durability or routing semantics.

### Milestone B — Selection critical-section deconvoying

Plan file: `plans/2026-07-15-dispatch-stability-milestone-b-selection-lock-deconvoying.md`

Refactor selection so a request never holds the coordinator-wide selection lock while waiting for the database. Introduce an explicit in-memory claim/rollback state machine that preserves circuit, active-count, and quota invariants around persistence failure and cancellation.

Deliverables:

- documented lock ordering;
- narrow, bounded selection claim section;
- persistence outside the global selection lock;
- deterministic rollback for every failure point;
- cancellation and retry race tests;
- lock-held and lock-wait performance gates.

### Milestone C — Durable dispatch write pipeline and microbatching

Plan file: `plans/2026-07-15-dispatch-stability-milestone-c-durable-write-pipeline.md`

Create a dedicated in-process writer for correctness-critical dispatch intents. The writer may gather a small bounded microbatch, persist each request/reservation/attempt bundle in one transaction, commit once, and resolve per-request futures only after durability. Low-volume traffic must not incur an unnecessary fixed batching delay.

Deliverables:

- `DispatchIntent`/result contract;
- bounded queue and backpressure policy;
- repository-level dispatch bundle persistence;
- adaptive microbatch policy;
- per-intent commit acknowledgement;
- graceful shutdown and reload ownership;
- failure isolation and queue saturation tests.

### Milestone D — Off-path observability and trace pressure control

Plan file: `plans/2026-07-15-dispatch-stability-milestone-d-off-path-observability.md`

Remove routing trace writes and other diagnostic-only rows from synchronous dispatch. Add a bounded, coalesced observability writer with explicit drop accounting. Harden span-recorder synchronization and make detailed tracing sampleable without losing the always-on coarse dispatch metric.

Deliverables:

- asynchronous routing-trace buffer/writer;
- bounded drop policy and diagnostics;
- no trace transaction before upstream send;
- safe recorder snapshotting;
- optional detailed-span sampling;
- dashboard behavior for sampled/dropped trace data.

### Milestone E — Bounded maintenance and SQLite hygiene

Plan file: `plans/2026-07-15-dispatch-stability-milestone-e-bounded-maintenance-and-sqlite-hygiene.md`

Convert retention, reconciliation, backfill, and rollup cleanup work into bounded batches with row and time budgets. Add WAL/database-size diagnostics and operational thresholds so a maintenance tick cannot monopolize the sole writer connection.

Deliverables:

- chunked request/event/trace/rollup retention;
- bounded reservation and stale-request reconciliation;
- per-task row/time budgets;
- event-loop yields between batches;
- WAL checkpoint and database growth telemetry;
- index/query-plan verification;
- interrupted-cleanup resume tests.

### Milestone F — Runtime concurrency and hot-path hardening

Plan file: `plans/2026-07-15-dispatch-stability-milestone-f-runtime-concurrency-and-hot-path-hardening.md`

Verify Granian multi-runtime-thread behavior against shared asyncio primitives, repair unsafe or misleading synchronization, and remove remaining avoidable request-path allocations and repeated parsing. Establish a supported runtime-thread matrix rather than assuming additional event loops improve performance.

Deliverables:

- cross-loop/thread safety audit and tests;
- supported `server.threads` guidance;
- metrics buffer synchronization correction;
- bounded/sharded telemetry contention where justified;
- request preprocessing allocation reductions;
- provider/header/config lookup precomputation;
- no capability regression in transcoding, compression, or cache synthesis.

### Milestone G — Long-running proof, rollout, and operational closure

Plan file: `plans/2026-07-15-dispatch-stability-milestone-g-soak-validation-and-rollout.md`

Run multi-hour and accelerated multi-day-equivalent workloads, compare early and late latency windows, verify resource plateaus, exercise rehash and cancellation, and define rollout/rollback thresholds for SBC and general-host profiles.

Deliverables:

- deterministic mock-upstream soak suite;
- native/transcoded/streaming/cancellation/retry workload matrix;
- early-versus-late latency stability assertions;
- RSS/FD/thread/WAL/database/queue plateau checks;
- rehash retirement checks;
- Pi-class benchmark profile;
- operator runbook and release acceptance report.

## Dependency graph

```text
A: scheduler correctness + baseline
            |
            v
B: deconvoy selection lock
            |
            v
C: durable dispatch writer
            |
            +------------------+
            |                  |
            v                  v
D: off-path observability   E: bounded maintenance
            \                  /
             \                /
              v              v
        F: runtime/hot-path hardening
                    |
                    v
        G: soak, rollout, closure
```

Milestone D can begin after B if C's writer contract is sufficiently stable, but final integration must account for the process/generation ownership decisions made in C. Milestone E may proceed in parallel with D. Milestone F should not finalize runtime-thread guidance until B through E have removed known sources of artificial contention. Milestone G is the release gate for the complete roadmap.

## Ownership model

The durability writer and correctness-critical queue should be process-owned because the primary database is process-owned and must survive generation swaps. Each intent must carry immutable data derived from the leased generation, including account ID/name, provider/model/protocol, request identifiers, and reservation estimates. The writer must not reach back into a mutable or retired generation to resolve fields after enqueue.

Routing-trace and other observability writers should also be process-owned when they write to process-owned SQLite. Generation-specific configuration should be represented as immutable per-event policy fields or transitioned through the established task-spec/reload mechanism. A rehash must not duplicate process-owned writers.

## Required observability contract

By the end of the roadmap, `/api/stats/runtime` or an equivalent internal snapshot should expose at least:

- coarse dispatch overhead p50/p95/p99 and sample count;
- named dispatch spans, including routing-plan, selection-claim wait/held, dispatch-intent queue wait, database transaction execution, commit wait, trace enqueue, and upstream request build;
- primary DB lock-wait p50/p95/p99/max and recent sample count;
- dispatch writer queue depth, capacity, oldest age, batch size, batch duration, commit duration, enqueue rejects, and failed intents;
- observability queue depth, drops by reason, rows written, and flush duration;
- finalization retry queue depth and oldest age;
- background task configured interval, actual interval, schedule drift, tick duration, rows processed, and remaining work;
- database file size, WAL size, last checkpoint result/duration, and retention backlog;
- event-loop lag or scheduler delay;
- active/retiring runtime generations and retirement age;
- provider HTTP client pool construction count and available pool diagnostics where supported;
- RSS, open file descriptors, and thread count where the platform permits.

No diagnostic payload may expose API keys, request content, authorization headers, or unredacted provider errors.

## Testing strategy

Every milestone must add focused unit and integration tests. The complete roadmap additionally requires:

1. Single-request latency tests to prevent microbatching from adding a fixed delay at low volume.
2. High-concurrency dispatch tests with an intentionally blocked database writer.
3. Cancellation at every state boundary: before claim, after claim, during enqueue, before commit, after commit, during upstream connect, and during streaming finalization.
4. Persistence failure injection for request, reservation, attempt, commit, and post-commit publication.
5. Retry tests proving attempted-account and reservation semantics remain exact.
6. Background task cadence tests over at least three iterations.
7. Retention tests with large synthetic row counts and a strict maximum batch size.
8. Rehash tests proving process-owned writers are not duplicated and generation retirement remains bounded.
9. Multi-runtime-thread tests or an explicit fail/guard path if shared object graphs cannot safely span loops.
10. Long-running soak assertions comparing warm early windows with late windows.

## Performance and stability acceptance targets

Targets should be calibrated against the existing CI host and at least one Pi-class system, but the release gate must include relative stability constraints:

- After a warm-up period, last-hour dispatch p95 must be no greater than 1.20x first-hour p95 under a stationary workload.
- Last-hour dispatch p99 must be no greater than 1.50x first-hour p99.
- Selection claim lock-held p95 should remain below 5 ms on the CI host and below 15 ms on the reference SBC, excluding deliberate fault injection.
- No request may hold the selection claim lock while waiting for SQLite.
- Dispatch writer queue depth must return to baseline after a burst and must not grow monotonically under sustainable load.
- Finalization retry queue depth must return to zero or a stable low baseline after cancellation bursts.
- No periodic task may run more frequently than its configured interval after its one-time initial delay, allowing normal scheduler tolerance.
- No maintenance transaction may exceed its configured row budget; wall-clock budget overruns must stop the pass after the current transaction.
- Routing trace loss under normal sustainable load should be zero; overload drops must be explicit and must not affect requests.
- RSS, file descriptors, threads, retiring generations, and WAL size must plateau within documented bounds.
- Existing routing, billing, retry, transcode, compression, cache, dashboard, rehash, and security test suites must pass.

## Rollout principles

- Land milestones separately with benchmark evidence attached to each implementation commit or PR.
- Keep new writer/batching behavior behind conservative configuration or an internal feature flag until failure injection and soak coverage pass.
- Provide an immediate fallback to the pre-batched direct persistence path during milestone C rollout, but do not retain two divergent correctness implementations indefinitely.
- Default routing traces remain sampled; the asynchronous trace writer must support `all`, `sampled`, and `off` modes.
- Keep `database.worker_threads = 2` as the recommended file-backed dashboard profile.
- Do not increase Granian runtime threads as a substitute for fixing SQLite and lock contention.
- For SBC release validation, test both low-wear storage and a faster SSD-backed configuration where available.

## Documentation deliverables

Update the following as applicable during implementation:

- `architecture/README.md` with the dispatch claim/persistence state machine and lock ordering;
- `docs/deployment.md` with supported runtime/database profiles;
- `docs/raspberry-pi.md` with long-running stability guidance and WAL/retention recommendations;
- `config.example.toml` with any new queue, batching, maintenance-budget, or tracing knobs;
- runtime/dashboard tooltips explaining dispatch overhead versus upstream connect/TTFT;
- `CHANGELOG.md` with user-visible defaults and operational changes.

## Definition of roadmap completion

This roadmap is complete only when all milestone plan acceptance criteria are satisfied, the long-running soak suite passes without time-dependent dispatch degradation, the operator can identify active contention from runtime diagnostics, process-owned writers survive rehash without duplication, and the default file-backed SBC profile runs for an extended period without monotonic growth in dispatch latency, retry queues, retiring generations, file descriptors, RSS, or WAL size.