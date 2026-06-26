---
name: architecture
description: Architecture principles and design decisions for the EggPool project. Use when understanding the codebase structure, making design decisions, or reviewing architectural changes. Covers package boundaries, request lifecycle, and core invariants.
---

# Architecture Principles

See `architecture/README.md` for the full design overview.

## Core Principles

- Package boundaries must remain explicit
- Request proxying, routing, accounting, and dashboard concerns must not be combined in endpoint handlers
- Use Pydantic v2 for all data validation
- Use aiosqlite for all database operations

## Request Lifecycle

- All data-plane requests flow through `RequestCoordinator`
- Requests must be persisted before upstream dispatch
- Pre-body failures can retry; no retry after first downstream byte emitted
- Every retryable failed attempt must reach terminal state before the next attempt
- Each attempt reservation is released exactly once via `AttemptFinalizer`
- Streaming cancellation finalization is wrapped in `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock; the outer `Stale request finalizer` background task (`app._finalize_stale_requests`) is the safety net for anything that escapes this path
- `_crash_recovery` runs at every startup and recovers ALL pending requests and active reservations (no time threshold); a process restart is a definitive boundary, so leaked state from the previous process is unconditionally cleaned up
- **Structured observability persistence (migrations 0026-0029)**: every `request_attempts` row carries `provider_id/model_id/protocol/retry_category/release_reason/bytes_received/latency_ms/streamed/is_retry_outcome`; every routing decision is persisted as a `routing_decisions` row inside the same transaction as the `request_attempts` INSERT so the audit trail cannot diverge from durable state; the safety-net tasks (`_crash_recovery`, `_finalize_stale_requests_once`, `reconcile_expired_reservations`) record `operational_events` rows in the same transaction as the durable state mutation; latency is decomposed into `upstream_connect_ms / upstream_read_ms / coordinator_overhead_ms` so the dashboard can tell whether slowness is network, upstream, or eggpool-side
- **Runtime metrics**: `eggpool runtime-status` (CLI) and `GET /api/stats/runtime` (API) expose live operational health — process topology, memory usage, background task status, DB health, and in-flight request counts. The `/runtime` dashboard page renders these metrics for operator visibility. Runtime metrics are always auth-gated regardless of dashboard public/private setting

## Process Model

- `eggpool serve` is a single supervisor process that invokes `Granian` with `workers=1`; Granian spawns one worker, so exactly two processes run under the canonical name
- The Granian worker is launched with `process_name="eggpool"`, so both supervisor and worker appear as `eggpool` in `ps` / `top` / `pgrep` (not as a generic `python` entry)
- The supervisor owns the PID file. Path resolution lives in `eggpool.runtime_paths.default_pid_file()` and follows this precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`. The supervisor writes `os.getpid()` before `Granian.serve()` and clears it in a `finally` block; the FastAPI lifespan does not touch the PID file. This prevents the "kill worker leaves supervisor orphaned" failure mode
- `eggpool serve` refuses to start a second instance: first checks `runtime.read_pid()` + `runtime.is_process_running()`; if no live PID, probes `GET /v1/healthz` via stdlib `urllib.request` (bind `0.0.0.0` / `::` is rewritten to `127.0.0.1`). A live PID or a 200 from the probe exits `1`. Stale PID files (PID not running) are cleared before starting
- `[server].threads` (int, default `1`, min `1`, max `64`) controls Granian `runtime_threads` (the number of worker event-loop threads). Default `1` keeps process and thread counts minimal for SBC / Raspberry Pi; raise on capable hardware
- `eggpool restart` no longer has inline subprocess logic; it delegates to `runtime.restart_server` which calls `runtime.send_sigterm` and `runtime.start_server` (which `subprocess.Popen`s a new supervisor)
- `eggpool ensure-running` is the canonical cron watchdog — it atomically checks-and-starts without ever spawning a duplicate instance. Use it from `@reboot` and `*/5 * * * *` crontab lines instead of `croncheck || eggpool serve &`

### Daemon Mode

