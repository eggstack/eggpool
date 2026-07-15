# Dispatch Stability Milestone F — Runtime Concurrency and Hot-Path Hardening

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: F of G
Depends on: Milestones A–E sufficiently integrated to remove known artificial contention

## Objective

Validate and harden EggPool's long-lived runtime behavior across Granian runtime-thread configurations, correct synchronization that is unsafe or misleading across event loops/threads, and remove remaining avoidable request-path parsing, allocation, copying, and instrumentation overhead without reducing capability.

This milestone must not use higher runtime thread counts to mask the SQLite and lock problems addressed by earlier milestones. It determines the supported concurrency profile only after known contention has been corrected.

## Problem statement

EggPool uses one Granian worker with configurable runtime threads. The process-owned and generation-owned object graphs contain multiple asyncio primitives and mutable structures:

- coordinator selection/claim locks;
- router active-count and recovery locks;
- quota estimator locks/state;
- runtime manager locks and generation slots;
- metrics and observability buffers;
- finalization and dispatch queues;
- DNS cache locks/singleflight futures;
- database connection lock;
- task supervisors.

`Database` contains explicit handling for an idle `asyncio.Lock` that was bound to another loop, indicating that cross-loop reuse has been encountered. Other shared objects may not have equivalent protection. If Granian invokes the same ASGI application graph from multiple event loops, blindly sharing loop-bound primitives can create errors, hidden serialization, or undefined behavior.

The request path also includes smaller fixed costs that matter after lock contention is reduced: repeated JSON parsing, synthetic padding allocations for token checks, repeated provider/header/config set construction, broad header copying, trace lock acquisition, and repeated capability lookups.

## Scope

### In scope

- Establish actual Granian runtime-thread/app sharing behavior with tests and documentation.
- Audit every shared asyncio primitive for loop/thread ownership.
- Correct queue/buffer synchronization, especially process-owned writers and metrics coalescing.
- Define supported `server.threads` profiles.
- Add event-loop lag and per-loop diagnostics.
- Reduce repeated request parsing and avoidable allocations.
- Precompute immutable provider/header/config lookup state.
- Optimize detailed telemetry only where measurement shows material overhead.
- Verify HTTP client/DNS connection lifecycle plateaus.
- Preserve native, transcoded, compression, cache synthesis, thinking, and streaming capability.

### Out of scope

- Multiple Granian workers.
- Replacing Python services with Rust extensions.
- Changing public API request/response semantics.
- Removing diagnostics rather than sampling/bounding them.
- Major transcoder feature redesign.

## Target files and modules

Runtime/concurrency:

- `src/eggpool/app.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/background/__init__.py`
- `src/eggpool/request/coordinator.py`
- milestone C/D writer modules
- `src/eggpool/routing/router.py`
- `src/eggpool/quota/estimation.py`
- `src/eggpool/metrics/buffer.py`
- `src/eggpool/providers/dns_cache.py`
- `src/eggpool/providers/client_pool.py`
- `src/eggpool/providers/outbound.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/runtime_metrics.py`

Hot path:

- `src/eggpool/api/proxy_request.py`
- `src/eggpool/request/coordinator.py`
- transcoder preflight/selection modules
- compression segmentation/analyzer/applier modules
- cache synthesis modules
- `src/eggpool/proxy/client.py`
- `src/eggpool/jsonx.py`
- configuration/runtime generation builders

Tests:

- runtime-thread integration tests;
- thread/loop safety unit tests;
- performance harness;
- transcode/compression/cache end-to-end suites.

## Workstream F1 — Determine Granian's actual sharing topology

Create an integration probe that records, per request:

- process ID;
- OS thread ID;
- asyncio loop identity;
- app object identity;
- runtime manager/process runtime identity;
- active generation identity;
- coordinator/router/database/writer identity.

Run with `server.threads = 1`, `2`, and `4` while issuing concurrent HTTP and streaming requests. Determine whether:

