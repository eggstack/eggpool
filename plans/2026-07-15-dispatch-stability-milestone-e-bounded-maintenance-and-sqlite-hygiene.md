# Dispatch Stability Milestone E — Bounded Maintenance and SQLite Hygiene

Date: 2026-07-15
Status: detailed handoff plan
Roadmap: `plans/2026-07-15-long-running-dispatch-overhead-stability-roadmap.md`
Milestone: E of G
Depends on: Milestone A; may proceed in parallel with D after B

## Objective

Ensure retention, reconciliation, backfill, rollup, checkpoint, and other periodic database work cannot monopolize EggPool's single primary SQLite writer connection. Convert unbounded maintenance passes into resumable bounded batches, add row/time budgets, and expose database/WAL growth and maintenance backlog diagnostics suitable for long-running SBC deployments.

## Problem statement

EggPool deliberately uses one process-owned primary SQLite connection and `BEGIN IMMEDIATE` transactions for predictable write serialization. This is appropriate for correctness, but several periodic tasks can operate on an unbounded number of rows in one transaction:

- request and reservation retention;
- account event retention;
- routing/operational trace retention where present;
- usage rollup cleanup;
- stale request finalization;
- expired reservation reconciliation;
- model-info canonical backfill/repair;
- ping cleanup;
- WAL checkpoint activity.

As tables and indexes grow, these tasks can take longer, produce larger WAL bursts, and block dispatch persistence/finalization. Milestone B prevents this from becoming a global selection-lock convoy, but dispatch still waits for the sole writer. Long transactions must therefore be bounded directly.

## Architectural decision

Adopt a common maintenance-pass contract:

```text
periodic tick
  -> determine bounded candidate set
  -> process one batch in one transaction
  -> commit
  -> update runtime/in-memory reconciliation outside transaction
  -> yield to event loop
  -> stop when row budget, time budget, batch budget, or no work remains
  -> persist/derive cursor for next tick
```

Each maintenance task must declare:

- maximum rows per transaction;
- maximum transactions/batches per tick;
- maximum wall-clock budget per tick;
- query ordering/cursor semantics;
- idempotency and resume behavior;
- whether it can safely skip work under active contention;
- operational priority.

Correctness-critical stale-state cleanup remains reliable, but it must not scan/update unlimited rows under one lock acquisition.

## Scope

### In scope

- Chunked retention and cleanup.
- Bounded stale request/reservation reconciliation.
- Bounded model-info and ping backfills/cleanup where required.
- Maintenance budgets and common result diagnostics.
- WAL checkpoint strategy and telemetry.
- Database/WAL size and retention backlog metrics.
- Query-plan/index verification.
- Cancellation-safe resume behavior.
- Contention-aware scheduling and task staggering compatibility.

### Out of scope

- Replacing SQLite or changing to multiple writers.
- VACUUM on every schedule.
- Background compaction in an external process.
- Correctness-critical dispatch microbatching; milestone C.
- Generic job scheduler redesign.

## Target files and modules

Primary:

- `src/eggpool/background/cleanup.py`
- `src/eggpool/runtime_tasks.py`
- `src/eggpool/background/__init__.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/db/repositories.py`
- `src/eggpool/db/rollup_repository.py`
- model-info repository/service modules
- ping repository
- routing decision/operational event repositories
- `src/eggpool/runtime_metrics.py`
- `src/eggpool/models/config.py`

Potential new module:

- `src/eggpool/background/maintenance.py`

Schema/index files:

- `src/eggpool/db/schema/*.sql`
- migration/version metadata

Tests:

- cleanup/repository unit tests;
- large-row integration tests;
- cancellation and resume tests;
- runtime task cadence tests;
- milestone A performance harness.

## Workstream E1 — Inventory maintenance tasks and classify priority

Create an authoritative table with:

- task name;
- ownership;
- interval/initial delay/timeout;
- tables touched;
- read/write behavior;
- current transaction scope;
- expected cardinality;
- correctness priority;
- safe deferral period;
- required in-memory reconciliation;
- existing indexes used.

