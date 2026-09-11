# Deep Dive: Health, Circuit Breakers, and Quarantine

Back to [Architecture](README.md)

`rust/src/health/` owns account health, bounded backoff, circuit breakers, and
per-model quarantine. Health effects are classified from the provider response
and applied at the narrowest safe scope: a model-specific failure quarantines
the account/model pair, while transport failures may advance the account-wide
breaker.

Routing consults health and quarantine state before claiming an account. A
quarantine does not silently disable unrelated models or credentials. Readiness
uses a cached probe snapshot and never performs a write.

Health transitions are durable where needed for restart safety, bounded in
diagnostics, and included in generation lifecycle/reconciliation contracts.
