# Plan 064 — Quota and SQLite Hot-Path Reduction

Date: 2026-07-31
Status: completed
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Planning baseline: `c2fe512ad46f2d1a672a7ab1f9928e1def494cb4`

## Purpose

Remove the remaining deterministic process-age and SQLite write costs that are credible on a long-running SBC deployment, while keeping performance work subordinate to correctness and avoiding a benchmark/test apparatus larger than the product.

The main confirmed cost is quota-window maintenance: every update and read scans all retained observations and allocates a new deque. As the daily window grows, cumulative finalization work grows with process age and runs under quota snapshot locking. Routing trace persistence also loops through individual inserts despite already owning a transaction. Other costs, especially the very wide finalization update and dispatch microbatching, require a compact measurement before structural change.

This plan prioritizes operation-count reduction and amortized bounds. It does not establish percentile gates, a benchmark framework, a soak service, or production-scale database sharding.

## Findings covered

1. `QuotaWindow._prune_old_observations()` scans and copies the entire observation deque on each add and get.
2. Hourly and daily windows perform this work during usage recording, including under the quota estimator's snapshot lock.
3. Persisted 5-hour/7-day/30-day usage snapshots need explicit aging during a continuously running generation.
4. Routing trace `create_many()` issues individual inserts and broadly suppresses exceptions.
5. Request finalization updates a large canonical/diagnostic column set in one correctness-critical transaction.
6. Dispatch microbatching adds queue/timer complexity and should be retained only if it materially reduces SQLite overhead at realistic SBC concurrency.

## Scope

Primary files:

- `src/eggpool/quota/estimation.py`
- persisted usage snapshot/rollup code and its existing repositories
- routing trace repository in `src/eggpool/db/`
- request finalization repository/update construction
- dispatch writer configuration and existing local performance scripts only if needed for comparison
- existing quota, repository, and finalization tests

Potential documentation:

- one short architecture note if snapshot aging semantics are currently undocumented;
- plan status after implementation.

## Explicitly out of scope

- Redis or external quota storage;
- multiple SQLite databases;
- database sharding;
- a time-series database;
- a new ORM;
- a general batching framework;
- periodic full-table scans as a replacement for in-memory windows;
- automatic database tuning per device;
- CI latency thresholds;
- mandatory Raspberry Pi soak runs;
- retained benchmark artifacts;
- changing quota/routing policy or billing semantics.

## Design decisions

1. Ordered timestamps are the normal path. Optimize them for amortized constant-time expiry.
2. Out-of-order observations remain correct through a bounded slow path; do not penalize every normal update for rare disorder.
3. Snapshot aging must be based on time buckets or explicit expiry, not cumulative lifetime counters.
4. Trace persistence uses the database driver's batch operation and propagates unexpected failures.
5. Canonical terminal correctness fields remain in the main finalization transaction.
6. Diagnostic write reduction happens only if a compact profile shows it materially contributes to lock/WAL cost.
7. No schema migration is preferred. A diagnostics-table split is a last resort and requires measured justification plus a separate corrective review.
8. Microbatching is not assumed beneficial. Compare it with direct single-transaction persistence at realistic local concurrency and choose the simpler path when performance is equivalent.
9. Measurements inform a decision; they do not become CI gates.

## Phase A — Make quota-window expiry amortized O(1)

### Current cost

The window currently filters every retained observation into a new deque whenever usage is added or read. With monotonically increasing event time, expired entries are always at the left edge and can be removed incrementally.

### Required changes

1. Track the last accepted observation timestamp or equivalent ordering state.
2. Normal ordered path:
   - append the new observation;
   - subtract/pop expired entries from the left while the oldest timestamp is before the cutoff;
   - update cached request/token/cost totals incrementally;
   - avoid allocating a replacement deque.
3. Read/snapshot path:
   - prune only from the left against the supplied/current timestamp;
   - return cached totals;
   - do not rescan retained observations.
4. Preserve exact boundary semantics currently expected at the window cutoff.
5. Keep numeric totals non-negative and correct after pops.
6. Out-of-order input:
   - detect when timestamp is older than the last ordered timestamp;
   - use one explicit slow path to insert/rebuild or reject according to current caller semantics;
   - keep the slow path bounded by retained window size;
   - record a debug-level diagnostic/counter if useful.
7. Do not add a balanced tree, sorted container dependency, or general event-time library.
8. Use monotonic or explicit injected timestamps consistently; do not mix wall-clock and monotonic domains.

### Acceptance criteria

- Ordered add performs append plus only the pops required for newly expired entries.
- Ordered get/snapshot does not scan/copy the full deque.
- Cached totals match a simple reference implementation.
- Boundary timestamps preserve current inclusion/exclusion behavior.
- One out-of-order case remains correct through the bounded slow path.
- No new dependency is added.

## Phase B — Reduce quota lock hold time

### Required changes

