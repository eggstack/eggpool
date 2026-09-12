# Deep Dive: Routing and Quota

Back to [Architecture](README.md)

`rust/src/routing/` and `rust/src/quota/` select eligible provider accounts
using health, availability, active load, quota reservations, routing priority,
and bounded fairness. Routing is load-based and never cost-based.

`rust/crates/eggpool-model-routing/` owns the neutral semantic policy boundary:
structural validation, deterministic compilation, route IDs, static selector
policy bytes, fingerprints, and hashed session identities. EggPool's
`rust/src/model_router.rs` retains the process-owned Tokio TTL/LRU/single-flight
affinity cache and adapts TOML config into the neutral policy types. A selector
chooses a concrete model before ordinary provider routing; it cannot pin an
account, bypass health/quota, or reselect after submission.

Claims and reservations are released on every terminal path. Quarantine,
backoff, and capability gates are evaluated before selection and remain scoped
to the provider/model facts that produced them.

## Shared crate contract

EggPool owns `eggpool-model-routing`; Codegg is a pinned downstream consumer.
The crate's `model-router/v1` static-policy format is a semantic protocol
contract separate from the crate's Rust semver. Public Rust API breaks require
review and updates in Codegg before the pin moves. Changes to policy bytes or
fingerprint semantics require explicit compatibility review and may require a
selector protocol-version decision. Provider/account routing, quota, health,
retry, and transport remain outside the shared crate.
