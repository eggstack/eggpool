# Plan 228 — Performance Qualification, Contention Characterization, and Conditional Runtime Closure

Date: 2026-09-20  
Status: implementation handoff  
Planning baseline: 3e90d36c4094af1c93756ace1c882f55b5f8f5d5  
Parent roadmap: plans/225-native-runtime-performance-optimization-roadmap.md  
Prerequisites: Plans 226 and 227 complete; include Plan 191 candidate if available  
Priority: P1/P2 evidence and only measurement-supported structural optimization  
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close the performance campaign with comparable measurements and determine
whether any remaining runtime contention justifies architectural complexity.

This plan intentionally treats dashboard SQLite reads, the streaming mpsc
bridge, routing selection serialization, and Tokio runtime threading as
questions to measure rather than presumed defects.

The default outcome is allowed to be "no further structural change required."

## Required end state

At closure:

1. the request-path changes from Plans 226/227 have comparable before/after
   evidence;
2. dashboard/SQLite gate behavior has been characterized with a realistic
   file-backed database;
3. dashboard query plans have been inspected and missing indexes fixed only if
   evidence supports them;
4. the streaming bridge has been characterized for CPU, buffering, and
   downstream backpressure;
5. the single-thread Tokio runtime has been characterized under concurrent
   large-request/stream workloads;
6. routing selection-lock contention has been measured;
7. only material, reproducible bottlenecks receive code changes;
8. no new generic database pool, second writer, broad async queue, or runtime
   complexity is introduced without evidence;
9. the roadmap receives closure evidence.

## Workstream 0 — Freeze comparable candidates

Record three commits where available:

- audit baseline: 3e90d36c4094af1c93756ace1c882f55b5f8f5d5;
- after Plan 226;
- after Plan 227;
- after Plan 191 as a separate dependency/footprint dimension if it has landed.

Use the same:

- Rust toolchain;
- target triple;
- release profile;
- strip/LTO settings;
- host;
- configuration;
- fixture provider.

Do not compare debug and release results.

Do not attribute Plan 191 binary/dependency movement to request-path CPU work.

## Workstream 1 — Lightweight request-path benchmark matrix

Use local loopback fixtures so WAN variance does not dominate.

### Surfaces

At minimum:

- Chat Completions finite native path;
- Responses finite native path;
- Responses streaming native path;
- Messages or another cross-surface/transcoded path;
- provider-qualified model request;
- virtual model request.

### Body sizes

Use representative bounded payloads:

- approximately 4-8 KiB;
- approximately 64-128 KiB;
- approximately 512 KiB or another realistic large-agent payload safely below
  max_request_body_bytes.

Include a tool-heavy Responses/Codex-shaped payload because repeated parsing is
most visible with deep tool schemas.

Do not record prompt/tool contents in the plan. Record only size/surface/facts.

### Concurrency

At minimum:

- 1;
- 4;
- 16 concurrent requests.

For SBC evidence, add 32 only if the device remains stable and the test is
useful.

### Metrics

Record where reliable:

- p50 latency;
- p95 latency;
- requests/second;
- first-byte latency for streams;
- total stream duration for a fixed fixture;
- process CPU;
- peak/steady RSS;
- final release artifact bytes;
- resolved package count when Plan 191 is included.

Also record structural facts:

- full JSON parse count on each path;
- full request-body copy count on native direct path;
- presence/absence of account lookup key allocation/topology mutex.

A permanent benchmark dependency is not required.

A small repository script is acceptable only if it is dependency-free,
deterministic, and clearly useful for future regression checks. Otherwise keep
the harness local and append command/workload details to the closure record.

## Workstream 2 — Characterize dashboard/SQLite gate contention

### Current architecture

rust/src/db/connection.rs owns one tokio-rusqlite Connection and serializes
Database::call/with_transaction through Semaphore::new(1).

This is intentionally simple and should remain the default write architecture.

rust/src/db/repositories.rs::DashboardRepository::load performs multiple
aggregate/list queries within one Database::call, so a slow dashboard request
can occupy the same gate used by publication/finalization/metrics work.

### Build a realistic file-backed fixture

Do not use :memory: for the primary contention measurement.

Seed a temporary database through current migrations/repositories with enough
history to make dashboard queries non-trivial.

Suggested orders of magnitude:

- 10k requests for a small retained history;
- 100k requests for a larger local deployment;
- representative events/pings/routing rows.

Use bounded synthetic metadata only. Never seed credentials, prompts, or raw
provider bodies.

### Measure

Measure:

1. dashboard load alone;
2. request publication/finalization loop alone;
3. both concurrently;
4. repeated dashboard refresh during concurrent inference lifecycle writes.

Record:

- DashboardRepository::load duration;
- publication/finalization p50/p95 where measurable;
- time waiting to enter Database::call if a temporary test-only measurement can
  be obtained without production instrumentation;
