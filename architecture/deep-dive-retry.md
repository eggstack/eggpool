# Deep Dive: Retry and Failure Classification

Back to [Architecture](README.md)

`rust/src/coordinator/failure.rs` classifies provider, transport, protocol,
credential, rate, model, and wire-surface failures. `rust/src/health/` applies
the narrowest matching health/quarantine effect, while the coordinator owns
retry and failover decisions.

All account and wire retries consume one shared bounded upstream-submission
budget. Retries are allowed only before downstream handoff and never replay a
request after a client-visible terminal has started. Rate pressure ends wire
discovery without suppressing a candidate; cancellation cannot release
capacity it did not own.
