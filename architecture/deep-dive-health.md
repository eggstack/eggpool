# Deep Dive: Health, Circuit Breakers, and Quarantine

Back to [Architecture](README.md)

`rust/src/health/` owns account health, bounded backoff, circuit breakers, and
per-model quarantine. Health effects are classified from the provider response
and applied at the narrowest safe scope through `effects.rs`: a
model-specific failure quarantines the provider/account/model/protocol key,
while transport/auth failures may advance the account-wide breaker and
bounded backoff.

Routing consults health and quarantine state before claiming an account. A
quarantine does not silently disable unrelated models or credentials.
Readiness shares the bounded evaluation in `rust/src/operations/status.rs`
over generation config, enabled accounts, credentials, catalog counts, and
cached health snapshots; it performs no outbound provider probes and no
writes.

Restart-safety state is limited to the schema-54 `account_backoffs` and
`model_quarantine` tables via `repository.rs`, with validated hydration and
bounded diagnostics. Health transitions stay inside generation
lifecycle/reconciliation contracts.
