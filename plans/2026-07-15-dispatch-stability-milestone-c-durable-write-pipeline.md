# Dispatch Stability Milestone C — Durable Dispatch Write Pipeline

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: C of G
Depends on: Milestones A and B

## Objective

Replace per-request correctness-critical dispatch transactions with a process-owned, bounded in-process persistence pipeline that can microbatch concurrent dispatch intents into fewer SQLite transactions while preserving the invariant that no upstream request is sent until its own request, reservation, and attempt state is durably committed.

This milestone addresses transaction and commit amplification after milestone B removes the global selection lock convoy. It is not a lossy metrics queue. Every accepted dispatch intent must receive an explicit durable success or failure result.

## Problem statement

The current first-attempt path performs at least three SQLite statements in a transaction before upstream dispatch:

- create pending request;
- create reservation;
- create request attempt.

Retries add a request update and another reservation/attempt pair. Under concurrent traffic, each request independently waits for the single primary connection, enters `BEGIN IMMEDIATE`, executes its statements, and commits. Even after removing `_select_lock` from the database wait, transaction setup, worker-thread crossings, WAL writes, and commit overhead remain serialized.

A dedicated writer can preserve correctness while amortizing transaction overhead across requests that arrive within a very small concurrency window.

## Architectural decision

Add one process-owned `DispatchPersistenceWriter` attached to `ProcessRuntime`. Request coordinators from all runtime generations submit immutable `DispatchIntent` objects and await a per-intent future. The writer owns queue draining and primary-database dispatch bundle persistence.

Core contract:

```text
request generation lease
  -> routing plan
  -> selection claim
  -> build immutable DispatchIntent
  -> enqueue to process-owned writer
  -> await result
  -> writer persists one or more intents in a single transaction
  -> commit
  -> writer resolves each successful intent
  -> coordinator publishes in-memory state
  -> upstream send
```

No future is resolved successfully before commit completes.

The writer must not depend on mutable generation services after enqueue. The intent contains all persistence fields required to write the rows. The writer may use process-owned repositories or direct repository functions over the process-owned database.

## Scope

### In scope

- Process-owned dispatch persistence writer and lifecycle.
- Immutable dispatch intent/result/error contracts.
- Bounded queue and explicit backpressure.
- Adaptive microbatching with no mandatory delay for isolated requests.
- Repository-level bundle persistence.
- Per-intent commit acknowledgement.
- Failure isolation, cancellation semantics, shutdown draining, and rehash safety.
- Runtime diagnostics and configuration knobs.
- Direct-path compatibility only as a temporary rollout seam.

### Out of scope

- Finalization batching unless required for shared writer architecture; finalization remains a separate correctness path in this milestone.
- Routing trace persistence; handled in milestone D.
- Retention/maintenance batching; handled in milestone E.
- Multiple writer processes or external daemons.
- Multiple SQLite write connections. SQLite remains single-writer and the process-owned primary connection remains authoritative.

## Target files and modules

Potential new modules:

- `src/eggpool/request/dispatch_writer.py`
- `src/eggpool/request/dispatch_intent.py`
- `src/eggpool/db/dispatch_repository.py`

Existing modules:

