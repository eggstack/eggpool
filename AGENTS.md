# AGENTS.md

## Skills

Project-specific skills are in `.opencode/skills/`:

- `architecture` — design principles, request lifecycle, invariants, error hierarchy
- `deployment` — production deployment, systemd, operational scripts
- `development` — linting, testing, pre-commit checks, code style

## Quick Start

- Package manager: **uv** (not pip). Install deps: `uv sync --extra dev`
- Entry point: `src/eggpool/cli.py` → `eggpool` console script
- Config: `config.toml` + `.env` for API keys

## Pre-commit Checks (run before every commit)

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
```

All four must pass with zero errors.

## Code Style

- Python 3.11+ with `from __future__ import annotations` in ALL files
- Type hints on all function signatures and return types
- Ruff: E, F, W, I, N, UP, B, A, SIM, TCH rules
- Pyright strict mode — covers `src/` AND `scripts/` (not tests)
- Line length: 88 chars
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)

## Testing

- pytest with `asyncio_mode = "strict"` (from `pyproject.toml`)
- respx for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/contract/`
- Provider contract tests: `uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v`

## File Organization

- Source: `src/eggpool/`
- Tests: `tests/` (mirrors src structure)
- Config: `config.example.toml`, `.env.example`
- DB schema: `src/eggpool/db/schema/`
- Scripts: `scripts/` (operational, also type-checked by pyright)
- Deployment: `deploy/`

## Multi-Provider Architecture

- Provider-suffixed model IDs: `model-id/provider-id` (e.g., `claude-sonnet-4/opencode-go`)
- `ProviderClientPool` manages per-provider `httpx.AsyncClient` with independent connection pools
- Flat `[[accounts]]` configs auto-normalize to a default `opencode-go` provider
- `parse_model_provider()` and `format_model_provider()` in `routing/provider.py`

## Provider Contracts

Key design rules that are easy to get wrong:

- `compose_provider_url()` is the single source of truth for upstream URLs — catalog fetch, non-streaming chat, and streaming chat all call it
- `build_auth_headers()` reads from `ProviderConfig.auth` — never hardcode Bearer
- `base_url` ending `/v1` + path beginning `/v1/` is rejected (duplicate version prefix)
- `auth.mode = "none"` sends no upstream auth (Ollama local)
- All outbound dispatch paths use `compose_provider_url()` so providers cannot list at one host and dispatch to another
- **`models_endpoint.method = "DISABLED"`** skips live model listing. Providers using this must declare `[[providers.<id>.static_models]]` rows; otherwise `eggpool check-config` warns and the catalog will be empty for the provider
- **`static_models` lifecycle**: rows are seeded by `CatalogService._seed_static_models` BEFORE live fetch tasks run. Static-source fields (`protocol`, `protocol_source == "static_config"`, `supports_tools`, `supports_vision`) are preserved by `ModelCatalogCache._preserve_static_fields` when live rows arrive without them
- **`eggpool check-config`** runs `_check_stale_contracts` after a successful load and emits advisory warnings for: `DISABLED` without static seeds, `DISABLED` with `require_models = true`, declared path fields ignored by protocol mismatch, duplicate `/v1` segments, `Authorization` in static headers when `auth.mode != "none"`, legacy `models_method`/`models_path` fields, and Anthropic providers using `auth.header = "Authorization"` instead of `x-api-key`. Warnings do not change the exit code

## Bearer-prefix Guard

`AppConfig.validate_account_credentials()` rejects API keys beginning with `Bearer` for `auth.mode = "bearer"` providers. EggPool prepends `Bearer ` automatically; storing `Bearer <token>` produces `Authorization: Bearer Bearer <token>` and causes 401s. Providers using `auth.mode = "raw_authorization"` are unaffected.

## Error Handling

Use the hierarchy in `errors.py`. Chain exceptions with `raise ... from err` or `raise ... from None`.

- `AggregatorError` → `ConfigError`, `DatabaseError`, `ProxyError`
- `UpstreamError` (has `status_code`) → `TemporaryUpstreamError`, `TransientUpstreamError`, `AuthenticationError`, `QuotaExhaustedError`, `RateLimitError` (has `retry_after`), `ModelUnavailableError`
- `ModelNotFoundError` (has `model_id`), `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`, `RequestTooLargeError`, `ContextLimitExceededError`

## Gotchas

