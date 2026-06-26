# SQLite low-wear metrics buffering plan

## Context

EggPool is intended to run comfortably on small SBCs, including Raspberry Pi-class devices that are often deployed with microSD storage. The current SQLite layer already has several good foundations for this environment: one SQLite connection by default, a single aiosqlite worker thread, explicit transaction ownership, `BEGIN IMMEDIATE` write transactions, WAL mode, and `synchronous = "NORMAL"` by default.

The remaining concern is write amplification from observability. A single proxied model request can produce multiple durable writes: pending request creation, reservation creation, attempt creation, parent request timestamping, attempt completion, reservation release, final request completion, provider ping rows, timeseries rows, bandwidth rows, and dashboard-facing aggregate data. Some of these writes are correctness-critical and should remain near-synchronous. Others are lossy analytics and can be buffered safely.

The design decision for this plan is:

- Keep routing/control-plane writes immediate enough to preserve correctness after restart.
- Buffer only analytics/observability writes whose bounded loss is acceptable after power failure.
- Prefer aggregate rollup tables over append-heavy event tables where exact per-event history is not needed.
- Make the behavior configurable, with a default balanced profile and an opt-in low-wear SBC profile.

## Goals

1. Reduce microSD wear by lowering commit frequency and append-heavy analytics writes.
2. Preserve request lifecycle correctness, reservation semantics, routing behavior, retry accounting, and stale-request recovery.
3. Keep dashboard data reasonably fresh in the default profile.
4. Provide an explicit low-wear profile for microSD deployments.
5. Bound memory usage and data-loss windows.
6. Make shutdown flushes deterministic and testable.
7. Avoid introducing a second SQLite writer thread or extra process.

## Non-goals

- Do not defer active request creation, active reservation creation, final request state, upstream suppression/backoff state, or retry-control state by minutes.
- Do not switch the default database to `synchronous = OFF`.
- Do not run frequent automatic `VACUUM` as a wear-reduction strategy.
- Do not remove existing per-request tables until dashboard/API callers have been migrated and tests prove parity.
- Do not make buffered metrics authoritative for routing eligibility.

## Write classification

### Correctness-critical writes: keep immediate

These writes should remain directly persisted during the request lifecycle, though adjacent operations should be grouped into fewer explicit transaction boundaries where practical:

- Request row creation for an accepted routed request.
- Active reservation creation and release/expiry.
- Attempt row creation and attempt terminal state, where retry logic or trace integrity depends on it.
- Final request transition from `pending` to terminal state.
- Upstream error/suppression/backoff state used by routing.
- Account/provider config synchronization.
- Model catalog updates if startup/readiness depends on them.
- Migration/version writes.

Implementation guidance:

- Audit coordinator call sites that currently perform several repository writes sequentially.
- Where operations are logically part of one lifecycle step, wrap them in one `async with db.transaction():` at the service/coordinator level rather than allowing each repository call to commit independently.
- Preserve idempotent finalization semantics such as `WHERE id = ? AND status = 'pending'`.
- Do not make analytics buffering a dependency of request completion returning to the client.

### Lossy analytics writes: eligible for buffering

These are candidates for in-memory aggregation and periodic flush:

- Timeseries grouped usage buckets.
- Bandwidth heatmap buckets.
- Provider/model/account aggregate counters.
- Latency sums/min/max/counts used for dashboard summaries.
- Token/cost aggregates used only for dashboard views.
- Provider ping history beyond the latest health snapshot.
- Detailed request traces, if sampling is enabled.
- Operational event rollups that are not needed for routing correctness.

Implementation guidance:

- Keep exact request rows if current dashboard/API code still depends on them, but introduce aggregate tables and migrate dashboard reads to the aggregates.
- For low-wear mode, make detailed traces sampled or disabled while preserving aggregate totals.

## Configuration model

Add a new `[metrics]` section. Keep defaults conservative and dashboard-friendly.

```toml
[metrics]
# immediate: existing direct-write behavior where practical.
# balanced: buffer lossy analytics with short flush intervals.
# low_wear: longer flush interval, coarser buckets, optional trace sampling.
write_mode = "balanced"

# Flush buffered analytics at least this often.
flush_interval_s = 30

# Flush earlier if buffered aggregate keys/events exceed this bound.
max_buffered_events = 500

# Default rollup bucket size for dashboard timeseries and bandwidth views.
timeseries_bucket_s = 60

# 1.0 stores all detailed trace/request diagnostic rows that are otherwise enabled.
# 0.05 stores roughly 5% of eligible detailed traces while preserving aggregate totals.
trace_sample_rate = 1.0

# When true, skip optional detailed analytics rows and persist only rollups.
aggregate_only = false
```