- `eggpool serve --daemon` is the operator-facing detach helper for personal / SBC deployments. It validates the config, refuses to start a second instance, then spawns a detached child and returns promptly with a short success message pointing at the log file
- The detached child runs the normal foreground `serve` command (Granian supervisor + worker). `--daemon` is **never** forwarded to the child; detachment is purely a parent-side concern
- stdin/stdout/stderr are detached from the calling terminal: `stdin=subprocess.DEVNULL`, `stdout`/`stderr` → log file (or `/dev/null` when `--quiet` is set without `--log-file`). The child is launched with `start_new_session=True` so it survives shell exit and signals to the parent CLI do not propagate
- Default log destination is `~/.local/state/eggpool/eggpool.log`, resolvable via `eggpool.runtime_paths.default_log_file()`. Override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The detached child is the supervisor; it owns its own PID file lifecycle via `runtime.write_pid_file()` / `runtime.clear_pid_file()`. The `Popen` handle from `start_server()` is intentionally not awaited; the parent returns as soon as the child is spawned
- `serve --daemon` refuses to run as root unless `--as-root` is passed (prevents accidental root personal deployment)
- Systemd should **not** use `--daemon`. The systemd unit already owns the process lifecycle; run foreground `serve` and let systemd manage the PID, journal logs, and restart policy
- `runtime.start_server()` signature: `start_server(config_path, *, cwd=None, daemon=True, log_path=None, quiet=True, verify=False, verify_timeout_s=3.0)`. `runtime.restart_server()` accepts the same `daemon`, `log_path`, and `quiet` options. The CLI flags `eggpool serve --daemon`, `--log-file PATH`, `--quiet`, and `--as-root` map directly to these parameters
- See `plans/daemon-and-runtime.md` for the full design

## Installation and Deployment

- `eggpool.deploy_user` — `DeployUser`, `resolve_deploy_user()` (handles normal, `SUDO_USER`, and direct-root cases), `resolve_config_path()` (single source of truth for `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`), `resolve_env_path()`, and XDG default helpers (`default_config_dir()` / `default_data_dir()` / `default_state_dir()` / `default_config_path()` / `default_env_path()`)
- `eggpool.deploy` — bundled constants (`SYSTEMD_UNIT`, `LOGROTATE_CONF`, `CRON_BACKUP_FILE`, `CRON_BACKUP_SCRIPT`) + personal builders (`build_personal_systemd_unit`, `build_personal_watchdog_cron`, `build_personal_backup_block`, `build_personal_logrotate`) + cron block management (`install_cron_block`, `remove_cron_block`, `strip_managed_cron_blocks`). Every cron block is bracketed by `# BEGIN EggPool ...` / `# END EggPool ...` markers so uninstall only strips eggpool-owned lines
- `eggpool.cli_full.deploy_*` — Click commands: `deploy systemd [--install|--production|--as-root]`, `deploy cron [--install|--uninstall|--interval N]` (the **watchdog**, not the backup), `deploy backup-cron` (the actual backup), `deploy logrotate [--install]` (validates via `logrotate -d`), `deploy all`
- `eggpool.cli_full.uninstall [--deploy-artifacts]` — detects the install method, previews PATH edits via `preview_eggpool_path_changes()` + `RcFileChange` before writing, and removes the binary, config, data, and shell-rc entries. `--deploy-artifacts` extends this to systemd / logrotate / cron / backup-script cleanup
- Production systemd unit (`SYSTEMD_UNIT` constant) is the source of truth; `deploy/eggpool.service` is kept byte-for-byte identical
- `eggpool.deploy_user.resolve_config_path()` is the single source of truth for every CLI command's config-path resolution
- `eggpool deploy cron` is the **watchdog**; `eggpool deploy backup-cron` is the **backup**. The two are intentionally separate commands so a missing backup never blocks the watchdog and vice versa

## Fast-Path CLI

- The entry point `eggpool.cli:main` tries `fastcli.maybe_run_fast_command()` before importing Click
- The fast path imports `eggpool.runtime_paths` and `eggpool.fastcli` only — both modules are stdlib-only
- Recognized fast commands: `croncheck` (pure status probe) and `ensure-running` (check-and-spawn watchdog)
- Everything else falls through to `eggpool.cli_full` (the heavy Click CLI)
- Public symbol forwarding via PEP 562 `__getattr__` keeps `from eggpool.cli import cli` working for tests without forcing the heavy CLI graph to load at `eggpool.cli` import time
- See `plans/lightweight-cli-watchdog.md` for the full design