- SQLITE_BUSY/timeout behavior;
- database size.

### Inspect query plans

Run EXPLAIN QUERY PLAN for the major DashboardRepository queries against the
seeded database.

Check whether current migration indexes cover:

- requests by started_at/account/model/status;
- events by timestamp/type;
- provider pings by timestamp/account/model;
- routing decisions by timestamp/account/model;
- any join key used by the dashboard aggregations.

Do not add an index because it appears intuitively useful. Add one only when
the actual plan shows a scan that is material in the measured workload.

If an index is required, add the next immutable migration and qualify migration
upgrade/backup/recovery normally.

### Small unconditional cleanup

If DashboardRepository still builds SQL by repeatedly replacing keywords in
dashboard_sql, replace that runtime string-rewrite helper with readable static
SQL constants/strings while touching the code.

This is a low-risk allocation cleanup, but do not mix it with query semantic
changes.

## Conditional Workstream 2A — Dedicated dashboard read connection

Implement this only if dashboard activity measurably delays lifecycle writes or
holds the sole gate for a material portion of request latency.

If justified:

- keep exactly one authoritative write connection;
- add at most one dedicated read-only async SQLite connection for
  dashboard/reporting queries;
- use it only for file-backed databases;
- retain WAL semantics;
- do not create a general connection pool;
- do not permit writes from the read connection;
- do not change migration ownership;
- keep :memory: tests on the existing shared connection unless a deterministic
  shared-memory design already exists;
- close the read connection with the same runtime/database lifecycle;
- preserve current dashboard results/error behavior.

Prefer constructing the read connection from the same validated database path
rather than teaching every repository about multiple database handles.

If read-only connection creation cannot be made lifecycle-safe and the measured
contention is small, leave the single-connection design intact.

### database.worker_threads

DatabaseConfig still exposes worker_threads for compatibility, while the current
db::connection::DatabaseConfig does not use it to create multiple workers.

Do not reinterpret worker_threads as "number of SQLite writers" in this plan.

Preserve the config key. Any future deprecation/documentation cleanup is
separate unless needed to explain the measured architecture.

## Workstream 3 — Characterize the streaming task/channel bridge

### Current path

server/inference.rs currently:

- receives StreamingExecution;
- creates mpsc::channel(32);
- spawns one body task;
- repeatedly awaits execution.next_chunk();
- sends Bytes through the channel;
- wraps ReceiverStream in Body::from_stream.

The chunks are Bytes, so the bridge does not inherently copy each payload, but
it does add:

- one task;
- one channel;
- scheduler wakeups;
- up to 32 queued chunks per stream.

### Fixture matrix

Use deterministic local streaming fixtures with:

- small chunks, e.g. approximately 100-500 bytes;
- larger chunks, e.g. approximately 4-16 KiB;
- 100-1000 chunks;
- fast downstream consumer;
- deliberately slow downstream consumer;
- concurrency 1, 16, and optionally 64 when host memory permits.

Record:

- first-byte latency;
- end-to-end duration;
- CPU;
- RSS;
- queued/backpressure behavior if observable;
- cancellation/finalization correctness.

### Change threshold

Do not refactor because an extra task exists.

Only replace the bridge when measurements show a clear material cost in the
target workload and a direct body implementation can preserve all ownership
semantics.

If a direct implementation is justified, it must preserve:

- no whole-stream buffering;
- bounded translated Responses state;
- exact native byte forwarding;
- downstream backpressure;
- post-handoff no-retry rule;
- idle timeout behavior;
- terminal evidence requirements;
- usage metric recording;
- durable finalization;
- cancellation/drop convergence;
- generation lease ownership.

Do not merely shrink the channel capacity without evidence. That trades memory
for scheduling/backpressure behavior and is not automatically an optimization.

If the current bridge is inexpensive, record that result and keep it.

## Workstream 4 — Characterize Tokio current_thread runtime

### Current state

rust/src/main.rs uses:

    #[tokio::main(flavor = "current_thread")]

The Tokio feature set includes rt but not rt-multi-thread.

The server configuration accepts a thread count for compatibility, but the
runtime does not currently use it to select a worker pool.

This is a sensible default for small LAN/SBC deployments.

### Measure only after Plans 226/227

Repeated JSON parsing is synchronous CPU work and can make a single-thread
reactor look worse than it is.

Do not evaluate multithreading until the deterministic request-path CPU work is
removed.

### Workload

On a multicore target representative of intended deployment, preferably a
Raspberry Pi 5 class device when available, test:

- many concurrent large request admissions against a loopback provider;
- concurrent streaming responses;
- dashboard refresh plus inference;
- cancellation/retry/finalization stress.

Compare current_thread to a temporary two-worker experimental build.

