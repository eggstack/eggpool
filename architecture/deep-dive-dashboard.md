# Deep Dive: Dashboard and Stats API

Back to [Architecture](README.md)

`rust/src/server.rs` serves the dashboard and stats routes using the embedded
assets under `rust/assets/`. `rust/src/operations/metrics.rs` and the database
repositories provide bounded, redacted snapshots for request, usage, model,
runtime, health, and routing views.

The dashboard is observational: it does not alter routing, quota, health, or
provider state. Authenticated operational endpoints return metadata-only
responses, and rendering escapes operator/provider-controlled values. Runtime
and generation views distinguish active and retiring ownership without exposing
raw prompts, credentials, cache keys, or provider bodies.

The native server owns route registration and static asset delivery. Changes to
dashboard assets or API contracts must update the Rust asset manifest and the
corresponding Rust integration tests.
