# Dispatch Stability Milestone D — Off-Path Observability and Trace Pressure Control

Date: 2026-07-15
Status: complete
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: D of G
Depends on: Milestone B; integrate against milestone C ownership model when available

## Objective

Remove routing decision trace persistence and other diagnostic-only writes from synchronous dispatch, add bounded asynchronous observability buffering with explicit loss accounting, and harden runtime timing recorders so instrumentation remains low-overhead and safe during long-running, multi-threaded operation.

Diagnostic fidelity must never delay or fail correctness-critical request dispatch.

## Problem statement

The coordinator currently builds a routing trace after durable request selection and, when sampling/guard conditions permit, enters a separate SQLite transaction to persist it before returning the selected attempt and proceeding to upstream send. The trace is explicitly best-effort and not required for billing, retry, quota, health, finalization, or crash recovery, yet a sampled request can still wait behind unrelated SQLite writers.

The current routing-trace guard skips writes when rolling SQLite lock-wait p95 exceeds a threshold. This is useful overload protection, but it reacts after contention is already present and still leaves accepted trace writes on the request path.

The detailed dispatch span recorder also uses a shared `threading.Lock` and bounded deques. The storage is bounded, but every recorded span takes the same lock, and snapshot logic must safely copy samples before sorting/percentile calculation.

## Architectural decision

Create a process-owned `ObservabilityWriteBuffer` or focused `RoutingTraceWriter` that accepts immutable, content-free trace events synchronously or through a thread/loop-safe nonblocking submission method. A supervised background writer drains events in bounded batches and writes them to SQLite outside the request path.

Correctness-critical request handling must only perform:

```text
build trace event if sampled
  -> attempt bounded enqueue
  -> record accepted or dropped counter
  -> continue dispatch immediately
```

No request waits for trace commit.

The writer may lose trace events on overload, abrupt power loss, or bounded shutdown timeout. That loss is acceptable because traces are diagnostic. Loss must be explicit, measured, and surfaced to operators.

## Scope

### In scope

- Asynchronous routing-trace buffering and writing.
- Immutable trace event contract.
- Bounded queue, drop policy, batching, flush, and shutdown behavior.
- Sampling and score-component policies.
- Routing trace retention compatibility.
- Detailed span recorder sampling/sharding/hardening.
- Safe snapshot copying.
- Dashboard semantics for sampled/dropped traces.
- Runtime diagnostics and operator documentation.

### Out of scope

- Correctness-critical dispatch writer; milestone C.
- Finalization batching.
- Retention batching implementation; milestone E, although D must expose retention-friendly metadata.
- Request content logging.
- Distributed tracing backend integration.

## Target files and modules

Primary:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/routing_trace_guard.py`
- `src/eggpool/db/routing_decision_repository.py` or current repository location
- `src/eggpool/runtime_dispatch.py`
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/app.py`
- `src/eggpool/runtime_tasks.py`
- `src/eggpool/models/config.py`
- dashboard stats/routes/templates for routing diagnostics

Potential new modules:

- `src/eggpool/observability/routing_trace_writer.py`
- `src/eggpool/observability/events.py`

Tests:

- routing trace unit/integration tests;
- runtime recorder tests;
- dashboard sparse/sample tests;
- rehash ownership tests;
- milestone A performance harness.

## Workstream D1 — Separate correctness data from diagnostic trace data

Audit the current `RoutingDecisionTrace` payload and repository schema. Classify each field:

- required for request correctness;
- required for postmortem diagnostics;
- derivable from request/attempt/account rows;
- high-cardinality/large JSON;
- safe to omit under overload.

No routing trace field should remain in the synchronous dispatch persistence bundle unless it is genuinely correctness-critical. If any current code depends on trace rows for core behavior, remove that dependency or explicitly move the field into the correctness-critical schema.

Retain request/attempt foreign-key linkage for diagnostic joins, but asynchronous insertion must tolerate the request or attempt being retained/deleted before the trace is written. Decide between:

- foreign-key cascade and dropping a late trace if parent is gone;
- writer ordering/age bounds that make late parent deletion impossible;
- relaxed diagnostic linkage if schema permits.

Preferred: keep foreign keys and bound trace age so retention cannot normally race; treat missing parent as a dropped stale trace, not a request failure.

## Workstream D2 — Define immutable trace event

Suggested fields:

- request ID and durable request ID;
- attempt number and attempt ID if schema supports it;
- event creation monotonic/wall timestamp;
- generation ID;
- model/provider/protocol;
- selected account ID/name;
- selected tier/score;
- eligible/scored/excluded counts;
- top score/account;
- compact exclusion reason counts or bounded exclusions;
- optional score components according to config;
- sampling decision metadata;
- payload version.

