# Deep Dive: Dashboard and Stats API

Back to [Architecture](README.md)

`rust/src/server/dashboard.rs` serves the dashboard and static routes, while
`rust/src/server/health.rs` owns health/readiness/runtime-status/status endpoints
(`GET /api/status` is the authenticated compact proxy/provider snapshot sharing
readiness evaluation with `readyz`; aggregation lives in
`rust/src/operations/status.rs`).
The shared server assembly and route topology remain in `rust/src/server/mod.rs`.
Embedded assets live under `rust/assets/`. `rust/src/operations/metrics.rs` and the database
repositories provide bounded, redacted snapshots for request, usage, model,
runtime, health, and routing views.

The dashboard is observational: it does not alter routing, quota, health, or
provider state. Authenticated operational endpoints return metadata-only
responses, and rendering escapes operator/provider-controlled values. Runtime
and generation views distinguish active and retiring ownership without exposing
raw prompts, credentials, cache keys, or provider bodies.

Default auth posture: `[dashboard].public` defaults to `true`, so ordinary
pages and non-sensitive stats data render without an API key. Inference
(`/v1/*`), integration (`/api/integrations/*`), and runtime/update/status
endpoints never inherit that exemption and stay authenticated;
`eggpool dashboard public --off` restores key auth on ordinary pages and data.

The native server owns route registration and static asset delivery. Changes to
dashboard assets or API contracts must update the Rust asset manifest and the
corresponding Rust integration tests. Dashboard handlers remain observational
and do not become a second runtime authority.