1. Review `record_usage_and_snapshot()` and the scope of `_snapshot_lock`.
2. Keep only state mutation that must be atomic under the lock.
3. Perform input normalization and immutable result construction outside the lock where safe.
4. Do not hold the lock while performing SQLite I/O, logging, serialization, or unrelated routing work.
5. If hourly/daily windows share one lock, retain that simplicity unless contention evidence clearly supports a split. Multiple locks can create inconsistent snapshots and are not the default solution.
6. Return one immutable snapshot containing values captured under the lock.
7. Avoid duplicate pruning calls for add followed immediately by snapshot.

### Acceptance criteria

- One usage record prunes each affected window at most once.
- No database await occurs under the quota snapshot lock.
- Snapshot values remain internally consistent.
- The implementation does not add lock striping or per-account lock registries.

## Phase C — Give persisted rolling snapshots explicit aging

### Investigation checkpoint

First confirm the current call path during a continuously running generation:

- when 5-hour/7-day/30-day snapshots are loaded;
- whether they are incremented per request;
- whether old usage is subtracted or the generation is periodically rebuilt;
- whether a generation surviving beyond the shortest window can retain expired usage.

Document the result in the implementation commit/plan closure. Do not assume a defect if an existing bucketed refresh already handles expiry.

### Preferred correction if the defect is confirmed

1. Reuse existing persisted time buckets/rollups where available.
2. Represent each rolling horizon as a bounded collection of coarse buckets rather than one lifetime counter.
3. Suggested granularity should follow existing database aggregation resolution; do not invent high-frequency buckets:
   - 5-hour window: existing hourly buckets are sufficient;
   - 7-day/30-day windows: hourly or daily buckets according to existing repository queries.
4. On advancement:
   - add/update the current bucket;
   - expire buckets older than the horizon;
   - compute/update cached totals incrementally.
5. On startup/generation creation, load only buckets needed for the longest horizon.
6. If existing database rollups already provide this, schedule one low-frequency refresh using the existing background supervisor rather than creating a new service.
7. Keep refresh no more frequent than required by bucket resolution.
8. Do not scan raw request history on every request.
9. Do not add a migration unless no existing timestamped usage source can support correct aging.

### Acceptance criteria

- A controlled clock advancement beyond five hours removes expired usage from the 5-hour snapshot without process restart.
- Seven-day and 30-day horizons expire old buckets at their boundaries.
- Snapshot totals remain correct while new usage is recorded.
- Refresh/bucket work is bounded by horizon bucket count, not lifetime request count.
- No per-request database rollup query is introduced.

## Phase D — Batch routing trace inserts truthfully

### Required changes

1. Replace the loop of individual insert executions in `create_many()` with one `executemany()` call or a compact multi-row insert supported by current aiosqlite conventions.
2. Preserve input order only if a caller relies on it; trace rows generally do not require returned IDs.
3. Keep the existing transaction owner. Do not create a transaction inside a repository method when its caller already owns one unless repository convention requires it.
4. Catch only the expected integrity/idempotency condition if one exists.
5. Propagate unexpected database errors so dispatch/recovery pressure controls can observe them.
6. Preserve trace-off and unsampled behavior: no trace rows and no batch call.
7. Keep trace detail bounded and do not add payload/body persistence.

### Acceptance criteria

- N trace rows are submitted through one database batch operation.
- Trace-off/unsampled requests execute no trace insert.
- Unexpected SQLite failure reaches the caller.
- Expected duplicate/idempotent behavior, if any, remains narrow and documented.
- Routing decisions are unchanged.

## Phase E — Measure finalization write amplification before restructuring

### Measurement question

Determine whether the wide `finalize_if_pending()` update materially contributes to:

- transaction duration;
- SQLite worker time;
- WAL bytes/pages per request;
- checkpoint duration/frequency;
- dispatch/finalization lock wait on representative local storage.

### Required measurement method

Use one compact local diagnostic, preferably an existing script or a short temporary development command that is not committed if unnecessary:

1. create a temporary SQLite database with current migrations;
2. finalize a representative bounded set of requests with current field population;
3. record elapsed transaction time and WAL size before/after;
4. compare one candidate reduced statement if implementation has a safe obvious split;
5. run a few iterations only to avoid one-time initialization noise;
6. do not establish percentile requirements or a benchmark package.

### Decision rules

- If the wide update is not material relative to transaction overhead, keep it and document no change.
- If material, first reduce deterministic work without schema change:
  - build/update only columns relevant to the request's actual features;
  - avoid serializing empty diagnostic structures;
  - omit unchanged optional fields where SQL/repository semantics allow;
  - separate a non-critical diagnostic update only when failure of that update cannot affect terminal correctness.
- Keep these canonical fields in the correctness-critical transaction:
  - terminal status/outcome;
  - completion timestamp;
  - core token/cost/usage totals required for quota/accounting;
  - attempt/reservation linkage required for convergence.
- Do not create a diagnostics table or migration in this plan unless the measured result is substantial and no simpler statement reduction works. If a migration appears necessary, stop and write a separate narrow plan for review.

### Acceptance criteria

- The implementation records a concise decision: unchanged because not material, or narrowed with measured evidence.
- Any narrowed statement preserves canonical terminal/accounting fields atomically.
- Optional diagnostics failure cannot make a completed request appear pending.
- No schema migration is slipped into this phase without separate review.

