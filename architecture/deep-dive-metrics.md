# Deep Dive: Metrics and Telemetry

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md) (§§11, 12): §11/§12 are the birds-eye summary,
this file is the metrics authority.

`rust/src/operations/metrics.rs` owns bounded request, usage, latency, failure,
reasoning, and routing telemetry. Correctness-critical request/accounting
state is persisted immediately; low-wear analytics may be coalesced under the
configured metrics mode.

## Event contract

The request path hands the boundary scalar, already-redacted
`UsageMetricEvent` facts only: a 300 s `bucket_start` timestamp plus
`bucket_size_s`, provider/model/account IDs, protocol, `streamed`, and
status, with counters for input/output/cache-read/cache-write/reasoning
tokens, `thinking_characters`, `cost_microdollars`, bytes received/emitted,
`latency_ms`, optional `first_byte_ms`, and retries. It never stores
bodies, headers, credentials, or arbitrary diagnostic text. Aggregation
is saturating and non-negative (`non_negative`/`add` clamp at zero and
`i64::MAX`), and cost accumulation is capped against `SQLITE_MAX` in the
UPSERT so a burst cannot overflow the rollup row.

## Coalescer and write modes

The coalescer buffers scalar-only facts keyed by bounded
(bucket, provider, model, account, protocol, streamed, status) tuples.
`MetricsConfig` defaults to `write_mode = "low_wear"` with
`max_buffered_events = 250` (row cap; the pending-event ceiling is 64x
that), `flush_interval_s = 120`, and `timeseries_bucket_s = 300`;
`"immediate"` flushes through the same coalescer/transaction boundary before
returning, while buffered modes only enqueue. `record_usage()` (sync)
refuses immediate mode; `record_usage_async()` flushes before returning
under it. Over-cap events and rows are counted in `total_dropped`, never
silently lost. `MetricsSnapshot` exposes
`buffered_events`/`buffered_rows`/`total_received`/`total_flushed`/
`total_dropped`/`flush_failures`/`last_flush_rows` counters only.

Ownership around the single database gate is move-based (persistence M002):
enqueue folds the event into one additive delta and then moves the already-
owned key Strings into the buffer map instead of cloning them; flush consumes
the taken map into one ordered immutable row batch shared (`Arc`) between DB
execution and failure recovery, so no second deep batch clone is retained;
the repeated UPSERT is prepared once per flush transaction and executed once
per row. Capacity, drop, additive-aggregation, rebuffer-merge, ordering, and
immediate/low-wear semantics are unchanged.

Flush holds a dedicated async flush lock, takes the buffer map, converts it
to one ordered `Arc<[MetricFlushRow]>` batch, and runs a single
`usage_rollups` UPSERT transaction (additive counters, `MIN`-capped cost,
`MIN`/`MAX`-folded latency bounds, first-byte sum/count). On failure it
increments `flush_failures` and rebuffers the shared rows subject to the
same capacity/drop policy.

## Retention and companion config

`MetricsConfig` also owns `trace_sample_rate` (default `0.05`),
`aggregate_only` (default `true`), `rollup_retain_days`,
`operational_event_retain_days`, and `routing_decision_retain_days`
(default 90), plus the generation-leased cleanup pass
(`cleanup_interval_s`, `cleanup_max_rows_per_pass`) and the opt-in
`event_loop_lag_enabled` / `dispatch_spans` switches. The SBC profile
keeps `low_wear` with model-info/backup disabled while request and
accounting durability stay intact.

## Bounds

Metrics labels and snapshots are bounded, deterministic, and metadata-only.
They never contain credentials, raw request bodies, cache keys, or provider
response bodies. Pricing contributes to accounting and observability only; it
does not influence routing.

The process exposes operational and runtime snapshots through the native CLI
and dashboard APIs. Generation and finalization ownership counters distinguish
active work from retiring work, and shutdown performs one deadline-bounded
flush before the database closes.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
```