- Configuration changes require a service restart; live reload is intentionally not supported
- No CI workflows or pre-commit hooks are configured in this repo
- `Database.vacuum()` is the only sanctioned path for `VACUUM` in production code
- Every DML write must run inside `async with db.transaction():`
- SQLite transactions are serialized across concurrent tasks via a single connection lock + ContextVar
- Requests must be persisted before upstream dispatch; pre-body failures can retry, but no retry after first downstream byte
- **Upstream-authoritative suppression**: local quota estimates are advisory by default (`local_quota_mode = "score_only"`). Above-capacity accounts stay eligible; only upstream-observed failures (429/402/5xx/auth) and explicit operator disablement suppress routing. Switch to `hard_cap` only as an opt-in escape hatch.
- **Backoff persistence**: upstream-derived backoffs survive restarts via the `account_backoffs` table (`src/eggpool/db/schema/0024_account_backoffs.sql`). Hydration runs at startup after account sync, best-effort (never blocks boot). Local cost overruns must never be persisted as backoff rows.
- **Synthetic 503 vs 502**: `ModelUnavailableError` (503) is reserved for genuine pre-dispatch unavailability. `UpstreamExhaustedError` (502) is raised when every candidate account was attempted and exhausted mid-request. Single-account upstream errors pass through to the client rather than becoming synthetic 503s.
- **Streaming finalizer shielding**: streaming `_build_stream_generator` finalization runs under `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task cancellation cannot kill the finalizer while it holds the DB lock. Leaks that escape this path are caught by the periodic `stale_request_finalizer` background task (`app._finalize_stale_requests`, runs every 60s) which force-finalizes any request that has been `pending` longer than `upstream.read_timeout_s`.
- **Startup crash recovery**: `_crash_recovery` runs at every startup and recovers ALL pending requests and ALL active reservations with no time threshold. A process restart is a definitive boundary, so leaked state from the previous process is unconditionally cleaned up. If `Crash recovery: marked N stale requests` appears in logs, the safety net caught leaks from the previous run.
- **Pricing resolution pipeline**: prices flow TOML override → upstream metadata → external catalog (OpenRouter / OpenCode Zen via the alias registry). `ResolvedPricing` records `source_detail` (`operator_override` / `provider_metadata` / `openrouter` / `opencode_zen`) and `source_confidence` (`exact_external_id` / `curated_alias` / `provider_metadata`). Cost rows are then labelled `derived` (every category trusted), `partial` (some categories filled by per-category fallback), `estimated` (no trusted rates), or `unknown` (no token usage). `partial_count` is a new exactness value exposed via `/api/stats/summary`, `/api/stats/accounts`, and `/api/stats/models`. The dashboard renders a per-row cost-exactness badge and a high-spend estimated warning banner (>$10 estimated) on the Accounts page.
- **`eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`**: walks the requests table in started_at DESC order, recomputes cost from the current price snapshots, and reports / applies the change. Default is `--dry-run`. Use after upgrading the resolver to fix inflated totals on cached-token-heavy models (e.g. MiMo 2.5). Implemented in `src/eggpool/cost_recompute.py` and reuses the live `CostCalculator` so the new values match what the finalizer would write today.
- **Migration 0030 (`model_pricing_aliases`) + 0031 (`price_snapshot_provenance`)**: 0030 introduces the alias registry that maps upstream model IDs (e.g. `mimo-v2.5`) onto external catalog IDs (e.g. `xiaomi/mimo-v2.5`) with an `exact`/`curated_alias`/`ambiguous_skip` confidence enum. 0031 adds `source_detail`, `source_confidence`, `catalog_source` columns to `model_price_snapshots` so the dashboard can attribute prices back to the resolver that produced them. Seed data lives in `seed_default_aliases()` (`src/eggpool/catalog/pricing_aliases.py`) and runs idempotently at startup via `CatalogService.attach_pricing_resolvers()`.

## Observability schema (migrations 0026–0029)

Four migrations extend the request lifecycle with structured observability. Every persistence step runs inside the same transaction as the related state mutation, so the audit trail cannot diverge from durable state.

- **0026_attempt_observability.sql** — extends `request_attempts` with `provider_id, model_id, protocol, retry_category, release_reason, bytes_received, latency_ms, streamed, is_retry_outcome`. Extends `requests` with `first_attempt_at, last_attempt_id`. `first_attempt_at` is set inside `AttemptRepository.create()` when `attempt_number == 1`; `last_attempt_id` is set inside `finalize_if_incomplete()` in the same DB transaction as the attempt terminal UPDATE. `retry_category` is populated by `_RetryableUpstreamError.retry_category` from both the non-streaming and streaming error-classification sites.
- **0027_routing_decisions.sql** — new `routing_decisions` table (`request_id, attempt_number, model_id, provider_id, protocol, selected_account_*, eligible_count, scored_count, attempted_excluded_count, top_score, top_score_account_name, exclude_reasons_json, decision_made_at`). Persisted inside the same coordinator `async with db.transaction():` block as the `request_attempts` INSERT. Routing-distribution queries use `<=` for the end-filter to avoid dropping rows at exact-second boundaries.
- **0028_operational_events.sql** — new `operational_events` table (`event_type, details_json, occurred_at`). Instrumented at: `_crash_recovery` (event_type `crash_recovery`), `_finalize_stale_requests_once` (event_type `stale_request_finalizer`), `reconcile_expired_reservations` (event_type `reservation_reconcile`). Each event is recorded inside the same transaction as the durable state mutation, so the audit trail cannot lag the rows it summarises.
- **0029_latency_phases.sql** — extends `requests` with `upstream_connect_ms, upstream_read_ms, coordinator_overhead_ms` (all nullable for pre-migration rows). `upstream_connect_ms` is measured around `client.send()`; `upstream_read_ms = first_byte_ms - upstream_connect_ms`; `coordinator_overhead_ms = upstream_latency_ms - connect - read` (clamped at 0). All three phases are surfaced via `/api/stats/latency` under the `phases` key with per-phase `avg/p50/p99/sample_count`.

## Observability API surface

| Endpoint | Purpose | Auth |
|---|---|---|
| `GET /api/stats/attempts` | Per-attempt aggregates: total attempts, retry attempts, success attempts, latency p50/p99, byte totals, retry rate. Filterable by `account`, `provider`, `model`. | dashboard default |
| `GET /api/stats/retries` | Distribution by `retry_category` (quota_exceeded, transient, auth_failure, …) | dashboard default |
| `GET /api/stats/routing` | Per-`(model, provider)` decision aggregates: count, avg eligible/scored, avg selected score, distinct selected accounts | dashboard default |
| `GET /api/stats/routing-selections` | Account-level selection counts | dashboard default |
| `GET /api/stats/routing-exclusions` | Per-`(account, reason)` exclusion counts from `exclude_reasons_json` | dashboard default |
| `GET /api/stats/operational` | `crash_recovery` / `stale_request_finalizer` / `reservation_reconcile` summary + recent | dashboard default |
| `GET /api/stats/pending-health` | Instantaneous snapshot: pending count, oldest pending age, stale pending (>15m), active reservations, reserved microdollars, oldest reservation age | **always auth-gated** |
| `GET /api/stats/latency` (extended) | Adds `phases` key: connect/read/coordinator-overhead breakdown | dashboard default |
| `GET /api/stats/recent/{request_id}` | Parent request + full attempt chain + routing decisions. Returns account name, model, protocol, status, error class (never raw `error_detail`). | **always auth-gated** |
| `GET /api/stats/recent-requests` | Bounded recent-requests metadata list (no body, no auth headers, no error_detail, no client_ip by default). | **always auth-gated** |
| `GET /api/stats/accounts` / `/api/stats/models` (extended) | Adds `exact_count/derived_count/estimated_count/unknown_count`, `estimated_cost_fraction`, `cache_read_ratio`, `cache_write_ratio`, `reasoning_output_ratio`, `avg_cost_per_request`, `avg_cost_per_1k_tokens` | dashboard default |
| `GET /api/stats/runtime` | Runtime/operations metrics: process topology, memory, background tasks, DB health, in-flight counts. Always auth-gated. | **always auth-gated** |
| `GET /api/timeseries` | Legacy aggregate timeseries: one row per `(bucket)` with totals (kept for backward compatibility). | dashboard default |
| `GET /api/timeseries/grouped` | Grouped usage timeseries for the `/timeseries` chart. Query params: `period`, `bucket` (`hour`/`day`), `group_by` (`provider_model`/`provider`/`model`/`account`), `metric`, `limit` (1-25), `account`, `model`. Returns `{bucket, group_by, metric, limit, series, buckets, bucket_totals, points}` — series outside `limit` fold into an `Other` series so bucket totals stay loss-less. | dashboard default |

## Dashboard Pages

Server-rendered HTML pages in `src/eggpool/dashboard/render.py`, all inheriting the dashboard's public/auth setting via the `require_auth` flag wired through `register_dashboard_routes`. Frontend helpers live in `src/eggpool/dashboard/static/dashboard.js` under `window.EggPoolDashboard` (`fetchStats`, `formatDurationMs`, `formatAgeSeconds`, `formatPercent`, `formatCount`, `formatBytes`, `formatMicrodollars`, `formatTokens`, `formatDollarsFromMicro`, `initGroupedTimeseriesCharts`, `reinitTimeseriesChart`).

| Page | Route | Purpose | Chart.js |
|---|---|---|---|
| Overview | `/` | At-a-glance counters, bandwidth heatmap, request timeseries, System Health row (pending requests + reservations) | yes |
| Reliability | `/reliability` | Attempt success/retry breakdown by `(provider, model)`, `retry_category` distribution, pending-health snapshot, operational events | yes |
| Routing | `/routing` | Per-`(model, provider)` decision aggregates, account selection counts, exclusion taxonomy (suppressive vs advisory) | yes |
| Traces | `/traces` | Recent request metadata (no `error_detail`, no `client_ip`). Auth-gated via shared `require_auth` flag | no |
| Accounts | `/accounts` | Per-account stats with exactness, cache/reasoning ratios, cost-per-1k-tokens | no |
| Models | `/models` | Per-model stats with the same exactness columns | no |
| Latency | `/latency` | TTFT breakdown + connect/read/coordinator-overhead phases | yes |
| Runtime | `/runtime` | Runtime health: process count, memory, background tasks, DB sizes | no |
| Pings | `/pings` | Provider health/ping stats | no |
| Events | `/events` | Recent events | no |
| Timeseries | `/timeseries` | Stacked-bar grouped usage chart + controls (period, bucket, group_by, metric, limit) + grouped detail table | yes |
| Bandwidth | `/bandwidth` | GitHub-style heatmap of daily bytes | no |

The Overview page is the only page that auto-refreshes in place (every `[dashboard].refresh_interval_s`). All other pages are static unless the user navigates or hits refresh. Charts use the bundled Chart.js v4 served at `/static/chart.js` with `Cache-Control: public, max-age=86400`. New pages opt into Chart.js via `include_chart_js=True` in `_render_layout`.
- **Fast-path CLI imports**: fast-path commands (`croncheck`, `ensure-running`) import only `eggpool.runtime_paths` from the package; do not add transitive imports to `runtime_paths` or `fastcli` or you break the Raspberry Pi watchdog performance contract
- **Config-path resolution**: `--config PATH` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`. The resolver lives in `eggpool.deploy_user.resolve_config_path()` and is the single source of truth for every CLI command.
- **`eggpool deploy cron` is now the WATCHDOG** (not backup). It installs `eggpool ensure-running` entries (`@reboot` + `*/5 * * * *` by default) bracketed by `# BEGIN EggPool watchdog` / `# END EggPool watchdog` markers so uninstall only strips the eggpool-owned lines. Backups live under `eggpool deploy backup-cron`.
- **`eggpool deploy systemd --install`** defaults to **personal mode** (runs as the invoking user via `User=`/`Group=` in the generated unit). Use `--production` for the dedicated-system layout; use `--as-root` to permit a root-owned personal unit. Personal mode refuses direct-root without `--as-root` to prevent accidentally installing a personal unit as root.
- **Top-level lifecycle commands** (`uninstall`, `recover`) skip `ensure_config` so they work even when the config is missing or corrupted. The CLI's main entry point exempts them from the auto-config-create guard.