## Database Invariants

- SQLite is the durable source of truth for quota windows (5h/7d/30d)
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- All SQL operations on the shared connection are serialized; no task can execute SQL inside another task's transaction
- Every DML write must run inside `async with db.transaction():`; write helpers refuse to operate outside an owned transaction
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code

## Metrics Buffering

- `MetricsWriteCoalescer` buffers lossy analytics events in memory and flushes to `usage_rollups` periodically
- Correctness-critical writes (request state, reservations, routing) remain immediate and are never buffered
- Three `write_mode` values: `immediate` (direct write), `balanced` (30s flush), `low_wear` (120s flush, coarser buckets)
- The coalescer emits one `UsageMetricEvent` per terminal request transition from `RequestFinalizer.finalize()`
- Shutdown flush has a 5-second timeout; lossy analytics are best-effort
- `usage_rollups` table uses additive upserts (`INSERT ... ON CONFLICT DO UPDATE SET col = col + excluded.col`)
- Rollup retention is configurable via `metrics.rollup_retain_days` (default 90)
- Runtime diagnostics expose buffer health via `/api/stats/runtime` (`metrics_buffer` section)

## Concurrency

- Readiness probes use `probe_writable()` with owned transactions, never interfere with request lifecycle work
- Child tasks cannot inherit transaction ownership (both task identity and ContextVar depth must match)
- Reservation and active-count in-memory cleanup occur only when the database reservation actually transitions
- Exhausted retries cannot corrupt another request's in-memory state

## Quota and Routing

- Successful responses without terminal usage consume the reservation estimate
- Unknown model protocols are rejected before durable selection
- Quota-exhausted accounts recover after cooldown expiration via `_refresh_transient_state()`
- Pending active requests are excluded from expiry cleanup
- Cancelled nonzero-cost requests remain in usage windows
- Cache-only rate changes create snapshots; cache-only token usage invokes cost calculation
- Tier-based routing: eligible accounts are grouped by `routing_priority` (default `0`); the highest non-empty tier wins; the `QuotaFairScorer` load-balances within the chosen tier; lower tiers are reached only via `exclude_accounts` retry paths
- `routing_priority` orders tiers; `weight` orders accounts within a tier — the two compose
- `collapse_models = false` (default) exposes provider-suffixed model IDs; `collapse_models = true` collapses to a single unsuffixed ID routed across all providers
- **Upstream-authoritative suppression** (default `local_quota_mode = "score_only"`): local cost estimates influence routing rank but never hard-exclude accounts. Only upstream-observed failures, explicit operator disablement, catalog/protocol incompatibility, or an explicit `local_quota_mode = "hard_cap"` may make an account ineligible. See `plans/upstream-authoritative-suppression.md` for context.
- **`hard_cap` opt-in**: setting `local_quota_mode = "hard_cap"` restores legacy behavior where locally over-quota accounts are excluded. Subscription aggregators should normally leave the default unchanged; a warning is logged at startup when `hard_cap` is enabled.
- Reservation cleanup is gated on `reservation_released` alone — `health_already_applied` must not be a precondition for in-memory reservation teardown, otherwise single-account 429/402 paths leak in-memory reservation state.

## Multi-Provider

