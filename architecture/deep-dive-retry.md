# Deep Dive: Retry and Failure Classification

Back to [Architecture](README.md)

`rust/src/coordinator/failure.rs` owns classification and retry legality:
`FailureSource` (transport, provider response, client/validation, local
preparation, database, cancellation), `FailureCategory` (bad request,
authentication, quota, rate limit, temporary, transient transport, model
unavailable, wire-rejected, cancelled, fatal), `RetryScope`
(none/account/wire/wait), and `NextAction` (complete, retry-account,
retry-wire, wait-rate-limit, exhaust). `RetryPolicy` bounds the shared
upstream-submission budget (`max_attempts`, default 3; `max_retry_after`
cap); `FailureDecisionEngine` classifies once per attempt with an
`EffectLedger` so retried finalization observes the same decision without
applying account/model effects twice. `rust/src/health/` applies the
narrowest matching health/quarantine/backoff effect, while the coordinator
(`coordinator/finite.rs`, `coordinator/streaming/coordinator.rs`) owns
retry and failover decisions.

All account and wire retries consume one shared bounded upstream-submission
budget: both `RetryAccount` and `RetryWire` require
`attempt_number < max_attempts`, and wire rejection (`reject_candidate` via
`coordinator/wire_resolver.rs`) applies only to wire-signal failures with
an alternate wire available. Rate pressure ends wire
discovery without suppressing a candidate; cancellation cannot release
capacity it did not own. Retries are allowed only before downstream handoff
(`!response_started && !downstream_started`): `streaming/coordinator.rs`
owns all pre-handoff retry/alternate-wire decisions, and returning
`StreamingExecution` closes that window — `streaming/execution.rs` plus
`streaming/terminal.rs` finalize post-handoff failures rather than replaying
them.