Add a documented SBC example:

```toml
[metrics]
write_mode = "low_wear"
flush_interval_s = 120
max_buffered_events = 250
timeseries_bucket_s = 300
trace_sample_rate = 0.05
aggregate_only = true
```

Validation requirements:

- `write_mode` must be one of `immediate`, `balanced`, `low_wear`.
- `flush_interval_s` should be clamped or rejected outside a sane range, e.g. 1-600 seconds.
- `max_buffered_events` should be positive and bounded, e.g. 1-100_000.
- `timeseries_bucket_s` should be positive and preferably divide common periods cleanly, e.g. 10, 30, 60, 300, 900, 3600.
- `trace_sample_rate` must be `0.0 <= rate <= 1.0`.
- `aggregate_only = true` should not disable correctness-critical request state.

## Data model changes

Add rollup tables through a migration. Exact names can be adjusted to match the current schema conventions.

### `usage_rollups`

Purpose: dashboard usage/timeseries aggregation without scanning or appending one analytics row per request.

Suggested schema:

```sql
CREATE TABLE usage_rollups (
    bucket_start TEXT NOT NULL,
    bucket_size_s INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    account_id INTEGER,
    protocol TEXT NOT NULL,
    streamed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_characters INTEGER NOT NULL DEFAULT 0,
    cost_microdollars INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    bytes_emitted INTEGER NOT NULL DEFAULT 0,
    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
    latency_ms_min INTEGER,
    latency_ms_max INTEGER,
    first_byte_ms_sum INTEGER NOT NULL DEFAULT 0,
    first_byte_ms_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        bucket_start,
        bucket_size_s,
        provider_id,
        model_id,
        account_id,
        protocol,
        streamed,
        status
    )
);
```

SQLite primary keys treat `NULL` in composite keys awkwardly. Prefer one of these patterns:

- Use `account_id INTEGER NOT NULL DEFAULT 0` where `0` means unknown/all accounts.
- Or use generated/normalized text keys.

For simplicity, prefer `account_id INTEGER NOT NULL DEFAULT 0` in implementation.

Required indexes:

```sql
CREATE INDEX idx_usage_rollups_bucket ON usage_rollups(bucket_start, bucket_size_s);
CREATE INDEX idx_usage_rollups_provider_model ON usage_rollups(provider_id, model_id, bucket_start);
CREATE INDEX idx_usage_rollups_account ON usage_rollups(account_id, bucket_start);
```

### `provider_health_rollups` or adapted ping rollup

Purpose: keep latest health exact enough while avoiding unbounded ping history churn.

Potential approach:

- Keep latest provider/account/model health snapshot immediate if readiness/routing needs it.
- Buffer historical ping aggregates per `(bucket_start, provider_id, account_id)`.
- Store count, success_count, failure_count, latency sum/min/max, last_status_code, last_error_class.

### Existing request table retention

Do not remove existing request rows in this phase. Instead:

- Keep request rows for correctness and traceability.
- Migrate dashboard endpoints that only need aggregate totals to read from rollups.
- Add retention cleanup that deletes or compacts old request detail rows after the configured retention period.
- Do not run `VACUUM` automatically after retention cleanup.

## New component: metrics write coalescer

Add a component such as `eggpool.metrics.buffer.MetricsWriteCoalescer`.

Responsibilities:

- Accept immutable analytics events from request finalizers, provider pingers, retry handlers, and dashboard-visible event producers.
- Convert event timestamps to bucket starts using configured `timeseries_bucket_s`.
- Aggregate deltas in memory by stable keys.
- Flush periodically or when `max_buffered_events` is reached.
- Flush on application shutdown.
- Expose in-memory status for runtime diagnostics.
- Never block request completion on slow analytics flush except in `write_mode = immediate`.

Suggested public API:

```python
@dataclass(frozen=True)
class UsageMetricEvent:
    timestamp: datetime
    provider_id: str
    model_id: str
    account_id: int | None
    protocol: str
    streamed: bool
    status: str
    retry_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    thinking_characters: int
    cost_microdollars: int
    bytes_received: int
    bytes_emitted: int
    latency_ms: int
    first_byte_ms: int | None

class MetricsWriteCoalescer:
    def record_usage(self, event: UsageMetricEvent) -> None: ...
    async def flush(self, reason: str) -> FlushResult: ...
    async def run(self, stop_event: asyncio.Event) -> None: ...
    def snapshot(self) -> dict[str, object]: ...
```