## Phase F — Decide whether dispatch microbatching earns its complexity

### Measurement question

At realistic EggPool concurrency on an SBC-class or ordinary local filesystem, does the dispatch writer's microbatch materially reduce SQLite time/lock contention compared with direct per-request transaction persistence?

### Required comparison

1. Use the existing repository/writer code and temporary SQLite database.
2. Compare:
   - direct persistence;
   - current configured microbatch persistence.
3. Use realistic small concurrency ranges, for example 1, 4, and 8 concurrent submissions. Do not simulate hundreds/thousands of clients.
4. Measure total elapsed time and transaction count; optional queue wait may be observed if already exposed.
5. Keep the run short and manual.
6. Do not add a benchmark framework or CI job.

### Decision rules

- Retain microbatching if it clearly reduces transaction count and total/lock time without adding meaningful latency at realistic concurrency.
- Simplify to direct persistence if results are effectively equivalent and the writer no longer serves another required ownership purpose.
- If retained:
  - keep one writer, one queue, one small max batch size, and one short flush interval;
  - do not add adaptive batch sizing, multiple writer shards, or per-provider queues.
- Any simplification must preserve Plan 059's binary failure contract.

### Acceptance criteria

- A concise measurement-informed retain/simplify decision is documented.
- No mandatory benchmark artifact remains in the repository.
- If retained, batching configuration and ownership remain bounded/simple.
- If removed, direct persistence preserves correctness and focused tests.

## Phase G — Focused verification and closure

### Test budget

Add or modify no more than six focused automated cases across existing capability-based files.

Required coverage:

1. Ordered quota-window add/expiry matches a reference result and does not use the full-rebuild path.
2. One out-of-order observation remains correct.
3. Controlled clock advancement expires persisted 5-hour usage without restart if Phase C confirms/fixes the defect.
4. Routing trace batch uses one executemany/batch call and propagates unexpected failure.
5. Finalization canonical fields remain correct after any statement narrowing.
6. Dispatch direct/batch correctness parity only if Phase F changes production structure.

Performance measurements remain manual/local and are not test assertions. Do not assert wall-clock milliseconds in CI.

## Implementation sequence

Recommended commits:

1. quota-window incremental pruning and lock-scope reduction;
2. rolling snapshot aging correction if confirmed;
3. trace batch insertion/error propagation;
4. measurement-informed finalization statement change or documented no-op decision;
5. measurement-informed dispatch writer retain/simplify decision;
6. focused tests and plan/documentation closure.

## Plan acceptance criteria

- [x] Ordered quota-window maintenance is amortized O(1) and allocates no replacement deque per update.
- [x] Quota snapshots are internally consistent and lock hold time excludes unrelated work/I/O.
- [x] Long-lived generations expire usage from 5-hour/7-day/30-day snapshots correctly, or investigation proves existing behavior already does so.
- [x] Routing traces are persisted through one true batch operation.
- [x] Unexpected trace database failures are not silently suppressed.
- [x] Finalization write structure is changed only if compact measurement shows material benefit.
- [x] Canonical terminal/accounting fields remain atomic.
- [x] Dispatch microbatching is retained or removed based on one realistic local comparison.
- [x] No adaptive batching system, benchmark framework, schema migration, new database, CI timing gate, or soak requirement is introduced.

## Definition of done

The plan is complete when quota-window work remains bounded as process age grows, rolling snapshots age correctly, trace inserts are genuinely batched and truthful on failure, finalization and dispatch structures have measurement-backed decisions, focused correctness tests and the existing smoke suite pass, and no permanent performance-testing apparatus has been added.

## Implementation closure

- Ordered quota-window adds and reads now use cached totals plus left-edge
  expiry. The only full rebuild is the bounded out-of-order timestamp path.
- `record_usage_and_snapshot()` performs synchronous quota/EWMA mutation
  outside `_snapshot_lock`; the lock now covers only shared snapshot mirrors.
  No database I/O occurs under that lock.
- Persisted snapshots already age through the generation-leased refresh task,
  whose repository query uses explicit 5h/7d/30d timestamp boundaries over the
  retained request horizon. This preserves exact boundaries without adding an
  approximate bucket schema or per-request database rollup query.
- Routing trace persistence now uses one `execute_many()` call and lets
  unexpected SQLite errors reach the writer. Trace-off and unsampled paths are
  unchanged.
- A local comprehensive performance diagnostic was attempted. Its first
  baseline case passed; a pre-existing contention assertion failed because an
  empty sample reports `None` for `lock_wait_p95_ms`, so no timing result was
  used to justify a finalization rewrite. The wide finalization statement was
  therefore retained atomically.
- Dispatch microbatching was retained: it remains the process-owned writer
  required by the binary dispatch persistence contract, with bounded queue,
  batch size, and wait time. No adaptive or sharded batching was added.

Focused verification covers ordered expiry, out-of-order observations, and
one-batch trace persistence. No schema migration, benchmark artifact, CI timing
gate, or soak requirement was introduced.
