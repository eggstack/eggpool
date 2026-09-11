# Deep Dive: Data Models

Back to [Architecture](README.md)

Typed configuration and runtime models live in `rust/src/config.rs`,
`rust/src/model_router.rs`, `rust/src/coordinator/`, and the database
repositories. They preserve protocol identities, provider/account ownership,
reasoning-control dimensions, request lifecycle state, and bounded diagnostics.

Unknown capability data remains distinct from unsupported data. Structural
configuration is validated before generation publication; generation-owned
models are immutable after construction and process-owned affinity/state is
kept separate.