Implementation details:

- `record_usage()` should be non-async or very cheap. It may take an `asyncio.Lock` if necessary, but avoid awaiting database I/O.
- Keep aggregate maps bounded. If the number of keys exceeds a hard limit, trigger a flush or degrade by folding rare keys into an `Other` bucket.
- Use `db.execute_many()` inside one transaction for flushes.
- Use `INSERT ... ON CONFLICT DO UPDATE` to merge buffered deltas into rollup tables.
- Track dropped event count if memory bounds are exceeded. Dropping should be explicit in runtime diagnostics.
- Use monotonic timers for flush scheduling but wall-clock timestamps for bucket assignment.

Suggested upsert shape:

```sql
INSERT INTO usage_rollups (
    bucket_start, bucket_size_s, provider_id, model_id, account_id,
    protocol, streamed, status,
    request_count, error_count, retry_count,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens, thinking_characters, cost_microdollars,
    bytes_received, bytes_emitted,
    latency_ms_sum, latency_ms_min, latency_ms_max,
    first_byte_ms_sum, first_byte_ms_count, updated_at
) VALUES (...)
ON CONFLICT (...) DO UPDATE SET
    request_count = request_count + excluded.request_count,
    error_count = error_count + excluded.error_count,
    retry_count = retry_count + excluded.retry_count,
    input_tokens = input_tokens + excluded.input_tokens,
    output_tokens = output_tokens + excluded.output_tokens,
    cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
    reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
    thinking_characters = thinking_characters + excluded.thinking_characters,
    cost_microdollars = cost_microdollars + excluded.cost_microdollars,
    bytes_received = bytes_received + excluded.bytes_received,
    bytes_emitted = bytes_emitted + excluded.bytes_emitted,
    latency_ms_sum = latency_ms_sum + excluded.latency_ms_sum,
    latency_ms_min = CASE
        WHEN latency_ms_min IS NULL THEN excluded.latency_ms_min
        WHEN excluded.latency_ms_min IS NULL THEN latency_ms_min
        ELSE MIN(latency_ms_min, excluded.latency_ms_min)
    END,
    latency_ms_max = CASE
        WHEN latency_ms_max IS NULL THEN excluded.latency_ms_max
        WHEN excluded.latency_ms_max IS NULL THEN latency_ms_max
        ELSE MAX(latency_ms_max, excluded.latency_ms_max)
    END,
    first_byte_ms_sum = first_byte_ms_sum + excluded.first_byte_ms_sum,
    first_byte_ms_count = first_byte_ms_count + excluded.first_byte_ms_count,
    updated_at = CURRENT_TIMESTAMP;
```

## Application lifecycle wiring

1. Parse `[metrics]` into `AppConfig`.
2. During `create_app()`, construct `MetricsWriteCoalescer` after the database is connected and migrations have run.
3. Store it in application state next to repositories/services.
4. Start one background task for periodic flushing when `write_mode != immediate`.
5. Register shutdown handling to:
   - stop accepting new analytics events,
   - flush buffered metrics with reason `shutdown`,
   - then disconnect the database.
6. Add runtime-status fields:
   - metrics write mode,
   - flush interval,
   - current buffered key count,
   - current buffered event count,
   - last flush timestamp,
   - last flush row count,
   - last flush duration,
   - last flush error class,
   - dropped analytics event count.

Shutdown semantics:

- Best-effort flush is acceptable for lossy analytics.
- Do not prevent process shutdown indefinitely if flush fails.
- Use a bounded timeout, e.g. 2-5 seconds.
- Log flush failure, but do not mark request lifecycle writes failed.

## Request lifecycle integration

At the finalization boundary, after correctness-critical final request state is persisted, emit a `UsageMetricEvent` into the coalescer.

Important ordering:

1. Persist final request state immediately.
2. Release reservation immediately.
3. Update attempt state immediately if needed for trace/retry correctness.
4. Record analytics event into buffer.
5. Return/finish request path without waiting for analytics flush in `balanced` or `low_wear` modes.

If `write_mode = immediate`, the coalescer may write through directly or the existing repository path may remain active. Keep immediate mode useful as a debugging/bisect fallback.

