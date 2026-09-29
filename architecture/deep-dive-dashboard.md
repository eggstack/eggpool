# Deep Dive: Dashboard and Stats API

Back to [Architecture](README.md)

`rust/src/server/dashboard.rs` serves the dashboard and static routes, while
`rust/src/server/health.rs` owns `GET /v1/healthz`, `GET /v1/readyz`,
`GET /v1/models`, `GET /api/stats/runtime`, `GET /api/stats/update`, and the
authenticated compact `GET /api/status` snapshot plus the authenticated
versioned `GET /api/integrations/v1/profile` (aggregation lives in
`rust/src/operations/status.rs`, sharing readiness evaluation with `readyz`).
The shared server assembly and route topology remain in `rust/src/server/mod.rs`.
Embedded dashboard assets live under `rust/assets/dashboard/static/`
(`dashboard.css`, `dashboard.js`, `chart.umd.min.js`, `favicon.svg`, served
under `/static/`); `/static/theme.css` renders the selected embedded theme
from `rust/assets/dashboard/themes/`. `rust/src/operations/metrics.rs` and the database
repositories provide bounded, redacted snapshots for request, usage, model,
runtime, health, and routing views.

## Pages and API routes

When `[dashboard].enabled`, the router serves `/`, `/accounts`, `/models`,
`/models/{*model_id}`, `/latency`, `/events`, `/timeseries`, `/bandwidth`,
`/pings`, `/reliability`, `/routing`, `/traces`, `/runtime`, `/cache`, plus
`GET /api/stats/summary` — all rendered server-side from
`db::DashboardRepository` / `db::UsageRollupRepository` snapshots for one
of four validated periods (`1h`, `24h`, `7d`, `30d`; anything else is
`400 Invalid period`). `GET /v1/models` stays the standard OpenAI-schema
model list and is always authenticated; the rich sanitized projection is
the separately versioned integration-profile endpoint.

The dashboard is observational: it does not alter routing, quota, health, or
provider state. Authenticated operational endpoints return metadata-only
responses, and rendering escapes operator/provider-controlled values. Runtime
and generation views distinguish active and retiring ownership without exposing
raw prompts, credentials, cache keys, or provider bodies.

## Rendering and themes

Pages share one layout (`render_dashboard_layout`) with period/theme
selectors, auto-refresh footer, and preloaded static assets. All dynamic
values pass through `html_escape()`; chart payloads pass through
`json_escape()` (angle brackets, ampersands, control characters, and
U+2028/2029 are neutralized). Model links use percent-encoded path
components (`query_component`). DB failures degrade to a bounded
`503 {"status":"degraded","reason":"dashboard data unavailable"}` — never
a stack trace or raw error.

`THEME_NAMES` holds 51 choices (`default` plus 50 named files); the
configured default is `Cyber Red` (`DEFAULT_THEME`, matching
`DashboardConfig::theme`). Unknown theme names fall back to it, and
`[dashboard].themes_dir` is a deprecated no-op: themes always come from
the embedded `rust/assets/dashboard/themes/*.toml` set parsed into CSS
variables. Static assets carry explicit cache headers; failures to read
dashboard data never block inference.

Default auth posture: `[dashboard].public` defaults to `true`, so ordinary
pages and non-sensitive stats data render without an API key. Inference
(`/v1/*`), integration (`/api/integrations/*`), and runtime/update/status
endpoints never inherit that exemption and stay authenticated;
`eggpool dashboard public --off` restores key auth on ordinary pages and data.

## Safety invariants

`dashboard.store_request_content` is validated `false`: request content
must not be persisted. Operator surfaces (`operator.rs` explain/cost/
transcoding services, `explain_dashboard` query plans) stay
presentation-light and secret-free. The native server owns route
registration and static asset delivery. Changes to
dashboard assets or API contracts must update the Rust asset manifest and the
corresponding Rust integration tests. Dashboard handlers remain observational
and do not become a second runtime authority.