- `src/eggpool/runtime_manager.py`
- `src/eggpool/app.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/db/repositories.py`
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/runtime_tasks.py`
- `src/eggpool/models/config.py`
- `src/eggpool/config_reload_policy.py`
- `src/eggpool/control/reload_manager.py`

Tests:

- new writer unit tests;
- database bundle integration tests;
- coordinator failure/cancellation tests;
- rehash ownership tests;
- milestone A performance harness.

## Workstream C1 — Define immutable persistence contracts

### `DispatchIntent`

Suggested fields:

- enqueue token/monotonic sequence;
- proxy request ID;
- attempt number;
- optional existing durable request ID for retries;
- account ID;
- account name only if required for diagnostics, not database lookup;
- provider ID;
- model ID;
- client protocol;
- upstream protocol if persistence requires it;
- streamed flag;
- estimated tokens;
- estimated microdollars;
- request start wall-clock timestamp in canonical database format;
- client IP if persisted;
- optional retry update fields;
- immutable routing decision fields required for correctness, excluding diagnostic trace payloads;
- generation ID for diagnostics only;
- enqueue timestamp.

Do not include:

- API keys;
- request bodies;
- incoming authorization headers;
- mutable registry/router/catalog objects;
- callbacks into the generation.

### `PersistedDispatchResult`

Fields:

- durable request ID;
- reservation ID;
- attempt ID;
- attempt number;
- commit timestamp/sequence;
- batch ID and batch size for diagnostics;
- queue wait and transaction timing metadata if desired.

### Errors

Define stable internal error classes:

- queue closed;
- queue saturated/enqueue timeout;
- intent cancelled before acceptance;
- transaction failed;
- ambiguous commit outcome;
- validation/invariant failure;
- writer shutdown/interruption.

Errors returned to the coordinator must be safe to map into existing proxy/database error handling without leaking SQL or secrets.

## Workstream C2 — Repository-level dispatch bundle operation

Create a single repository API for one intent:

```python
persist_dispatch_bundle(intent) -> PersistedDispatchResult
```

and a batch API:

```python
persist_dispatch_bundles(intents) -> list[PersistedDispatchResult]
```

The batch implementation executes all accepted intents inside one `db.transaction()` where safe. Within the transaction, each intent retains independent generated IDs and result mapping.

Atomicity policy must be explicit:

### Recommended initial policy: batch-fail together

If any statement fails unexpectedly, roll back the entire transaction and fail every intent in that batch. This is simple, predictable, and preserves atomic durability semantics. Because intents are generated from validated internal data, per-intent constraint failures should be rare and signal a bug or stale state.

Do not use savepoints initially unless evidence shows one malformed intent can frequently poison unrelated valid intents. Savepoints add statement and complexity overhead.

For retries, validate that the existing request row is still pending and belongs to the expected logical request before updating it. Use stable uniqueness constraints or idempotency keys where possible.

Add or verify uniqueness constraints supporting idempotent recovery:

- proxy request ID uniqueness for logical request row;
- `(request_id, attempt_number)` uniqueness for attempts;
- reservation identity tied to request/attempt as appropriate.

If schema changes are needed, add a migration with compatibility tests.

## Workstream C3 — Writer queue and ownership

The writer is process-owned because it owns access to the process-owned primary database and must survive generation swaps.

Add it to `ProcessRuntime`, not `RuntimeGeneration`.

Lifecycle:

- construct after database/repositories are ready;
- start before readiness becomes healthy;
- expose to initial and candidate coordinators;
- do not duplicate it during rehash;
- stop accepting new intents during process shutdown;
- drain or fail pending intents within a bounded shutdown deadline;
- flush/commit accepted work before primary DB disconnect when possible;
- expose closed/draining state in runtime diagnostics.

The writer should use a single asyncio task on the owning event loop. If Granian runtime threads can invoke coordinators on multiple event loops, define a thread/loop-safe submission boundary. Options:

1. a process-owned writer loop plus `call_soon_threadsafe` and `concurrent.futures.Future` bridging;
2. a thread-safe bounded queue consumed by the writer loop;
3. restrict supported runtime-thread topology until milestone F validates a cross-loop design.

Do not share a loop-bound `asyncio.Queue` blindly across runtime loops.

## Workstream C4 — Bounded enqueue and backpressure

Configuration should be conservative and bounded. Suggested fields under a new `[database.dispatch_writer]` or `[dispatch.persistence]` section:

- `enabled = true` after rollout proof;
- `queue_capacity`;
- `enqueue_timeout_ms`;
- `max_batch_size`;
- `max_batch_wait_ms`;
- `shutdown_drain_timeout_s`;
- optional `direct_fallback_on_writer_failure`, default false after stabilization.

Recommended semantics:

- If queue capacity is available, accept immediately.
- If saturated, wait up to `enqueue_timeout_ms`.
- On timeout, fail the request before upstream dispatch with a clear local-overload error, normally 503.
- Never silently drop correctness-critical intents.
- Never bypass durability and send upstream because the queue is full.
- Expose saturation counters and queue age.

Queue capacity must be based on sustainable SQLite throughput and memory constraints. Each intent is small and content-free, but the default should remain bounded for SBCs.

## Workstream C5 — Adaptive microbatch algorithm

Avoid imposing a fixed batching delay on isolated traffic.

Recommended drain algorithm:

1. Await the first intent.
2. Immediately drain any intents already queued, up to `max_batch_size`.
3. If only one intent was available and recent queue pressure is low, persist immediately.
4. If queue pressure is present or another intent arrives during a bounded micro-window, gather until:
   - `max_batch_size` reached;
   - `max_batch_wait_ms` elapsed from first acceptance;
   - shutdown/drain requested.
5. Persist the batch in one transaction.

Start with `max_batch_wait_ms` in the 0.5–2 ms range for general hosts and potentially 1–4 ms for SBC testing. The final default must be benchmark-driven.

Track:

- batch size distribution;
- percent single-intent batches;
- queue wait p50/p95/p99;
- transaction duration p50/p95/p99;
- commit duration if separable;
- intents/transaction;
- queue saturation/rejection count.

Low-volume serial benchmark acceptance: median added queue wait should be effectively zero or below the timing resolution target, with no unconditional sleep.

## Workstream C6 — Cancellation semantics

Cancellation is subtle because queue acceptance and durable commit can race with the request task.

Define states:

- not enqueued;
- enqueued/unclaimed by writer;
- claimed into batch;
- transaction executing;
- committed;
- result delivered;
- caller cancelled.

Required behavior:

- Cancellation before queue acceptance: no intent exists; coordinator rolls back selection claim.
- Cancellation after acceptance but before writer claim: writer may remove/cancel the intent if safely supported; otherwise persist then immediately compensate. Prefer cancellable pending entries if implementation remains simple.
- Cancellation after writer claim but before commit: do not cancel the shared batch transaction. Complete the transaction, then mark the caller result and run post-commit compensation for the cancelled request.
- Cancellation after commit: coordinator or writer-owned cleanup must finalize/release the durable state; do not leave it pending.
- One caller's cancellation must not cancel other intents in the same batch.

Use shielding only around the minimum shared commit/result-delivery operations. Do not shield the entire request path.

Add an idempotent writer-to-coordinator handoff so a committed result cannot be lost if the awaiting task is cancelled at the exact delivery boundary. A completion callback or process-owned committed-result reconciliation table/map may be required, but keep it bounded and remove entries after acknowledgement/compensation.

## Workstream C7 — Failure and ambiguous commit handling

A transaction exception before commit can fail the whole batch and return errors. A connection error during commit may leave outcome ambiguous.

Implement reconciliation keyed by durable idempotency fields:

- query request by proxy request ID;
- query attempt by request/attempt number;
- verify reservation linkage;
- if all expected rows exist consistently, treat the intent as committed;
- if no rows exist, fail and rollback claim;
- if partial/inconsistent rows exist, fail closed, emit an operational event, and run a bounded repair/compensation path.

Do not resend the same intent blindly after an ambiguous commit without checking uniqueness and durable state.

Writer task failure must be supervised. If it crashes:

- runtime readiness should become degraded/unready for new dispatches;
- queued intents must be failed or recovered deterministically;
- supervisor restart must not duplicate committed bundles;
- diagnostics must show last error class and restart count;
- repeated failures must stop accepting traffic rather than loop indefinitely.

## Workstream C8 — Integration with rehash

The process-owned writer survives generation swaps. Candidate coordinators receive a reference to the same writer.

Requirements:

- no duplicate writer task after rehash;
- active and retiring generations may submit intents concurrently while their leases remain valid;
- intents include generation ID only for diagnostics;
- writer never dereferences retired generation state;
- task-spec reload does not accidentally stop/restart the writer unless a restart-required config field explicitly changes process-owned writer settings;
- define reload policy for writer config fields. Recommended initial policy: queue capacity and batch policy are restart-required unless a safe process-owned reconfiguration method is implemented and tested.

## Workstream C9 — Runtime diagnostics and readiness

Expose:

- enabled/state: starting, running, draining, failed, closed;
- queue depth/capacity;
- oldest queued age;
- accepted/completed/failed/rejected totals;
- batch count and batch-size percentiles/distribution;
- queue wait p50/p95/p99/max;
- transaction and commit p50/p95/p99/max;
- last batch timestamp/size/duration;
- last error class/time;
- restart count;
- cancellation counts by state;
- ambiguous commit reconciliation counts;
- direct-path fallback count while rollout seam exists.

Readiness should fail or degrade when the writer is required and not accepting intents.

## Test plan

### Unit tests

- isolated intent persisted immediately;
- concurrent intents form bounded batch;
- max batch size respected;
- max batch wait respected;
- queue capacity and enqueue timeout;
- FIFO result mapping where required;
- batch rollback fails every member;
- one caller cancellation does not cancel batch peers;
- cancellation before writer claim;
- cancellation after writer claim;
- cancellation after commit before result delivery;
- shutdown drain success and timeout;
- writer restart/idempotent reconciliation;
- runtime diagnostics counters.

### Database integration tests

- first-attempt bundle rows and foreign keys;
- retry bundle update and attempt numbering;
- uniqueness/idempotency behavior;
- forced statement failure rolls back whole batch;
- forced commit ambiguity reconciles correctly;
- no upstream send before commit acknowledgement;
- batch with multiple accounts/providers/models;
- WAL and transaction count reduction versus direct path.

### Rehash tests

- writer identity unchanged across 10 rehashes;
- no duplicate writer tasks;
- old and new generation requests persist correctly during drain;
- shutdown after rehash drains once;
- process-owned writer config changes follow declared reload policy.

### Performance tests

Compare to milestone B:

- serial low-volume latency;
- 5, 10, 25, and 50 concurrent dispatches;
- native and transcoded requests;
- streaming start bursts;
- primary DB contention;
- SBC/slow-storage simulation where feasible.

Measure transactions per request and commits per request. The expected reduction appears primarily during concurrency; serial traffic should remain near one transaction per request without an artificial wait.

## Acceptance criteria

1. No upstream request is sent before its own dispatch bundle commit is acknowledged.
2. The writer is process-owned and is not duplicated by live rehash.
3. Every accepted intent receives exactly one success or failure outcome.
4. Queue saturation fails closed before upstream dispatch and is visible in diagnostics.
5. Isolated requests do not incur an unconditional batching sleep.
6. Concurrent requests are microbatched up to configured size/wait bounds.
7. A batch transaction rollback leaves no partial request/reservation/attempt rows.
8. Caller cancellation cannot cancel unrelated batch members.
9. Cancellation/ambiguous commit paths do not leak pending requests, active reservations, health slots, active counts, or quota reservations.
10. Writer failure affects readiness and does not silently fall back to unsafe dispatch.
11. Under the standard concurrent benchmark, SQLite transactions/commits per dispatch are materially reduced and dispatch p95/p99 improve or remain stable.
12. Serial p50 regression remains within the agreed tolerance, recommended no more than 5% or 1 ms, whichever is larger on the CI host.
13. Runtime diagnostics expose queue, batch, timing, error, and cancellation state.
14. Full tests, ruff, format check, and pyright pass.

## Rollout strategy

### Phase 1: dark integration

Build the writer and persistence bundle API but retain direct coordinator persistence in production. Exercise the writer in tests and optional shadow validation without writing duplicate rows.

### Phase 2: opt-in writer

Add an internal/config opt-in. Run integration and soak workloads with direct fallback disabled during tests.

### Phase 3: default writer with emergency fallback

Enable by default after failure injection passes. Keep an explicit restart-required emergency fallback to direct persistence for one release. Count its use.

### Phase 4: remove divergent path

Once soak/field evidence is satisfactory, remove the long-term dual implementation or reduce the direct path to a test-only primitive sharing the same repository bundle method.

Rollback criteria:

- upstream dispatch before commit;
- duplicate request/attempt/reservation rows;
- ambiguous commit misclassification;
- queue not draining under sustainable load;
- rehash duplicates writer;
- cancellation causes durable leaks;
- serial latency exceeds tolerance without compensating concurrency benefit.

## Handoff evidence

The implementing agent should provide:

- intent/result schema;
- writer lifecycle/ownership diagram;
- cancellation state table;
- transaction count before/after;
- serial and concurrent benchmark results;
- ambiguous commit failure-injection evidence;
- rehash identity proof;
- queue saturation/readiness behavior;
- remaining integration constraints for milestones D and F.

## Exit condition

Milestone C is complete when dispatch persistence is handled by one bounded process-owned writer, concurrent intents can share transactions, every request waits for its own durable acknowledgement before upstream I/O, and cancellation, failure, shutdown, and rehash paths remain exact and leak-free.