Avoid double counting:

- Ensure every logical request produces at most one usage rollup event.
- Prefer emitting from the idempotent finalizer only when the final transition succeeds.
- If a request was already terminal and finalization returns false, do not emit a second usage event.
- Add tests for streaming cancellation and stale finalizer paths.

## Dashboard/API migration

Migrate dashboard endpoints in layers.

Phase A: add rollups without changing visible behavior.

- Keep current request-table queries.
- Write rollups in parallel.
- Add tests comparing rollup totals to request-table totals for synthetic data.

Phase B: switch aggregate endpoints to rollups.

Candidates:

- `/api/timeseries/grouped`.
- Bandwidth heatmap endpoint.
- Overview totals by period.
- Model/account/provider aggregate breakdowns where exact trace rows are not required.

Keep trace/detail endpoints on request/attempt tables.

Phase C: optional low-wear aggregate-only behavior.

- If `aggregate_only = true`, skip optional detailed analytics rows not needed for correctness.
- Dashboard pages that require unavailable detail should show a clear message or degraded view.
- Overview/timeseries/bandwidth/model/account aggregate pages should continue working from rollups.

## Retention and cleanup

Add or update retention jobs so they are wear-aware.

Requirements:

- Delete old rollup buckets based on configurable retention.
- Delete old provider ping history based on existing `ping_retain_days` or a metrics-specific value.
- Delete old detailed request traces based on `retain_request_stats_days`.
- Do not run `VACUUM` automatically after cleanup.
- Prefer chunked deletes with upper bounds, e.g. delete at most N rows per cleanup pass.
- Schedule cleanup infrequently, e.g. daily, not every dashboard refresh.

Potential config additions:

```toml
[metrics]
rollup_retain_days = 90
cleanup_interval_s = 86400
cleanup_max_rows_per_pass = 5000
```

If this is too much for the first implementation pass, keep retention unchanged and document it as a follow-up.

## SQLite/WAL considerations

Keep current defaults:

```toml
[database]
wal = true
synchronous = "NORMAL"
worker_threads = 1
```

Optional later tuning:

- Consider `PRAGMA wal_autocheckpoint` as an advanced config only after measuring WAL growth and checkpoint write behavior.
- Consider exposing a manual `eggpool db checkpoint` command for operators.
- Do not default to `synchronous = OFF`.
- Do not run frequent automatic `VACUUM`.

Add docs explaining that the largest durability improvement for SBCs remains using a USB SSD or high-endurance microSD, but the buffered metrics path reduces avoidable application-level write churn.

## Testing plan

### Unit tests

Add tests for the coalescer:

- Aggregates multiple events with the same key into one rollup delta.
- Separates events by bucket, provider, model, account, protocol, streamed flag, and status.
- Computes request count, error count, retry count, tokens, cost, bytes, latency sum/min/max, and first-byte aggregates correctly.
- Handles `first_byte_ms = None` without corrupting counts.
- Enforces memory/event bounds.
- Exposes accurate diagnostic snapshots.
- Flushes empty buffers as a no-op.
- Flush errors preserve or explicitly drop buffered data according to documented behavior.

### Repository/migration tests

- Migration creates rollup tables and indexes.
- Upsert merges counters correctly.
- Upsert min/max latency logic works for null and non-null values.
- Repeated flushes for the same bucket/key accumulate totals.
- Rollup query totals match equivalent request-table query totals for fixtures.

### Lifecycle tests

- Background flush task starts with `balanced`/`low_wear` and does not start with `immediate` if not needed.
- Shutdown invokes a bounded final flush.
- Flush failure is logged and reflected in runtime diagnostics.
- Request finalization emits exactly one usage event only when terminal transition succeeds.
- Streaming cancellation finalizer does not double-count.
- Stale-request finalizer does not double-count already-finalized rows.

### Dashboard tests

- `/api/timeseries/grouped` returns equivalent totals from rollups.
- Bandwidth heatmap uses rollups where possible.
- Overview totals match request-table totals within the selected time window.
- Low-wear/aggregate-only mode returns degraded trace detail intentionally but preserves aggregate pages.

### Wear-oriented regression tests

Add a lightweight instrumentation test using database contention counters or a test double repository:

- Simulate N completed requests.
- Compare write transaction count in immediate mode versus balanced mode.
- Assert balanced mode reduces analytics transaction count by batching rollup flushes.
- Do not require exact OS-level write byte measurements in CI.

