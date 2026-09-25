# Deep Dive: Observability and Routing Traces

Back to [Architecture](README.md)

`rust/src/operations/metrics.rs` and coordinator instrumentation emit bounded
request, routing, failure, usage, and lifecycle facts. Traces record decisions,
not credentials, prompts, cache keys, or raw provider bodies.

`rust/src/operations/status.rs` owns the compact proxy/provider health snapshot
(shared readiness evaluation with `readyz`, no outbound probes, secret-free).
`rust/src/runtime_lifecycle/diagnostics.rs` owns the bounded process/runtime
projections (active/retiring generations, publication/reload/task/shutdown
counters with truncated text). `rust/src/coordinator/streaming/diagnostics.rs`
owns the bounded streaming-outcome counters (counts, durations, labels only).
Persistence is buffered only where the selected low-wear policy permits it;
correctness-critical lifecycle and accounting rows remain durable.