## Process Model

- `eggpool serve` runs as a single supervisor process that invokes `Granian` with `workers=1`; Granian spawns one worker process, so exactly **two** processes appear under the canonical name
- The Granian worker is launched with `process_name="eggpool"`, so `ps` / `top` / `pgrep` show the canonical name for both supervisor and worker (not a generic `python` entry)
- `[server].threads` (int, default `1`, min `1`, max `64`) controls Granian `runtime_threads` — the number of worker event-loop threads. Default is `1` for SBC / Raspberry Pi; raise on capable hardware
- PID path resolution lives in `eggpool.runtime_paths` and is the single source of truth (`default_pid_file()`). Precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`. The PID file is owned by the **supervisor**, written before `Granian.serve()` and cleared in a `finally` block. The FastAPI lifespan no longer touches the PID file
- `eggpool serve` refuses to start a second instance: first checks `runtime.read_pid()` + `runtime.is_process_running()`; if no live PID, probes `GET /v1/healthz` via stdlib `urllib.request` (bind `0.0.0.0` / `::` is rewritten to `127.0.0.1`). A live PID or a 200 from the probe exits `1`. Stale PID files (PID not running) are cleared before starting
- `eggpool restart` no longer has inline subprocess logic; it delegates to `runtime.restart_server` which calls `runtime.send_sigterm` and `runtime.start_server` (which `subprocess.Popen`s a new supervisor)
- `eggpool ensure-running` is the canonical cron watchdog command — it atomically checks-and-starts without ever spawning a duplicate instance. Use it from `@reboot` and `*/5 * * * *` crontab lines, not `croncheck || eggpool serve &`

### Daemon Mode

- Foreground `eggpool serve` remains the debugging/operator path and prints Granian logs to the calling terminal
- `eggpool serve --daemon` validates the config, refuses to start a second instance, then spawns a detached child and returns promptly with a short success message pointing at the log file
- The detached child runs the normal foreground `serve` command (Granian supervisor + worker). The `--daemon` flag is **never** forwarded to the child; detachment is purely a parent-side concern. The child owns its own PID file lifecycle via `runtime.write_pid_file()` / `runtime.clear_pid_file()`
- Default log destination is `~/.local/state/eggpool/eggpool.log`, resolvable via `eggpool.runtime_paths.default_log_file()`. Override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The child is launched with `start_new_session=True`, `stdin=subprocess.DEVNULL`, and `stdout`/`stderr` redirected to the log file (or `/dev/null` when `--quiet` is set without `--log-file`). The child survives shell exit and signals to the parent CLI do not propagate
- `runtime.start_server()` signature: `start_server(config_path, *, cwd=None, daemon=True, log_path=None, quiet=True, verify=False, verify_timeout_s=3.0)`. `runtime.restart_server()` accepts the same `daemon`, `log_path`, `quiet` options
- `serve --daemon` refuses to daemonize when the effective UID is 0 unless `--as-root` is passed (prevents accidental root personal deployment)
- Systemd should **not** use `--daemon`. The systemd unit already owns the process lifecycle; run foreground `serve` and let systemd manage the PID, journal logs, and restart policy

## CLI Commands

| Command | Description |
|---------|-------------|
| `eggpool help` | Show help message and available commands |
| `eggpool version` | Print the installed version |
| `eggpool serve` | Start the aggregation proxy server (default command). Flags: `--daemon`, `--log-file PATH`, `--quiet`, `--as-root` (see [Process Model / Daemon Mode](#daemon-mode)) |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup (connect providers, start server) |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart to apply config changes |
| `eggpool croncheck` | Lightweight check: exit 0 if server is running, exit 1 if not |
| `eggpool ensure-running` | Repair: start the server if it is not running; no-op when alive. Fast-path. |
| `eggpool models refresh` | Refresh model catalog from upstream |
| `eggpool stats recompute-costs [--dry-run\|--apply] [--limit N]` | Recompute historical `cost_microdollars` from current price snapshots. Default `--dry-run`. |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool runtime-status` | Print compact runtime health summary from running server |
| `eggpool db vacuum` | Reclaim SQLite space |
| `eggpool deploy systemd` | Print systemd unit; `--install` writes it (personal by default; `--production` for the dedicated-system layout; `--as-root` for a root-owned personal unit) |
| `eggpool deploy cron` | Print / install / uninstall the watchdog crontab (`@reboot` + `*/N * * * *` `eggpool ensure-running`). `--interval N` (1-59, default 5) |
| `eggpool deploy backup-cron` | Print / install / uninstall the daily backup cron (personal user cron or production `/etc/cron.d/`) |
| `eggpool deploy logrotate` | Print / install the logrotate config (validated via `logrotate -d`) |
| `eggpool deploy all` | Print / install systemd + logrotate + watchdog cron (backup-cron is separate) |
| `eggpool backup` | Create a timestamped `.zip` backup of config, `.env`, and database |
| `eggpool recover [path]` | Restore from a backup archive (interactive menu if no path) |
| `eggpool uninstall` | Remove binary, config, database, and shell PATH entries; `--deploy-artifacts` also removes the systemd unit, logrotate config, watchdog + backup cron blocks, and backup script |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`; resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`).
Running `eggpool` with no arguments prints the help message.

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__` — so `from eggpool.cli import cli` and existing test imports still work without loading the full graph
- See `plans/lightweight-cli-watchdog.md` for the full design

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