- Provider-suffixed model IDs: `model-id/provider-id` format
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` instances for upstream LLM forwarding and catalog model-list fetches
- `OutboundClientManager` owns a shared `httpx.AsyncClient` for non-provider network paths: update checks (PyPI), external catalog fetches (OpenRouter), and future background/CLI network operations. Initialized once at startup, reused by all background tasks. The `build_count` property should stabilize at 1; growth with request volume indicates a hot-path client construction bug. Accepts `[network]` config for transport tuning (connect_timeout_s, read_timeout_s, max_connections, max_keepalive, keepalive_expiry_s). Integrates a `DnsNetworkBackend` that wraps the default httpcore transport and caches resolved DNS entries in memory, reducing latency for repeated connections to the same upstream hosts. The DNS cache is controlled by `[network.dns_cache]` (enabled by default, TTL 300s, max 50 entries). Exposes `snapshot()` with build_count, request_count, error_count for runtime diagnostics. `inject_client()` is the test escape hatch for injecting mock transports
- Hot-path provider requests must **never** construct fresh HTTP clients. Background and CLI paths should use the shared outbound client from `OutboundClientManager` rather than calling `httpx.get()` or building ad-hoc clients. `warn_adhoc_clientConstruction()` emits a runtime warning after startup when fresh clients are constructed outside managed paths
- Per-provider upstream paths: `openai_path`, `anthropic_path`, `models_endpoint` (a `[providers.<id>.models_endpoint]` table with `method`, `path`, `query`, `body`, `required`; `method = "DISABLED"` skips live model listing). Legacy `models_path` / `models_method` scalars are auto-synthesized into a default `models_endpoint`.
- **`static_models`** — providers may declare `[[providers.<id>.static_models]]` rows (`ProviderStaticModelConfig`) that seed the catalog at refresh time. Required when `models_endpoint.method = "DISABLED"`. Static rows participate in the same protocol/limit machinery as live rows; static-source fields (`protocol`, `protocol_source == "static_config"`, `supports_tools`, `supports_vision`) are preserved by `ModelCatalogCache._preserve_static_fields` when live rows arrive without them.
- Legacy flat `[[accounts]]` auto-normalizes to default `opencode-go` provider
- `parse_model_provider()` in `routing/provider.py` handles suffix parsing;
  `catalog/cache.py` retains a compatibility alias
- **`routing_priority`** — `[providers.<id>]` accepts `routing_priority: int` with `Field(default=0, ge=0)`. Higher values are preferred. The field is per-provider; accounts inside a tier are still load-balanced by `QuotaFairScorer`.
- **`collapse_models`** — `[models]` accepts `collapse_models: bool` (default `false`). When `false`, the catalog exposes one provider-suffixed entry per `(model_id, provider_id)`. When `true`, the same base model collapses to a single unsuffixed `model_id` and is routed across every provider that supports it.
- `eggpool connect` writes `routing_priority = 0` on every newly created provider block and leaves existing blocks untouched, so operators can edit one number to rebalance.
- `eggpool configsetup opencode` honors `collapse_models`: suffixed model IDs when `false`, unsuffixed when `true`.
- `/v1/models` includes an `eggpool.routing_priority` extension field on each suffixed entry.
- See `plans/provider_priority.md` for the full design and `docs/providers.md` for the worked example with three providers and three priorities.

### Provider Contract Rendering

`src/eggpool/providers/contract.py` centralizes:
- `compose_provider_url()` — absolute URL composition
- `build_auth_headers()` — provider-aware auth header construction
- `build_static_headers()` — static provider headers from config
- `build_upstream_headers()` — combines auth + static headers

The coordinator calls `_build_upstream_headers()` and `_get_upstream_url()` which use the provider
contract when available, falling back to legacy Bearer auth and bare paths respectively.

### URL Composition Consistency

`compose_provider_url()` is the single source of truth for upstream URL
construction. Catalog fetch, non-streaming chat, and streaming chat all
call it through the provider config so a provider cannot list models at
one host and dispatch requests to another. The coordinator's
`_get_upstream_url()` returns an absolute URL when a provider config is
present; only the no-config fallback returns bare paths.

### MiniMax Templates

- `minimax` — international host `https://api.minimax.io/anthropic` (default for `minimax.io` token-plan keys). Uses the Anthropic-compatible transport (`x-api-key` header, `anthropic-version: 2023-06-01` static header). Live model discovery via documented `/v1/models` endpoint; static seeds serve as fallback.
- `minimax-cn` — China host `https://api.minimaxi.com/v1`. Plain OpenAI-compatible. Live verification is required before production use because the China endpoint family has not been confirmed against the Anthropic-compatible transport.

API keys must be raw tokens; EggPool prepends the configured auth scheme automatically. An optional `[providers.<id>.verify]` block controls live verification probes.

## Dashboard

### Page Architecture