- one app object is shared across runtime threads/loops;
- each thread receives a separate app/lifespan instance;
- process-owned background tasks are started once or per runtime;
- request handlers cross loops while using the same service objects.

Do not rely solely on Granian documentation; retain an executable regression test or startup assertion aligned with the deployed version.

The probe must be test/debug only and must not expose object IDs through unauthenticated production endpoints.

## Workstream F2 — Audit shared async primitives

Build a table for every long-lived `asyncio.Lock`, `Queue`, `Event`, `Future`, semaphore, condition, and task:

- creating loop/thread;
- consuming loops/threads;
- process or generation ownership;
- cross-loop safety strategy;
- shutdown/reload behavior;
- replacement or sharding plan.

Classify each as:

1. loop-local and accessed only from its loop;
2. process-shared through a thread-safe submission bridge;
3. immutable/read-only and safe;
4. unsafe under current multi-loop sharing;
5. unknown pending test.

No “unknown” classifications may remain at milestone exit for data-plane objects.

## Workstream F3 — Define supported runtime model

Choose one of two explicit models based on F1 evidence.

### Model 1 — Single runtime loop is canonical

If shared object graph cannot safely span loops without disproportionate complexity:

- set/recommend `server.threads = 1` for the supported data-plane profile;
- reject or warn strongly for values above one;
- keep one worker;
- rely on async I/O and dedicated aiosqlite worker threads;
- document that additional Granian threads do not bypass SQLite serialization.

### Model 2 — Multi-runtime threads supported through bridges/shards

If Granian shares the app across loops and multi-thread operation provides measurable benefit:

- process-owned writers run on one designated owner loop/thread;
- request loops submit through thread-safe bridges;
- loop-bound state is sharded per loop or replaced with thread-safe primitives;
- generation lease accounting becomes thread-safe;
- snapshots merge shards;
- shutdown coordinates all loops deterministically.

Do not leave `server.threads=4` as an implied supported default unless this proof is complete. Update config default/guidance based on measured safety and performance, not prior assumptions.

## Workstream F4 — Harden process-owned writer submission

Milestones C and D introduce process-owned queues. Ensure submission is safe from every request loop.

Requirements:

- bounded capacity enforced globally;
- no loop-bound future awaited from another loop;
- per-request result can be delivered back safely;
- cancellation can be signaled without cross-loop task cancellation;
- shutdown rejects new work and drains accepted work;
- metrics snapshots are thread-safe and low overhead.

A robust pattern may use:

- a thread-safe queue;
- owner-loop wakeup via `call_soon_threadsafe`;
- `concurrent.futures.Future` per correctness intent;
- `asyncio.wrap_future()` on the caller loop;
- immutable events for lossy observability.

Benchmark bridge overhead. If multi-loop support is not worth it, choose Model 1 explicitly.

## Workstream F5 — Correct metrics coalescer synchronization

`MetricsWriteCoalescer.record_usage()` is synchronous and mutates `_buffer`/counters without acquiring the async lock that `flush()` uses to swap the buffer. Under true multi-thread access, the current “thread-safety” claim is not established.

Refactor according to supported runtime model:

- single-loop model: enforce loop affinity and correct documentation; or
- multi-loop model: use a short `threading.Lock`, per-loop shards, or thread-safe event submission.

Preferred multi-loop design: per-loop/thread aggregation shards with process-owned flush snapshot/merge. This reduces contention and avoids taking a global lock on every finalization. Simpler `threading.Lock` protection is acceptable if benchmarks show negligible cost.

Ensure cancellation restoration after failed flush cannot race new event recording or double-merge deltas.

Add invariant tests:

- total received = total flushed + pending + dropped, subject to documented failed-detached batch state;
- no negative counters;
- no lost updates during concurrent record/flush;
- no duplicate deltas after cancellation restore.

## Workstream F6 — Event-loop lag and per-loop diagnostics

Add a low-overhead process/runtime lag monitor. Record expected wake time versus actual wake time at a modest cadence.

Expose:

- event-loop count/identifiers in safe anonymized form;
- lag p50/p95/p99/max per loop or aggregate;
- last sample age;
- monitor task health;
- active request/stream count per loop if feasible;
- writer submission/ack latency by caller loop.

Avoid a high-frequency monitor on SBCs. A 0.5–1 second cadence is usually sufficient for operator diagnosis; make it configurable or adaptive.

Correlate lag with dispatch spans in soak analysis but do not perform expensive correlation in the request path.

## Workstream F7 — Parse request JSON once and share derived state

Audit all request phases that parse `original_body` or `upstream_body`:

- provider/model extraction;
- streaming flag;
- thinking requirement;
- context token estimation;
- transcode preflight;
- compression segmentation/analyzer;
- synthetic cache controls;
- selected-provider thinking adjustment;
- usage-related request options.

Create a request-local parsed payload/derived-state container where safe. Requirements:

- preserve original bytes unchanged for forwarding when no mutation is required;
- avoid retaining multiple large deep copies;
- distinguish original parsed object from mutable provider-bound transformed payload;
- do not allow compression/transcoding mutations to corrupt data needed for auditing or retries;
- invalidate/recompute serialized bytes only when transformation occurs;
- retain strict input validation behavior.

Prepared transcode reuse already exists; extend the same principle to other derived request values without creating a broad mutable cache.

Measure parse count per request in tests. Target one original JSON parse for normal valid JSON paths plus only necessary parses of transformed payloads.

## Workstream F8 — Remove synthetic padding allocation

The context-limit/transcode estimation path may construct a synthetic `b"\x00" * padding` object to represent estimated transformed payload expansion. Replace this with an API that accepts:

- base byte length;
- additional estimated bytes/tokens;
- or precomputed token estimate.

Do not allocate memory proportional to the estimated padding.

Add tests for very large tool schemas/context estimates to ensure bounded memory and equivalent limit decisions.

## Workstream F9 — Precompute immutable lookup state

During runtime generation construction, precompute and store immutable/frozen values used on every request:

- provider ID set/frozenset;
- provider protocol/path/auth/static header templates;
- account name -> account ID mapping;
- account -> provider mapping where registry/catalog precedence is stable;
- normalized/redacted header-name sets;
- resolved endpoint URL templates;
- feature flags for transcode/compression/cache fast exits;
- model/provider capability lookup indexes where catalog cache supports it.

Do not precompute values that must reflect mutable health, quota, catalog freshness, or routing state.

Invalidate naturally through generation swap. Avoid complex mutable global caches.

## Workstream F10 — Narrow request header copying

Audit `sanitize_request_headers(dict(context.incoming_headers))` and related paths.

Goals:

- avoid copying headers multiple times;
- filter hop-by-hop, authorization, host, and proxy-managed headers in one pass;
- construct only the upstream header map required by HTTPX;
- merge provider auth/static headers without logging values;
- preserve case-insensitive semantics;
- preserve compatibility with OpenAI/Anthropic/client-specific headers that EggPool intentionally forwards.

Add exhaustive header pass/drop tests. Performance gain is secondary to preserving exact security behavior.

## Workstream F11 — Telemetry contention reduction

Use milestone A/D measurements to identify whether span recorder locking is material.

Potential implementation sequence:

1. deterministic detailed-span sampling;
2. integer span IDs/constants;
3. per-thread/per-loop bounded deques;
4. snapshot merge outside request path.

Do not specify a native extension or lock-free structure without evidence. Bounded `threading.Lock` sections may remain adequate after sampling.

Ensure every shard remains bounded. Rehash/loop retirement must remove old shards or mark them inactive so telemetry state does not grow with uptime.

## Workstream F12 — HTTP client, DNS, and stream lifecycle plateau checks

Audit long-running state for boundedness:

- provider client pool clients per provider/account;
- HTTPX/httpcore connections and keepalive expiry;
- DNS positive/negative cache capacity;
- DNS singleflight map cleanup on success/error/cancellation;
- stream response close paths;
- cancelled generator finalization;
- retiring generation client closure;
- outbound manager closure;
- file descriptors and sockets.