Do not include:

- API keys;
- request or response body;
- prompts/messages/tool schemas;
- authorization headers;
- raw unredacted upstream errors;
- mutable runtime service references.

Pre-serialize only if it is measurably cheaper and safe. Avoid large per-request JSON construction when `include_score_components=false`.

## Workstream D3 — Process-owned trace writer

The trace writer should be process-owned, share the primary database, and survive generation swaps. It may be implemented as a specialized queue or integrated into a generic lossy observability writer if the abstraction remains simple.

Lifecycle:

- construct after database/repository initialization;
- start before requests are accepted if trace mode is enabled;
- inject into all coordinators;
- do not recreate on rehash;
- accept per-event policy fields from the active generation or support safe reconfiguration;
- stop accepting on process shutdown;
- flush within a bounded shutdown timeout;
- drop remaining events explicitly if the timeout expires;
- expose task health/readiness impact separately from data-plane readiness.

A trace writer failure must not make the proxy unavailable. It should mark observability degraded, restart under supervision with bounded backoff, and drop/reject events while unavailable.

## Workstream D4 — Queue, batching, and drop policy

Recommended configuration under `[routing.trace]` or a nested writer section:

- existing `mode = all|sampled|off`;
- existing `sample_rate`;
- existing `include_score_components`;
- `queue_capacity`;
- `flush_interval_ms` or seconds;
- `max_batch_size`;
- `shutdown_flush_timeout_s`;
- optional `drop_policy`, initially only `drop_newest` for predictable bounded behavior.

Recommended queue semantics:

- enqueue must be nonblocking on the request path;
- when full, drop the new event and increment counters;
- do not evict an older event initially because FIFO age/order aids diagnosis and eviction bookkeeping adds synchronization;
- writer drains immediately when events exist and may briefly coalesce up to `max_batch_size`;
- each database flush uses one transaction and `executemany`/batch repository support where practical;
- failed flushes may retry the detached batch a bounded number of times or requeue it if capacity permits;
- avoid unbounded retry loops that preserve stale trace memory forever.

Track drop reasons:

- mode off;
- deterministic sampling exclusion;
- queue full;
- writer unavailable;
- event too old;
- parent row missing;
- serialization failure;
- shutdown timeout;
- database flush failure after retry budget.

Sampling exclusions are not errors and should be counted separately from overload drops.

## Workstream D5 — Remove trace transaction from coordinator

Refactor `_select_and_persist_attempt()` so after the correctness-critical durable result is available it:

1. evaluates trace mode/sample decision;
2. builds the minimum configured event;
3. submits it to the process-owned writer;
4. records submission result;
5. immediately returns `SelectedAttempt`.

Delete the coordinator-owned `async with self._db.transaction()` trace write path.

Retain the existing lock-wait guard only if useful as a pre-enqueue adaptive sampling/drop signal. Its meaning must change from “skip a synchronous write” to “avoid adding trace pressure while the DB is contended.” Consider hysteresis so the guard does not oscillate on every snapshot.

The trace guard should inspect the writer queue as well as DB lock wait:

- high queue occupancy;
- oldest event age;
- recent flush failures;
- DB lock-wait p95.

Do not make trace acceptance await any of these values.

## Workstream D6 — Batch repository writes

Add `create_many(events)` or equivalent repository support. Use one transaction per batch.

Requirements:

- validate batch size;
- use compact JSON serialization once per event;
- preserve event-to-row diagnostic IDs if needed;
- tolerate individual stale-parent failures according to declared policy;
- avoid holding the DB transaction while performing expensive Python serialization;
- prebuild row tuples outside the transaction;
- record execution and commit duration.

If one malformed event can roll back the whole batch, validate events before transaction. For database constraint failures, decide whether to fail the batch or retry individual rows. Since traces are lossy, simplest acceptable behavior is to drop the failed batch with a clear counter, unless stale-parent failures are common enough to justify filtering.

## Workstream D7 — Harden dispatch recorders

### Snapshot safety

When creating a snapshot from bounded deques, copy all sample lists while holding the recorder lock. Release the lock before sorting and percentile calculation.

Do not retain references to mutable deques after the lock is released.

### Hot-path lock pressure

Measure recorder overhead before changing architecture. Possible optimizations:

- sample detailed spans while keeping coarse dispatch always-on;
- use per-thread/per-loop shards and merge at snapshot time;
- use lock-free append only if Python/runtime guarantees and cross-thread topology are proven;
- aggregate simple counters without labels;
- avoid span-name lookup allocations by using constants/enum IDs.