Record p50/p95/p99 and CPU utilization.

### Conditional implementation

Only make server.threads functional if the two-worker experiment produces a
clear repeatable tail-latency/throughput improvement that outweighs the
lifecycle complexity.

If justified:

- keep default threads = 1;
- preserve current single-thread behavior when configured as 1;
- enable Tokio rt-multi-thread only as needed;
- construct the runtime in a way that does not duplicate CLI/config parsing
  policy or weaken startup error handling;
- preserve deterministic tests;
- qualify shutdown, reload, task supervision, cancellation, DB access, and
  provider pools under both 1 and 2 workers.

Do not make multithread the default in this campaign.

If dynamic runtime construction requires a broad CLI/bootstrap redesign, stop
and record the result rather than forcing it for a marginal gain.

## Workstream 5 — Characterize routing selection-lock contention

RoutingRouter::select_and_claim_with_preference acquires one async selection
mutex, but after acquisition it performs no awaited network or SQLite work.

Measure under high local concurrency before changing it.

A lightweight test-only timing around lock acquisition is acceptable if it
does not enter production diagnostics.

If lock wait is negligible compared with request preparation/provider latency,
leave the design unchanged.

Do not introduce lock-free quota/fairness/probe coordination without a
substantial measured bottleneck; those structures encode correctness-sensitive
claim ownership.

## Workstream 6 — Release/dependency footprint evidence

If Plan 191 lands during the campaign, record separately:

- final release artifact bytes;
- resolved package count;
- direct Eggress implementation dependencies removed;
- default/no-default feature graph;
- provider transport suite results.

Do not claim runtime latency gains from dependency removal unless measured.

The maintenance/footprint benefit is valid independently.

## Focused correctness gates after any conditional change

### Dashboard/DB change

Run database, migration, dashboard, publication/finalization, backup/recovery,
and runtime lifecycle targets relevant to the touched code.

At minimum include current equivalents of:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
~~~

Also run dashboard/database focused targets present in rust/tests.

### Streaming bridge change

Run:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
~~~

### Runtime-threading change

Run the full workspace serial suite plus runtime_lifecycle_r002-r013, CLI
contract, serve/shutdown/control tests, provider transport, coordinator
publication/finalization, and no-default-feature checks.

Do not rely on benchmark success as correctness evidence.

## Full validation

At closure:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
~~~

If Cargo.toml/Cargo.lock changed, also run:

~~~bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
~~~

## Closure record requirements

Append a closure record to this plan containing:

- implementation/candidate commit SHAs;
- benchmark host/toolchain/target;
- request workload matrix;
- baseline versus Plan 226 versus Plan 227 results;
- dashboard seeded row counts and query-plan findings;
- dashboard/write-contention result;
- whether a read-only dashboard connection was implemented, with evidence;
- stream bridge result and whether code changed;
- current_thread versus two-worker result and whether code changed;
- routing lock result and whether code changed;
- Plan 191 dependency/footprint result if applicable;
- full validation result;
- known measurement limitations.

Then append a concise closure record to Plan 225 summarizing the campaign and
set both statuses complete.

Do not rewrite completed historical plans.

## Stop conditions

Stop a conditional optimization and retain the simpler architecture when:

1. the measured difference is within normal run-to-run noise;
2. the change improves throughput but regresses p95/p99 or cancellation
   behavior materially;
3. a read-only dashboard connection complicates migration/close/recovery more
   than the observed contention warrants;
4. a stream-body rewrite risks whole-stream buffering or finalization
   ownership;
5. making server.threads functional requires broad startup/CLI redesign for a
   marginal result;
6. routing-lock changes require duplicating quota/fairness/health ownership;
7. a proposed benchmark requires live provider credentials or records request
   content.

## Completion criteria

This plan is complete when:

- [ ] comparable request-path performance evidence is recorded;
- [ ] dashboard gate contention is measured on a file-backed database;
- [ ] major dashboard EXPLAIN QUERY PLAN results are recorded;
- [ ] any new index has measured justification and a proper migration;
- [ ] no second SQLite writer/general pool was introduced;
- [ ] streaming bridge overhead/backpressure is characterized;
- [ ] Tokio current_thread versus two-worker behavior is characterized on a
      multicore target where practical;
- [ ] routing selection-lock contention is characterized;
- [ ] only evidence-supported structural changes landed;
- [ ] all conditional changes have focused correctness coverage;
- [ ] full repository validation is green;
- [ ] Plan 225 receives final measured closure evidence.

## Handoff note

This is a closure/evidence plan, not a mandate to optimize every measured
component.

If Plans 226 and 227 remove the dominant local CPU/allocation costs and the
remaining SQLite/stream/runtime contention is small, the correct result is to
document that and stop. Eggpool benefits more from a simple, predictable
single-node architecture than from speculative concurrency machinery.