## Documentation updates

Update:

- `config.example.toml`: add `[metrics]` defaults and comments.
- `README.md`: mention low-wear metrics buffering under Raspberry Pi/SBC deployment guidance.
- `docs/deployment.md`: add microSD guidance.
- `docs/backup-restore.md`: mention that buffered analytics may lose at most the configured flush window after abrupt power loss, but correctness-critical request state remains immediate.
- Runtime/dashboard docs: explain metrics buffer health fields.

Suggested operator guidance:

```toml
# Recommended when running from microSD and dashboard traces are not critical.
[metrics]
write_mode = "low_wear"
flush_interval_s = 120
timeseries_bucket_s = 300
trace_sample_rate = 0.05
aggregate_only = true
```

Also document:

- Use a high-endurance microSD or USB SSD for sustained multi-session use.
- Keep logs under logrotate.
- Avoid frequent manual `eggpool db vacuum` on flash media.
- Backups are useful for recovery but are not a substitute for durable storage.

## Implementation sequence

### Step 1: config and docs skeleton

- Add `MetricsConfig` to the config model.
- Add validation and defaults.
- Add example config comments.
- Add placeholder runtime-status output for metrics mode and disabled buffer state.

Acceptance criteria:

- Existing configs still load.
- `config.example.toml` documents defaults.
- Invalid metrics settings fail `eggpool check-config` with clear messages.

### Step 2: migration and rollup repository

- Add migration for `usage_rollups`.
- Add `UsageRollupRepository` with `upsert_many()` and query helpers.
- Use `execute_many()` inside a single transaction.

Acceptance criteria:

- Migration passes on fresh and existing DBs.
- Rollup repository tests pass.
- No dashboard code changed yet.

### Step 3: coalescer component

- Implement `MetricsWriteCoalescer` and event dataclasses.
- Add unit tests for aggregation and flush behavior.
- Add diagnostic snapshot.

Acceptance criteria:

- Coalescer can aggregate and flush synthetic events.
- Flush is bounded and error-reporting behavior is tested.

### Step 4: lifecycle wiring

- Construct coalescer in app startup.
- Start periodic flush task for buffered modes.
- Flush on shutdown.
- Add runtime diagnostics.

Acceptance criteria:

- Background task appears in runtime status.
- Shutdown flush test passes.
- Immediate mode remains available.

### Step 5: request finalizer integration

- Emit usage events from successful terminal finalization.
- Ensure double-finalization does not double-count.
- Preserve correctness-critical request/reservation/attempt writes.

Acceptance criteria:

- Non-streaming, streaming, cancellation, retry, and stale-finalizer tests pass.
- Rollup totals match request-table totals in fixtures.

### Step 6: dashboard aggregate migration

- Switch `/api/timeseries/grouped` to rollups.
- Switch bandwidth heatmap and overview aggregate endpoints where feasible.
- Keep trace endpoints on detail tables.

Acceptance criteria:

- Dashboard tests pass.
- Totals remain lossless for flushed data.
- Top-N/Other behavior is preserved.

### Step 7: low-wear profile and retention polish

- Implement `low_wear` defaults when selected.
- Add `aggregate_only` behavior for optional detail writes only.
- Add or tune retention cleanup for rollups and detail rows.
- Ensure cleanup is chunked and does not auto-vacuum.

Acceptance criteria:

- Low-wear mode reduces analytics write transactions in tests.
- Dashboard aggregate pages remain functional.
- Detail pages degrade clearly when detail is intentionally unavailable.

## Review checklist

Before merging, verify:

- No correctness-critical write is delayed solely because metrics mode is buffered.
- Request finalization remains idempotent.
- Buffered analytics cannot suppress, unsuppress, or reroute accounts.
- Memory bounds are explicit and tested.
- Shutdown flush has a timeout.
- `immediate` mode can be used to bisect metrics bugs.
- Existing dashboard views either preserve behavior or degrade explicitly in low-wear aggregate-only mode.
- Documentation states the maximum expected analytics loss window after power failure.
- No frequent automatic `VACUUM` was added.

## Expected outcome

For normal personal use, `balanced` mode should keep the dashboard close to live while reducing analytics commit frequency. For microSD deployments, `low_wear` mode should trade detailed trace fidelity and dashboard freshness for materially fewer writes. Routing and request lifecycle correctness should remain governed by immediate durable state, not buffered analytics.
