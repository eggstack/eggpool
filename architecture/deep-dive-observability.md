# Deep Dive: Observability and Routing Traces

Back to [Architecture](README.md)

`rust/src/operations/metrics.rs` and coordinator instrumentation emit bounded
request, routing, failure, usage, and lifecycle facts. Traces record decisions,
not credentials, prompts, cache keys, or raw provider bodies.

Snapshots distinguish active and retiring generations, reservation/finalization
ownership, and health effects. Persistence is buffered only where the selected
low-wear policy permits it; correctness-critical lifecycle and accounting rows
remain durable.