- Server-rendered HTML pages in `src/eggpool/dashboard/render.py`, all using the existing `_render_layout(title, body, active_nav, period, refresh_interval_s, theme_css, available_themes, current_theme, auto_refresh, include_chart_js)` wrapper — no Jinja, no template engine
- Routes registered through `register_dashboard_routes(app, require_auth=...)` in `src/eggpool/dashboard/routes.py`; the `require_auth` flag is computed from `config.dashboard.public` once at startup and shared across every dashboard page
- Backend handlers fan out independent `StatsService` calls through `asyncio.gather` so page loads are bounded by the slowest query, not the sum of sequential round trips (the shared connection lock serializes per-query execution regardless)
- Frontend helpers live in `src/eggpool/dashboard/static/dashboard.js` under the `window.EggPoolDashboard` namespace (`fetchStats`, `formatDurationMs`, `formatAgeSeconds`, `formatPercent`, `formatCount`, `formatBytes`, `formatMicrodollars`, `formatTokens`, `formatDollarsFromMicro`, `initGroupedTimeseriesCharts`, `reinitTimeseriesChart`) — small, opt-in, no framework
- Chart.js v4 (MIT, bundled) is served at `/static/chart.js` with `Cache-Control: public, max-age=86400`; pages opt in via `include_chart_js=True` in `_render_layout`. When `include_chart_js=True`, the layout also loads `/static/dashboard.js` (the chart lifecycle helpers) in document order, both via `defer`
- Static assets (CSS, JS, favicon) are served via `app.py` handlers with appropriate `Cache-Control` headers
- Every free-text field on every page goes through `escape()` or `escape_attr()` from `src/eggpool/dashboard/escape.py`; never interpolate raw upstream or model data
- Format helpers in `escape.py` (`format_duration_ms`, `format_age_seconds`, `format_percent100`, `format_percent01`, `format_int`, `format_count_or_dash`, `short_id`) are shared by every renderer; do not redefine per-page

### Chart lifecycle