Suggested priority classes:

### P0 — correctness recovery

- stale request finalization;
- expired reservation reconciliation;
- finalization retry drain.

These must run reliably but still use bounded batches. They should receive priority over lossy analytics cleanup.

### P1 — storage safety

- request/reservation retention;
- event/trace/rollup retention;
- WAL checkpoint.

These prevent unbounded storage growth but can defer briefly under acute dispatch contention.

### P2 — metadata repair/refresh

- model-info backfill/repair;
- ping cleanup;
- ancillary canonicalization.

These may defer when the writer queue is saturated, provided staleness is visible.

## Workstream E2 — Common maintenance result and budget contract

Add a reusable result structure such as:

```python
MaintenancePassResult(
    task_name,
    rows_scanned,
    rows_changed,
    batches_completed,
    duration_ms,
    remaining_estimate,
    stopped_reason,
    last_cursor,
    error_class,
)
```

Stopped reasons:

- complete;
- row budget;
- batch budget;
- time budget;
- contention guard;
- cancelled;
- error.

Add configuration defaults, either globally with per-task overrides or directly on relevant sections:

- `maintenance.max_rows_per_batch`;
- `maintenance.max_batches_per_tick`;
- `maintenance.max_tick_duration_ms`;
- task-specific overrides for stale finalization and retention;
- checkpoint thresholds.

Defaults should be conservative for SBCs. Suggested initial values to benchmark:

- 500–1,000 rows per transaction;
- 1–4 batches per tick;
- 100–500 ms wall-clock budget for low-priority cleanup;
- higher but bounded budget for P0 recovery tasks.

Configuration must validate positive bounds and enforce hard internal maxima to prevent accidental unbounded operation.

## Workstream E3 — Chunk request and reservation retention

Current request retention can delete all qualifying reservations and requests in a single transaction. Replace with deterministic batches.

Recommended algorithm:

1. Select up to `batch_size` request IDs older than cutoff, ordered by `(started_at, id)`.
2. In one transaction:
   - delete dependent reservations/attempt-related diagnostic rows as schema requires, or rely on verified cascades;
   - delete requests by selected IDs.
3. Commit.
4. Update pass counters.
5. Yield.
6. Repeat while budget remains.

Use keyset/cursor pagination rather than large `OFFSET` scans. The delete itself changes the result set, so repeatedly selecting the oldest limited rows may be sufficient and simpler.

Verify foreign-key cascade behavior. Avoid duplicate explicit child deletes if cascades already provide safe indexed deletion. Conversely, do not assume cascades without schema tests.

Add indexes supporting the selection query, typically `requests(started_at, id)` if not already present. Confirm with `EXPLAIN QUERY PLAN`.

## Workstream E4 — Chunk event, trace, ping, and rollup retention

Apply the same pattern to:

- account events;
- operational events;
- routing decisions/traces;
- pings;
- usage rollups;
- other append-only observability tables.

Each table should have a time-plus-ID index suitable for limited oldest-first deletion.

Use table-specific repository methods rather than generic dynamic SQL where possible. Avoid accepting arbitrary table names from config or external input.

Expose backlog estimates without full counts on every tick. Options:

- existence check beyond cutoff;
- limited count up to a cap;
- periodic exact count on stats connection;
- oldest eligible row age.

Preferred runtime metric: oldest eligible age plus `more_remaining` boolean, with exact counts calculated less frequently off the primary path.

## Workstream E5 — Bound stale request finalization

`finalize_stale_requests_once()` currently identifies all matching pending requests and may update all of them in one transaction with a dynamically sized `IN` clause.

Refactor to process a limited ordered set per transaction.

Requirements:

- select no more than batch size;
- retain status predicates in updates to avoid racing legitimate finalization;
- return only rows actually transitioned;
- reconcile quota reservations and active counts outside the transaction;
- ensure a zero-cost reservation still decrements active count exactly once;
- if in-memory reconciliation fails, retain enough durable information for retry;
- avoid unbounded `IN (?, ?, ...)` generation;
- process multiple batches only within P0 time/batch budget.

Consider using `UPDATE ... WHERE id IN (SELECT id ... LIMIT ?) RETURNING ...` if supported by the minimum SQLite version and query plan is sound. Otherwise select IDs then update within the same transaction.

## Workstream E6 — Bound expired reservation reconciliation

The current `UPDATE ... RETURNING` can transition all eligible reservations. Add a limited candidate selection.

Maintain the `NOT EXISTS` guard for pending requests. Process transitioned rows outside the transaction to update quota estimator and router active counts.

If process interruption occurs after durable transition but before runtime reconciliation, startup or periodic reconciliation must repair the in-memory state from authoritative durable rows. Document the recovery mechanism and add a failure-injection test.

Do not extend a database transaction while awaiting router/quota locks.

## Workstream E7 — Bound model-info backfill and repair

Audit `backfill_missing_canonical()`, legacy detail block repair, catalog reconciliation, and refresh-due work for unbounded reads/writes.

Requirements:

- process limited model IDs per batch;
- deterministic ordering;
- carry or derive cursor safely;
- no network calls inside database write transactions;
- prefetch/transform network or catalog data outside transaction;
- commit compact row batches;
- expose backlog and last progress;
- avoid forcing a full backfill every short cadence after milestone A scheduler correction.

If a backfill is expected to be one-shot, record completion/progress so repeated ticks do not rescan the entire table.

## Workstream E8 — WAL checkpoint policy

Current periodic passive checkpointing is appropriate as a low-interference default, but fixed four-hour cadence alone may allow large WAL files during sustained write load.

Add telemetry first:

- database file size;
- WAL file size;
- SHM file size where present;
- last checkpoint timestamp;
- checkpoint mode;
- returned busy/log/checkpointed frame counts;
- checkpoint duration;
- checkpoint errors;
- writes/transactions since last checkpoint.

Then implement threshold-aware passive checkpointing:

- periodic cadence remains;
- optionally trigger when WAL exceeds a configured size threshold and no recent checkpoint is active;
- use `PASSIVE` by default;
- do not use `TRUNCATE` or `RESTART` automatically under active request load without evidence and explicit policy;
- defer low-priority checkpoint if dispatch writer queue/DB lock wait is above threshold;
- ensure only one checkpoint task runs.

Expose a manual operator command for stronger checkpoint/VACUUM only if one already fits the CLI model. `VACUUM` should remain explicit maintenance because it requires exclusive work and can be expensive on microSD.

## Workstream E9 — Contention-aware maintenance admission

Use runtime signals to decide whether P1/P2 tasks should defer before acquiring the primary DB:

- dispatch writer queue occupancy/oldest age, if milestone C is present;
- DB lock-wait p95;
- finalization retry queue depth;
- event-loop lag;
- active request count.

Add hysteresis and a maximum deferral age. A task must not starve forever because traffic is continuously moderate.

P0 tasks should generally proceed but remain batch-bounded. P1/P2 tasks may stop with `contention_guard` and retry later.

Do not use the guard as a substitute for bounded transactions.

## Workstream E10 — Event-loop yielding and task timeout

Between committed batches:

- update diagnostics;
- perform required in-memory reconciliation;
- `await asyncio.sleep(0)` or equivalent cooperative yield;
- re-check time/batch budget and cancellation.

Do not yield in the middle of a transaction.

Set task-level timeouts longer than the per-tick budget but still bounded. A timeout should cancel before the next batch, rollback any active transaction safely, and preserve progress from committed prior batches.

## Workstream E11 — Runtime and dashboard diagnostics

Expose per maintenance task:

- configured row/batch/time budgets;
- rows scanned/changed last tick;
- batches last tick;
- duration;
- stopped reason;
- remaining/backlog signal;
- oldest eligible age;
- cumulative rows changed;
- last success/error;
- next run and observed cadence;
- contention deferral count.

Expose database storage metrics:

- DB/WAL/SHM bytes;
- page count/page size/freelist count where safe;
- last checkpoint result;
- retention cutoffs;
- oldest retained request/event timestamps.

Use the read-only stats connection for expensive diagnostic counts when possible.

## Test plan

### Unit tests

- each cleanup method respects row limit;
- batch loop respects batch and time budgets;
- stopped reasons correct;
- deterministic oldest-first ordering;
- cancellation after first committed batch preserves progress;
- contention guard defers P1/P2 but not indefinitely;
- no transaction spans event-loop yield or runtime reconciliation;
- config bounds and hard maxima;
- checkpoint threshold/hysteresis logic.

### Database integration tests

Populate large synthetic tables and verify:

- no transaction changes more than configured rows;
- all eligible rows are eventually removed across ticks;
- foreign keys remain valid;
- query plans use intended indexes;
- stale request finalization transitions only limited rows;
- expired reservation reconciliation decrements runtime state once;
- interrupted reconciliation repairs on next pass/startup;
- WAL metrics/checkpoint behavior works on file-backed DB;
- `:memory:` tests degrade gracefully without filesystem metrics.

### Performance tests

Under active request load, trigger:

- large request retention backlog;
- event/trace cleanup;
- stale request backlog;
- expired reservation backlog;
- model-info backfill;
- WAL threshold checkpoint.

Compare dispatch/database p95/p99 with the old unbounded pass. Verify latency spikes are shorter and bounded, and backlog drains over multiple ticks.

## Acceptance criteria

1. No periodic maintenance task performs an unbounded write/update/delete over all eligible rows in one transaction.
2. Every targeted task has explicit row, batch, and wall-clock budgets.
3. Committed progress survives cancellation and resumes on later ticks.
4. Runtime quota/active-count reconciliation occurs outside the DB transaction and remains exactly-once/idempotent.
5. P0 cleanup remains reliable under load; P1/P2 tasks may defer but cannot starve beyond documented maximum age.
6. Query-plan tests verify time-plus-ID indexes for batched selection/deletion.
7. WAL/database size and checkpoint diagnostics are exposed without secrets.
8. Automatic checkpointing remains passive and does not introduce exclusive stalls by default.
9. Under synthetic large backlog, dispatch p95/p99 spikes remain within the configured maintenance transaction budget envelope and are materially lower than the unbounded baseline.
10. Backlogs eventually drain under sustainable load.
11. Rehash does not duplicate maintenance tasks or reset process-owned progress incorrectly.
12. Full tests, ruff, format check, and pyright pass.

## Rollout and rollback

Land batching per task, starting with request/event retention, then stale request/reservation reconciliation, then metadata cleanup. Keep old methods only as private test references during migration.

Rollback criteria:

- rows are skipped permanently because of an incorrect cursor;
- runtime reservations/active counts are decremented twice or not at all;
- retention violates configured cutoff;
- cleanup starves indefinitely;
- query plans regress to repeated full scans;
- checkpoint policy causes sustained busy errors or long stalls.

If rollback is required, prefer reducing per-tick batch count to one and disabling contention-aware deferral before restoring unbounded transactions.

## Handoff evidence

Provide:

- maintenance inventory and priority table;
- per-task budget defaults;
- before/after transaction duration and dispatch spike comparison;
- EXPLAIN QUERY PLAN output for each batched selector;
- cancellation/resume evidence;
- WAL growth/checkpoint snapshots;
- backlog drain test results;
- SBC storage guidance.

## Exit condition

Milestone E is complete when all high-impact periodic database work is bounded and resumable, SQLite/WAL behavior is visible, maintenance cannot monopolize the primary writer for an unbounded interval, and large retention/recovery backlogs drain without destabilizing dispatch.