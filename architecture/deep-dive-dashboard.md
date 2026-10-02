# Deep Dive: Dashboard and Stats API

Back to [Architecture](README.md)

`rust/src/server/dashboard/` is the dashboard subsystem. Its `mod.rs` is a
thin facade that preserves the route entry points consumed by
`rust/src/server/mod.rs`. `routes.rs` owns page request parsing and bounded
data gathering; `api.rs` owns dashboard JSON request parsing and projections;
`assets.rs` and `theme.rs` own static/theme delivery; `response.rs` owns
response/degraded helpers; and `format.rs` owns shared escaping and scalar
formatting. Pure page renderers live under `render/`: `layout.rs`,
`overview.rs`, `accounts.rs`, `models.rs`, `telemetry.rs`, `diagnostics.rs`,
`runtime.rs`, and `cache.rs`. They consume gathered snapshots and do not read
database or runtime state. Unit tests remain in `tests.rs` under the same
`server::dashboard::tests` test path. `rust/src/server/health.rs` owns
`GET /v1/healthz`, `GET /v1/readyz`,
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

The dashboard stylesheet sets `min-width: 0` on panels so max-content tables
stay within their `.table-scroll` wrapper on narrow screens. This is the sole
intentional difference from the frozen Python CSS; the Rust asset manifest
pins its corrected bytes. Theme CSS translates the embedded Halloy palette to
the complete 46-variable dashboard contract, including derived chip, tag,
button, link, and heatmap colors.

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

The dashboard chart client also reads `GET /api/timeseries` and
`GET /api/timeseries/grouped`. These are ordinary dashboard-gated read
endpoints: they follow `[dashboard].public`, validate the same four periods,
and return bounded bucket and grouped-series projections from dashboard
usage/request repositories. The grouped endpoint accepts `group_by`
(`provider_model`, `provider`, `model`, or `account`), a bounded `limit`, and
retains the historical `metric` parameter while ranking by request count. It
does not expose request bodies or credentials. Operational
diagnostics such as `/api/stats/runtime` remain separately authenticated.

Runtime and Cache pages share bounded owner projections with these five
dashboard-gated compatibility routes: `/api/stats/transcoding`,
`/api/stats/cache-observability`,
`/api/stats/canonical-request-segmentation`, `/api/stats/cache-stability`, and
`/api/stats/request-shaping`. They read finalized request counters and
segmentation/cache aggregates from `DashboardRepository`; process task,
generation, reload, and metrics-write facts come from the process-owned
`RuntimeDiagnosticsSnapshot`. Grouped request dimensions are capped at 100
keys where grouped projections are available, and hash columns, opaque
summaries, raw bodies, credentials, and cache keys are never read for these
views. A provider cache-hit rate remains null
because its eligible-input denominator is not persisted. Metrics not owned by
the current runtime (such as outbound pool build/request counts) are rendered
as not collected rather than as zero.

Runtime process age, parent PID, daemon hint, and host platform are read from
the current process. Linux load average is read from `/proc/loadavg`; hosts
without that safe snapshot source report load average as unavailable. The
dashboard does not spawn host utilities or add unsafe process APIs for this
observation.

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

## Overview, Accounts, and Models projections

`DashboardRepository` and `UsageRollupRepository` provide bounded historical
usage, per-account/model/IP aggregates, events, pings, token activity, and
latency percentiles. Overview and account renderers take enabled state from
the account configuration/database projection and health/backoff state from
the current router health snapshots. Persisted pings are used only when no
live snapshot exists; a newer live snapshot wins over an older successful
ping. Model availability follows the current catalog resolution result, and
model metadata/pricing/benchmarks come from the existing model-info and
pricing owners. The overview token calendar is built from the 180-day rollup
projection and uses the selected embedded theme palette.

Some fields from the retired renderer have no corresponding current source.
Request-shaping/compression policy, provider cache-hit denominators, account
budget status, and a missing live account health snapshot remain unavailable.
Model routing priority comes from the active generation's provider
configuration. Renderers preserve an explicit unknown value;
they do not infer these facts from request counts, model ordering, or an
absence of errors. Catalog resolution can also differ from the retired
renderer's broader “available” label: the current label reflects whether the
configured model resolves through the Rust catalog and can be routed.

Dashboard HTML does not add persistence or provider probes to fill these
gaps. The page handlers project owner-maintained snapshots, while read/query
costs remain bounded on the shared SQLite gate. The work package, acceptance
criteria, and evidence requirements are tracked in
`plans/implementation/dashboard/003-overview-account-model-parity.md`.

## Telemetry, Routing, Reliability, and Traces projections

M004 telemetry pages are projections of persisted request attempts, requests,
provider pings, routing decisions, usage rollups, and operational events.
Their repository reads are bounded: dashboard request/event/ping tables cap
at 100 rows, operational summaries at 25 event types, recent operational
events at 25 rows, routing selections at 100 groups, latency percentile groups
at 400, timeseries detail groups at 200, and bandwidth activity at 180 daily
rollups. The routing page reads persisted decisions; it never repeats account
selection or retry policy. The routing trace panel reports configured mode
and rate; writer counters remain unavailable when the runtime has no
authoritative writer snapshot.

Startup recovery records one `crash_recovery` operational event from the
runtime lifecycle recovery owner, with only interrupted-request,
released-reservation, and distinct affected-account counts. The reliability
page aggregates those safe JSON facts and caps recent event details at 200
characters. Trace rows intentionally omit request bodies, prompts, tool
arguments, raw error messages, client IPs, and cache keys; they expose only
bounded request/attempt metadata already persisted in the dashboard model.
Latency and timeseries values use persisted request/attempt observations;
bandwidth uses byte totals from bounded rollups. Missing live/runtime facts
remain unavailable rather than being inferred by page rendering.

The milestone evidence and source-to-panel review are recorded in
`plans/closure/dashboard/004-status.md`.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib server::dashboard::tests -- --test-threads=1
uv run python scripts/qualification_dashboard_parity.py --screenshots
uv run python scripts/qualification_dashboard_parity.py --shutdown-restart
```