The previous inline `<script>` chart pattern ran before Chart.js had loaded (because `<script defer>` on Chart.js hadn't executed yet) and broke on overview auto-refresh because `innerHTML` replacement does not re-execute scripts. The current contract:

- Page renderers never emit inline JavaScript that calls `new Chart(...)`. They emit a `<canvas class="grouped-timeseries-chart">` plus a sibling `<script type="application/json" class="grouped-timeseries-data">` data island
- `window.EggPoolDashboard.initGroupedTimeseriesCharts()` (called from `DOMContentLoaded` and after every auto-refresh swap) reads each data island, destroys any prior `canvas.__eggpoolChart` instance, builds Chart.js datasets, and renders
- `window.EggPoolDashboard.reinitTimeseriesChart()` rebuilds the legacy overview line chart by refetching `/api/timeseries` and rebinding the canvas after the auto-refresh `innerHTML` swap. The legacy overview line chart keeps its 60s in-page refresh interval for in-place updates
- `_render_auto_refresh_script()` invokes both helpers after `content.innerHTML = next.innerHTML;` so overview auto-refresh preserves both the legacy line chart and any grouped chart
- Pages that do not render a chart must not include `/static/chart.js` or `/static/dashboard.js` (`include_chart_js=False`)

### Grouped Timeseries

The `/timeseries` page replaces the old "table of bucket counts" with a stacked-bar grouped chart plus a grouped detail table:

- **Data contract**: `/api/timeseries/grouped` returns `{bucket, group_by, metric, limit, series, buckets, bucket_totals, points}`. Each `point` is one `(bucket, series_key)` row carrying per-bucket counters (request_count, error_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens, cost_microdollars, bytes_received, bytes_emitted, avg_latency_ms, avg_ttft_ms). Each `bucket_totals` entry aggregates across all series (including the folded `Other` bucket) so totals stay loss-less
- **Group dimensions**: `provider_model` (default — `provider_id:model_id` key, `provider_id / model_id` label), `provider`, `model` (uses `COALESCE(original_model_id, model_id)` so deprecated-model relinking still appears under the original id), `account`
- **Top-N + Other**: rows outside the top-N (`limit`, clamped 1..25) fold into a single `__other__` series with `is_other: true`. The fold is Python-side after the raw grouped SQL query (the SQL group-by keys are stable; folding by a Python `set` of top keys is easier to test and avoids SQL alias fragility). `bucket_totals` includes the `Other` rows so the bucket total stays equal to the sum of all points
- **Metric dimension**: in the first pass, the backend always ranks series by `request_count`. The frontend hides latency/TTFT from the metric dropdown and renders them in tooltips / detail rows only (averages are not additive and would mislead stacked bars). The `metric` field is preserved in the response for API stability
- **Caching**: `StatsService.get_grouped_timeseries(...)` participates in the 30s dashboard cache (TTL + 32-entry cap, same as `get_bandwidth_timeseries`). Cache key incorporates `bucket`, `group_by`, `limit`, `account_name`, `model_id`
- **Validation**: invalid `bucket` → `"hour"`, invalid `group_by` → `"provider_model"`, `limit` → `1..25`. Unknown `account_name` returns the empty stable payload (not `None`, not an exception) so the renderer never has to special-case it
- **Renderer**: `_render_grouped_timeseries_chart()` emits the canvas + JSON data island + `data-*` attributes; `_render_grouped_timeseries_table()` emits the 17-column grouped detail table (Account column conditionally shown); `_render_timeseries_controls()` emits the period/bucket/group_by/metric/limit/account/model form
- **Legacy compatibility**: `/api/timeseries` (the old aggregate endpoint) and the overview's `_render_timeseries_chart()` (line chart) remain unchanged for backward compatibility. The overview auto-refresh now correctly re-binds the legacy line chart via `reinitTimeseriesChart()` after every `innerHTML` swap
- **Empty state**: empty database / filtered-out time window renders `<p class="empty">No requests in this window.</p>` instead of a chart, without JavaScript errors

### Tooltip System

- Pure CSS only — declared at the bottom of `src/eggpool/dashboard/static/dashboard.css`. No JavaScript listeners, no per-site CSS, no new dependencies
- Generalizable `[data-tooltip]` rule at `src/eggpool/dashboard/static/dashboard.css:396`: any element with the attribute renders a themed bubble using existing CSS custom properties (`--card-bg`, `--card-border`, `--page-text`); new tooltip sites need no additional CSS
- `aria-label` is set on every tooltip target so screen readers announce the same text sighted users see
- Every interpolated value inside `data-tooltip="..."` and `aria-label="..."` is HTML-escaped via `_html_escape(..., quote=True)` — never interpolate raw upstream or model data
- Overview auto-refresh swaps regions via `innerHTML` every 15-60s; CSS-only tooltips survive because no JS listeners exist
- Reduced-motion friendly via `@media (prefers-reduced-motion: reduce)` at `src/eggpool/dashboard/static/dashboard.css:462` (transition: none)
- Optional `[data-tooltip-pos="bottom"]` modifier (`src/eggpool/dashboard/static/dashboard.css:450`) flips the bubble below the element — not used in the first pass
- Heatmap cells in `_render_bandwidth_heatmap()` at `src/eggpool/dashboard/render.py:615` still render the SVG `<rect>` grid with a `<title>` element (native fallback + the `tests/unit/test_dashboard.py:test_renders_tooltip` contract) but the rects carry `pointer-events="none"` via `.heatmap rect { pointer-events: none; }` at `src/eggpool/dashboard/static/dashboard.css:370` so hover never reaches the SVG title
- A sibling `<div class="heatmap-overlay">` (`src/eggpool/dashboard/render.py:789`, styled at `src/eggpool/dashboard/static/dashboard.css:375`) mirrors the cell grid as transparent hitboxes with `data-tooltip` and `aria-label` (date + metrics + request count). Cell color stays in the SVG `<rect>`; the overlay is `background: transparent`
- `_format_tooltip_date()` at `src/eggpool/dashboard/render.py:83` reformats `YYYY-MM-DD` into `Wed, Mar 5 2026`
- `_status_badge_tooltip()` at `src/eggpool/dashboard/render.py:61` maps status badge names (`cooldown_active`, `auth_failed`, `rate_limited`, `quota_exhausted`, `circuit_open`, ...) to human descriptions; status badges in event tables carry `data-tooltip` from the same mapping
- Topbar opt-ins: theme selector (`Switch dashboard theme`), period selector (`Select time range`), refresh `↻` button (`Reload this page`)

## Update Checker

- `UpdateChecker` is the single source of truth for "is there a newer eggpool release available?"
- Background task registered via `TaskSupervisor` under the exact name `update_checker`
- `_register_update_checker()` helper in `app.py` creates the checker, stores it on `app.state.update_checker`, and registers it with the supervisor
- Default check interval is 24h; PyPI request timeout is 15s
- `UpdateInfo` is a frozen dataclass that holds the snapshot; `snapshot()` returns an isolated copy via `dataclasses.replace` so callers cannot mutate the cached state
- `async_check_for_update()` is the shared one-shot helper used by both the background periodic task and the `eggpool update` CLI — both paths MUST go through this helper instead of inlining their own PyPI lookup
- `GET /api/stats/update` returns the JSON snapshot; always auth-gated regardless of `dashboard.public`
- Dashboard footer renders the update indicator only when `update_available=True`; renders nothing otherwise
- PyPI failures are non-fatal and reflected in `last_check_error`; the checker preserves the previous `latest_version` on failure so the indicator still surfaces a known-newer release during momentary outages
- The checker never auto-installs; it is passive notification only

## Model Context Limits

- `ModelLimitOverrideConfig` provides reusable limit fields (context, input, output, enforcement)
- Global overrides via `[model_overrides.<model-id>]`, provider overrides via `[providers.<id>.model_overrides.<model-id>]`
- `ModelLimitResolver` resolves per-field with precedence: provider > global > upstream > unknown
- `conservative_limits()` merges provider limits for unsuffixed model exposure (minimum across providers)
- `eggpool configsetup opencode --json-only` generates OpenCode config with explicit model limits
- Effective limits are configuration-derived; no database migration needed for static overrides

## Pricing Resolution

- Resolution order: TOML override (`[pricing] model_overrides`) → upstream `/v1/models` metadata → external catalog (OpenRouter, OpenCode Zen) via the alias registry. Implemented in `src/eggpool/catalog/pricing_resolver.py` as `resolve_pricing_from_metadata()` and the `CatalogResolverPipeline` in `src/eggpool/catalog/catalog_resolvers.py`
- **`ResolvedPricing` provenance**: every resolution records `source` (`config` / `upstream` / `mixed`), `source_detail` (`operator_override` / `provider_metadata` / `openrouter` / `opencode_zen`), `source_confidence` (`exact_external_id` / `curated_alias` / `provider_metadata`), `source_model_id` (external catalog model ID), `source_provider_id` (catalog name)
- **Cost exactness values** stored on every finalized request: `derived` (every nonzero billable category priced from a trusted rate), `partial` (at least one nonzero category filled by per-category fallback so the cost stays positive), `estimated` (no trusted rates; the local heuristic priced the request), `unknown` (no token usage)
- **Per-category fallback** in `CostCalculator._fallback_microdollars_for_category()` fills missing cache rates with conservative constants ($0.30/1M cache read, $3.75/1M cache write) so partial rows over-report rather than under-report. Opt out via `[pricing] fallback = "off"`
- **Alias registry** (`src/eggpool/catalog/pricing_aliases.py`): maps upstream model IDs (`mimo-v2.5`) onto external catalog IDs (`xiaomi/mimo-v2.5`) with a `confidence` enum (`exact` / `curated_alias` / `ambiguous_skip`). Seeded idempotently at startup via `seed_default_aliases()` and consulted by `CatalogResolverPipeline` before fetching
- **External catalog configuration** lives under `[pricing.catalogs.<name>]` in `config.toml`: `enabled`, `priority`, `ttl_seconds`, `base_url`, `api_key`, `options`. Built-in implementations: `openrouter` (enabled by default), `opencode_zen` (disabled by default)
- **`eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`** is the operator escape hatch after upgrading the resolver. Implemented in `src/eggpool/cost_recompute.py`; reuses the live `CostCalculator` so the new values match what the finalizer would write today. Default `--dry-run` reports deltas only
- **Dashboard wiring**: per-row cost-exactness badge (`<span class="exactness-badge derived|partial-mix|est-major">e:N,d:N,p:N,~:N,?:N</span>`) on the Accounts and Models tables; high-spend estimated warning banner on the Accounts page when any row exceeds $10 in estimated cost

## Health and Failure Classification

- Health systems use a normalized `FailureCategory` vocabulary shared by `HealthManager` and `AccountRuntimeState`
- `models.resolution_status` is set to `'resolved'` for all persisted models with resolved protocols
- **`BackoffPolicy` (in `health/backoff.py`)** maps each `FailureCategory` to a bounded exponential schedule (base, multiplier, cap, jitter, scope). Authentication failure is terminal — handled via `disable_account`. Context-limit failures produce no backoff. Rate-limit and quota-exhausted reasons honor upstream `Retry-After` when present.
- **`account_backoffs` table** persists upstream-derived backoffs across restarts. `AccountBackoffRepository` exposes upsert, clear-on-success, list_active, and expire_old. `HealthManager` state is rehydrated from this table at startup (best-effort, never blocks boot).
- **Successful requests clear transient backoff** for the relevant `(account_id, model_id, reason)` scope via `AccountBackoffRepository.clear_success`. Local cost overruns are never persisted as backoff rows.
- **Error classification (`retry/classification.py`)**: 408→TRANSIENT, 409/422→BAD_REQUEST (do not blindly suppress accounts), 429/402→QUOTA_EXCEEDED, 5xx→TEMPORARY/TRANSIENT. Provider error bodies are inspected for quota/rate-limit terms when status codes are ambiguous, with a denylist for false positives like "too many requests in queue".
- **`UpstreamExhaustedError` vs `ModelUnavailableError`**: 503 is reserved for genuine pre-dispatch unavailability (no enabled accounts, missing credentials, all explicitly disabled, model unknown). 502 (`UpstreamExhaustedError`) is raised when every candidate account was attempted and exhausted mid-request.
- **`/api/backoffs` endpoint** exposes active backoff rows from `AccountBackoffRepository.list_active(now)` for operator visibility during incidents.

## Error Hierarchy

- `AggregatorError` — base for all aggregator errors
- `ConfigError` — invalid or missing configuration
- `DatabaseError` — database-related failures
- `UpstreamError` — base for upstream API errors (`status_code` attribute)
  - `TemporaryUpstreamError` — temporary upstream errors (502, 503, 504)
  - `TransientUpstreamError` — transient upstream errors (retries may succeed)
  - `AuthenticationError` — upstream rejects credentials
  - `QuotaExhaustedError` — upstream account quota exhausted
  - `RateLimitError` — upstream rate-limited (`retry_after` attribute)
  - `ModelUnavailableError` — model not available upstream
- `ProxyError` — general proxy/transport errors
- `ModelNotFoundError` — requested model does not exist (`model_id` attribute)
- `NoEligibleAccountError` — no account can serve the request (503)
- `CatalogUnavailableError` — model catalog not available (503)
- `AuthenticationUnavailableError` — upstream credentials cannot be loaded (503)
- `UpstreamExhaustedError` — all upstream attempts exhausted (502)
- `AccountSuspendedError` — account suspended (503)
- `RequestTooLargeError` — request body exceeds configured limit
- `ContextLimitExceededError` — estimated request context exceeds configured model limit
- Chain exceptions with `raise ... from err` or `raise ... from None`

## Security

- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default; the strengthened redactor (regex + JSON sanitization) only runs when `security.persist_redacted_error_detail = true`
- Optional persisted `error_detail` uses a strict diagnostic allowlist (`SAFE_JSON_KEYS`); arbitrary provider payload keys are dropped
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification

## Deployment

- The systemd unit intentionally omits `ExecReload`; all configuration changes require `sudo systemctl restart eggpool`
- The `scripts/check_database.py` checker opens the database read-only via `file:...?mode=ro` and refuses to mutate anything
- The `scripts/check_database.py` checker is fail-closed: it treats missing `_migrations`, empty `_migrations`, missing required tables/columns, and query errors as exit code 2 (configuration/schema error), not zero violations
- The `scripts/smoke_test.py` stream diagnostics use a rolling tail buffer to recognize SSE markers split across arbitrary transport chunks
- `scripts/verify_upstream_auth.py` is operator-only: it bypasses EggPool to confirm the configured key works directly upstream
- Pyright in CI covers `src/` AND `scripts/`; narrow type annotations with `cast` or `Any` rather than excluding a file

## CLI Commands

- `models refresh` synchronizes configured accounts via `AccountRepository.sync_from_config` before refreshing the catalog, so cached account/model relationships match normal application startup
- The CLI has a two-tier entry point: `eggpool.cli:main` is a tiny bootstrap that dispatches `croncheck` and `ensure-running` through the stdlib-only `eggpool.fastcli` fast path, then falls through to the heavy Click CLI in `eggpool.cli_full` for everything else. See **Fast-Path CLI** above
