# Deep Dive: Control Plane and Rehash

Back to [Architecture](README.md)

`rust/src/operations/control.rs` owns the Unix-domain control endpoint and
client commands. `rust/src/reload.rs` coordinates validation, candidate
construction, atomic publication, and retirement. `rust/src/config_reload_policy.rs`
is the single source of truth for live versus restart-required settings.

`eggpool rehash` serializes reloads. A candidate is complete before publication;
failure leaves the active generation intact. In-flight leases continue using
their acquired generation, while retiring generations drain finalization and
background work before resources close.

Control responses are bounded and metadata-only. They report validation,
publication, retirement, or busy outcomes without secrets, request bodies, or
raw provider responses. Binding, database topology, and other restart-owned
resources are rejected as disruptive changes rather than partially applied.
