# Deep Dive: Data Models

Back to [Architecture](README.md)

Typed configuration and runtime models live in `rust/src/config.rs`,
`rust/src/accounts/registry.rs`, `rust/crates/eggpool-model-routing/`,
`rust/src/model_router.rs`, `rust/src/coordinator/`, and the database
repositories. `AccountRegistry`/`AccountIdentity` is immutable,
credential-free routing identity; credentials render only at dispatch.
They preserve protocol identities, provider/account ownership,
reasoning-control dimensions, request lifecycle state, and bounded diagnostics.

Semantic selection happens before provider/account routing: the neutral crate
validates/compiles policy (`model-router/v1`, fingerprints, bounded static
policy, hashed session identities) while `rust/src/model_router.rs` owns only
the process-shared bounded Tokio affinity cache (4,096 entries, TTL
1–604,800 s, single-flight joins counted, no raw request/credential data).
The generation owns the compiled model-router registry. A selector chooses a
concrete model and cannot pin an account, bypass health/quota, or reselect
after submission. See [Routing](deep-dive-routing.md) for the selection and
claim transaction.

Unknown capability data remains distinct from unsupported data. Structural
configuration is validated before generation publication; generation-owned
models are immutable after construction and process-owned affinity/state is
kept separate. The shared model-routing crate contains no HTTP, provider,
catalog, configuration-file, or application-routing types.
