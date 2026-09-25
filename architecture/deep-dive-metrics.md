# Deep Dive: Metrics and Telemetry

Back to [Architecture](README.md)

`rust/src/operations/metrics.rs` owns bounded request, usage, latency, failure,
reasoning, and routing telemetry. Correctness-critical request/accounting
state is persisted immediately; low-wear analytics may be coalesced under the
configured metrics mode.

The coalescer buffers scalar-only `UsageMetricEvent` facts keyed by bounded
(bucket, provider, model, account, protocol, status) tuples. `MetricsConfig`
defaults to `write_mode = "low_wear"` with `max_buffered_events = 250`;
`"immediate"` flushes through the same coalescer/transaction boundary before
returning, while buffered modes only enqueue. `MetricsSnapshot` exposes
`total_flushed`/`flush_failures`/`buffered_events` counters only.

Metrics labels and snapshots are bounded, deterministic, and metadata-only.
They never contain credentials, raw request bodies, cache keys, or provider
response bodies. Pricing contributes to accounting and observability only; it
does not influence routing.

The process exposes operational and runtime snapshots through the native CLI
and dashboard APIs. Generation and finalization ownership counters distinguish
active work from retiring work, and shutdown performs one deadline-bounded
flush before the database closes.