Add runtime metrics where feasible without reaching into unstable private library internals. Prefer supported client/pool snapshots and external FD counts in soak tests.

No dynamic client should be created per request. Any account-proxy client creation remains generation construction-time and bounded by configured accounts.

## Test plan

### Runtime topology tests

- threads 1/2/4 identity probe;
- task supervisor count;
- writer identity/count;
- active generation identity;
- loop-bound primitive access;
- shutdown and rehash under each supported setting.

### Synchronization tests

- concurrent multi-thread metrics record/flush;
- process writer submissions from multiple loops;
- cancellation/result delivery across loops;
- runtime manager acquire/release under thread pressure;
- DNS singleflight cancellation;
- telemetry shard creation/retirement bounds.

### Hot-path equivalence tests

- OpenAI native streaming/non-streaming;
- Anthropic native streaming/non-streaming;
- OpenAI <-> Anthropic transcode;
- thinking budget resolution;
- compression observe/apply/fallback;
- cache synthesis dry-run/apply;
- large tool schemas;
- invalid JSON and malformed fields;
- header forwarding/security cases;
- retry uses correct transformed/original payload.

### Performance tests

Measure threads 1/2/4, where safe, across:

- serial requests;
- 5–50 concurrent streams;
- native and transcoded requests;
- compression enabled/disabled;
- dashboard polling;
- dispatch writer enabled;
- detailed spans 0/10%/100%;
- large request bodies/tool schemas.

Record CPU, RSS, context switches, event-loop lag, dispatch p95/p99, throughput, and DB queue metrics.

## Acceptance criteria

1. The repository has an executable proof of Granian app/loop/thread topology for the supported version.
2. Every long-lived async primitive used by the data plane has a documented ownership and cross-loop strategy.
3. `server.threads` supported values/default are explicit and evidence-based.
4. Process-owned dispatch/observability writers accept work safely from every supported request loop.
5. Metrics coalescer updates cannot race flush or lose/double-count deltas under supported concurrency.
6. Event-loop lag is visible with bounded overhead.
7. Normal request paths avoid repeated original-body parsing where derived state can be shared safely.
8. Context-limit estimation no longer allocates synthetic padding proportional to estimated expansion.
9. Immutable provider/account/header lookup state is precomputed per generation where safe.
10. Header forwarding/redaction behavior remains exact and security tests pass.
11. Telemetry remains bounded across loop/generation retirement.
12. Provider/DNS/client/stream resources plateau in soak tests.
13. No native/transcode/thinking/compression/cache/streaming capability regression.
14. Chosen runtime-thread profile improves or preserves latency/throughput without introducing loop errors, duplicate tasks, or resource growth.
15. Full tests, ruff, format check, and pyright pass.

## Rollout and rollback

Treat runtime-thread support as a compatibility surface. If cross-loop hardening is incomplete, prefer `threads=1` with a clear warning rather than partial unsafe support.

Land hot-path changes in small independently benchmarked patches. Avoid combining header security changes, parsed-payload sharing, and concurrency primitives in one large unreviewable commit.

Rollback criteria:

- loop-bound primitive errors;
- duplicate background tasks/writers;
- request body mutation leakage across retries;
- changed header security behavior;
- lost metrics or reservation state;
- telemetry shard/resource growth;
- higher threads worsen p95/p99 materially without throughput benefit.

## Handoff evidence

Provide:

- Granian topology report;
- async primitive ownership matrix;
- supported thread-profile decision;
- multi-loop writer/metrics tests;
- parse/allocation before/after measurements;
- header equivalence/security results;
- client/DNS/FD plateau evidence;
- performance matrix used to select defaults;
- residual constraints for milestone G.

## Exit condition

Milestone F is complete when EggPool's supported runtime concurrency model is explicit and tested, shared synchronization is safe, hot-path preprocessing avoids known redundant work, and long-lived clients, caches, telemetry shards, queues, loops, and generation resources remain bounded.