Preferred initial approach:

- preserve coarse dispatch sampling at 100%;
- add configurable detailed span sampling, deterministic by request ID;
- avoid recording disabled spans entirely;
- copy under lock safely;
- only shard if benchmark evidence shows the recorder lock is material.

Add config such as:

```toml
[metrics.dispatch]
detailed_span_sample_rate = 0.1
```

A value of 1.0 retains full detail for debugging; 0 disables detailed spans but not coarse dispatch.

## Workstream D8 — Dashboard and statistical semantics

Dashboard routing pages must state:

- trace mode;
- configured sample rate;
- writer accepted/written/dropped counts;
- whether displayed routing distributions are sampled;
- observed trace coverage over the selected time range where computable;
- score components availability;
- last trace writer error.

Do not present sampled trace counts as exact request counts. Where exact totals are available from requests/attempts, use them as denominators and label trace-derived breakdowns as sampled.

If trace events are dropped under overload, show a warning without blocking page rendering.

## Workstream D9 — Rehash policy

Because the writer is process-owned, decide which trace fields can change live:

- mode/sample rate/include score components can be attached to each event based on the active generation and therefore change live safely;
- queue capacity may be restart-required unless the writer supports atomic queue replacement;
- flush interval/max batch size may be safely reconfigurable only through a tested process-owned transition method;
- ensure candidate generation creation does not register a duplicate writer task.

Add rehash tests for mode transitions:

- all -> sampled;
- sampled -> off;
- off -> sampled;
- include score components toggle;
- queue/batch setting rejected or transitioned according to policy.

## Test plan

### Unit tests

- deterministic sampling stability by request ID;
- mode off creates no event;
- sampled exclusion counted separately;
- enqueue success is nonblocking;
- queue full drops newest and increments reason;
- writer unavailable drop;
- batch size/flush behavior;
- shutdown flush and timeout drop;
- failed batch accounting;
- snapshot copies deques safely under concurrent writes;
- detailed span sample rate 0, fractional, and 1;
- no secrets/content in serialized trace event.

### Integration tests

- coordinator sends upstream without awaiting trace commit;
- block primary DB after correctness commit and prove upstream send is not delayed by trace writer;
- routing dashboard handles sparse/sampled/dropped data;
- writer survives 10 rehashes without duplication;
- trace modes transition correctly;
- retention parent deletion/stale event behavior;
- writer crash/restart does not fail proxy requests.

### Performance tests

Compare milestone B/C baseline with:

- trace mode all;
- sampled 5%;
- off;
- score components on/off;
- queue near capacity;
- slow DB flush.

Dispatch p95/p99 should no longer correlate directly with trace flush duration.

## Acceptance criteria

1. No routing trace database transaction occurs on the synchronous request path before upstream send.
2. Trace enqueue is bounded and nonblocking.
3. Queue overload or writer failure never fails or delays correctness-critical dispatch.
4. All trace loss is classified and visible.
5. Process-owned writer survives rehash without duplication.
6. Trace modes and score-component policy retain existing user-facing capability.
7. Dashboard labels sampled/dropped trace data accurately and does not treat it as exact traffic volume.
8. Detailed span recorder snapshots are safe under concurrent writes.
9. Coarse dispatch measurement remains always available and bounded.
10. Detailed span sampling reduces instrumentation pressure without changing request behavior.
11. Under slow trace flush, dispatch p95/p99 remain near trace-off behavior within benchmark tolerance.
12. Full tests, ruff, format check, and pyright pass.

## Rollout and rollback

Land the writer and queue behind the existing sampled default. Keep synchronous trace persistence only during a short comparison phase, never both writing the same event in production. Remove the old synchronous path after integration evidence.

Rollback criteria:

- trace enqueue blocks request tasks;
- writer is duplicated by rehash;
- dashboard silently presents sampled data as exact;
- queue or detached batch grows without bound;
- trace events include secrets or request content;
- recorder changes corrupt percentile snapshots.

Rollback may disable trace writing entirely while preserving correctness; it must not restore a known synchronous database wait as the long-term default.

## Handoff evidence

Provide:

- trace event schema and redaction review;
- writer lifecycle/ownership proof;
- before/after coordinator span showing removal of trace DB wait;
- queue overload behavior;
- dashboard sample/drop screenshots or test payloads;
- recorder concurrency tests;
- rehash transition results;
- performance comparison for all/sampled/off.

## Exit condition

Milestone D is complete when diagnostic trace persistence is fully detached from synchronous dispatch, loss is bounded and transparent, recorder synchronization is safe, and observability pressure can no longer create request-path SQLite stalls.