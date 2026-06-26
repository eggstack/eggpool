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
- **Migration 0032 (`usage_rollups`)**: introduces the rollup table for buffered analytics. Counter fields are designed for additive upserts (`INSERT ... ON CONFLICT DO UPDATE SET col = col + excluded.col`). Latency min/max use `CASE/WHEN` to converge monotonically within each bucket. The `UsageRollupRepository` provides `upsert_many()`, `query_timeseries()`, `query_summary()`, and `cleanup_old_rollups()`.
- **Low-wear metrics buffering**: `MetricsWriteCoalescer` buffers lossy analytics events in memory and periodically flushes to `usage_rollups`. Correctness-critical writes (request state, reservations, routing) remain immediate. `write_mode = "balanced"` (default) uses 30s flush intervals; `write_mode = "low_wear"` uses 120s with coarser buckets. Buffered data may lose at most `flush_interval_s` seconds after abrupt power loss. The coalescer is wired into `RequestFinalizer.finalize()` and emits one `UsageMetricEvent` per terminal transition. Runtime diagnostics expose buffer health via `/api/stats/runtime`.

## Observability

- **Attempt analytics**: per-attempt aggregates including latency percentiles, byte totals, retry rate, and the `retry_category` distribution. Every `request_attempts` row carries `provider_id/model_id/protocol/retry_category/release_reason/bytes_received/latency_ms/streamed/is_retry_outcome`
- **Routing analytics**: per-`(model, provider)` decision aggregates, account-level selection counts, and per-`(account, reason)` exclusion counts. Every routing decision is persisted as a `routing_decisions` row inside the same transaction as the `request_attempts` INSERT
- **Latency phases**: decomposes each request into `upstream_connect_ms`, `upstream_read_ms`, and `coordinator_overhead_ms`
- **Operational health**: `crash_recovery`, `stale_request_finalizer`, and `reservation_reconcile` safety-net events are recorded as `operational_events` rows in the same transaction as the durable state mutation
- **Pending health**: instantaneous snapshot of pending request count, oldest pending age, stale-pending count (>15 min), active reservations, total reserved microdollars, and oldest reservation age. Auth-gated
- **Per-request trace**: parent request row, full attempt chain, and per-attempt routing decisions. Returns account name, model, protocol, status, error class (never raw error_detail), and timing. Auth-gated
- **Recent request metadata**: bounded list of recent request rows with metadata only (no body, no auth headers, no error_detail). Auth-gated
- **Cost/cache/reasoning exactness**: per-account and per-model `exact_count`, `partial_count`, `derived_count`, `estimated_count`, `cache_read_ratio`, `cache_write_ratio`, `reasoning_output_ratio`
- Full API surface is documented in the `architecture` skill

## Dashboard

- Server-rendered HTML pages in `src/eggpool/dashboard/render.py`
- Overview page auto-refreshes in place (every `[dashboard].refresh_interval_s`); all other pages are static
- Charts use bundled Chart.js v4 at `/static/chart.js` with `Cache-Control: public, max-age=86400`
- New pages opt into Chart.js via `include_chart_js=True` in `_render_layout`
- Frontend helpers in `src/eggpool/dashboard/static/dashboard.js` under `window.EggPoolDashboard`
- Full page list and chart lifecycle details are in the `architecture` skill
- **`header.topbar` is `position: sticky; top: 0; z-index: 5`** with a subtle backdrop blur so the page nav stays visible while scrolling on desktop. Mobile layout is unchanged — the topnav disclosure still wraps cleanly under 480px. Use `header.topbar` selectors when overriding; do not collapse the existing z-index above the value used by `body::before` (2) or the egg background (0)
- **Footer update indicator**: `_render_update_indicator(update_info)` renders an empty string when `update_info is None` or `update_info.update_available is False`. The indicator is appended to the footer after `<span id="dashboard-updated">ready</span>` and contains a `data-update-command` markup hook consumed by `dashboard.js` `initUpdateCommandCopy()`. The hook uses Clipboard API with `document.execCommand("copy")` fallback for older browsers and surfaces a transient "copied!" indicator on Enter/Space/click

### Update Checker

- `src/eggpool/update_checker.py` is the single source of truth for "is there a newer eggpool release available?"
- `UpdateChecker` (frozen `UpdateInfo` dataclass) holds the latest snapshot; `snapshot()` returns `dataclasses.replace(self._info)` so callers cannot mutate the cached state. Writes are serialized through an `asyncio.Lock` so the periodic background task and synchronous `snapshot()` calls from request handlers do not tear
- `async_check_for_update()` is the shared helper used by both the dashboard background task and the `eggpool update` CLI — they MUST go through it instead of inlining their own PyPI lookup so the two paths cannot drift
- Default check interval is 24h (`_DEFAULT_CHECK_INTERVAL_S = 24 * 60 * 60`), timeout 15s (`_CHECK_TIMEOUT_S = 15.0`)
- The background task is registered in `src/eggpool/app.py` via `supervisor.register("update_checker", update_checker.run_periodic)` and uses the default supervisor `max_restarts` so transient PyPI failures do not give up
- On PyPI failure, the checker preserves the previous `latest_version` so the indicator still surfaces a known-newer release during momentary outages
- `/api/stats/update` (`src/eggpool/api/update.py`) returns the JSON snapshot — always auth-gated regardless of `dashboard.public`
- Add new dashboard footer UI by extending `_UPDATE_INDICATOR_TEMPLATE` in `src/eggpool/dashboard/render.py:259`; ensure `_render_update_indicator` continues to return `""` whenever an update is not available so the footer does not show a stale or "no update" pill

### Responsive / mobile

- Dashboard targets a **320px minimum** viewport (iPhone SE / small Android). All 12 pages are mobile-equally
- Topnav wraps page links in a `<details class="topnav-hamburger">` disclosure; below 480px the `<summary>` becomes a Menu chip and the 12 links collapse into it. Theme selector and refresh button stay **outside** the disclosure so they remain reachable on every viewport
- Tables use `data-priority="N"` on every `<th>` and matching `<td>` to drive responsive column hiding: `P1` always shown, `P2` hidden below 480px, `P3` hidden below 760px. Renderers MUST use the `_th(label, *, priority=1)` and `_td_priority(content, priority, *, class_=None)` helpers in `src/eggpool/dashboard/render.py:460` — never emit a bare `<th>` for a tabular column
- Chart.js canvases MUST sit inside a `<div class="chart-wrap" style="height: …">` (not an inline-style `<div style="position: relative; height: …">`). `.chart-wrap` sets `position: relative; width: 100%` so the canvas respects its parent panel's width on every viewport
- `html { overflow-x: hidden }` is a deliberate safety net in `dashboard.css` to prevent chart-canvas resize races from causing horizontal scroll on the body. It hides real overflow bugs, so any new layout code should still design for `clientWidth == scrollWidth` even though it's masked
- New tables: pick priority mappings that put operator-glance columns in P1, diagnostic core in P2, and deep-tail columns in P3. Update `tests/unit/test_dashboard.py` `TestResponsiveColumns` with a snapshot test
- Adding a new topnav link requires updating both `_render_nav` and the breakpoints in `dashboard.css` (`@media (max-width: 480px)` and `topnav-hamburger > .topnav-links { … }`)

## Fast-Path CLI

- `src/eggpool/cli.py` is a tiny bootstrap (74 lines)
- `main()` calls `eggpool.fastcli.maybe_run_fast_command()` first; recognized fast commands (`croncheck`, `ensure-running`) are dispatched without importing Click
- **Do not add transitive imports to `runtime_paths` or `fastcli`** — they are stdlib-only and must stay lightweight for the Raspberry Pi watchdog contract
- Unrecognized commands fall through to `eggpool.cli_full`, which holds the heavy Click CLI
- Public symbols (`cli`, helpers used by tests) are lazily forwarded from `cli_full` via PEP 562 `__getattr__` — so `from eggpool.cli import cli` and existing test imports still work without loading the full graph
- See `plans/lightweight-cli-watchdog.md` for the full design

## Process Model

- `eggpool serve` runs as a single supervisor process that invokes `Granian` with `workers=1`; Granian spawns one worker process, so exactly **two** processes appear under the canonical name
- The Granian worker is launched with `process_name="eggpool"`, so `ps` / `top` / `pgrep` show the canonical name for both supervisor and worker (not a generic `python` entry)
- `[server].threads` (int, default `1`, min `1`, max `64`) controls Granian `runtime_threads` — the number of worker event-loop threads. Default is `1` for SBC / Raspberry Pi; raise on capable hardware
- PID path resolution lives in `eggpool.runtime_paths` and is the single source of truth (`default_pid_file()`). Precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`. The PID file is owned by the **supervisor**, written before `Granian.serve()` and cleared in a `finally` block. The FastAPI lifespan no longer touches the PID file
- `eggpool serve` refuses to start a second instance: first checks `runtime.read_pid()` + `runtime.is_process_running()`; if no live PID, probes `GET /v1/healthz` via stdlib `urllib.request` (bind `0.0.0.0` / `::` is rewritten to `127.0.0.1`). A live PID or a 200 from the probe exits `1`. Stale PID files (PID not running) are cleared before starting
- `eggpool restart` delegates to `runtime.restart_server` which calls `runtime.send_sigterm` and `runtime.start_server` (which `subprocess.Popen`s a new supervisor)
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
| `eggpool serve` | Start the aggregation proxy server (default command). Flags: `--daemon`, `--log-file PATH`, `--quiet`, `--as-root` |
| `eggpool check-config` | Validate the configuration file |
| `eggpool migrate` | Run database migrations |
| `eggpool onboard` | Run the interactive onboarding setup (connect providers, start server) |
| `eggpool connect` | Connect to a new provider interactively |
| `eggpool connect list` | List available providers |
| `eggpool logout` | Remove a configured provider account |
| `eggpool rehash` | Restart to apply config changes |
| `eggpool restart` | Fully restart the server (stop then start) |
| `eggpool stop` | Stop the running server |
| `eggpool set` | Set a server configuration value and restart |
| `eggpool getkey` | Print the current server API key |
| `eggpool newkey` | Generate a new server API key |
| `eggpool edit` | Open the configuration file in the default editor |
| `eggpool configsetup` | Print configuration snippets for code editors |
| `eggpool configsetup opencode` | Print OpenCode provider config JSON with model limits |
| `eggpool configsetup claude-code` | Print Claude Code config snippet |
| `eggpool update` | Check for updates and reinstall if newer |
| `eggpool croncheck` | Lightweight check: exit 0 if server is running, exit 1 if not |
| `eggpool ensure-running` | Repair: start the server if it is not running; no-op when alive. Fast-path. |
| `eggpool runtime-status` | Print compact runtime health summary from running server |
| `eggpool models refresh` | Refresh model catalog from upstream |
| `eggpool accounts status` | Show configured account status |
| `eggpool accounts list` | List configured provider accounts |
| `eggpool dashboard public` | Toggle dashboard public access |
| `eggpool db vacuum` | Reclaim SQLite space |
| `eggpool init-config` | Write bundled config.example.toml to current directory or TARGET |
| `eggpool stats recompute-costs [--dry-run\|--apply] [--limit N]` | Recompute historical `cost_microdollars` from current price snapshots. Default `--dry-run`. |
| `eggpool deploy systemd` | Print systemd unit; `--install` writes it (personal by default; `--production` for the dedicated-system layout; `--as-root` for a root-owned personal unit) |
| `eggpool deploy cron` | Print / install / uninstall the watchdog crontab (`@reboot` + `*/N * * * *` `eggpool ensure-running`). `--interval N` (1-59, default 5) |
| `eggpool deploy backup-cron` | Print / install / uninstall the daily backup cron (personal user cron or production `/etc/cron.d/`) |
| `eggpool deploy logrotate` | Print / install / logrotate config (validated via `logrotate -d`) |
| `eggpool deploy all` | Print / install systemd + logrotate + watchdog cron (backup-cron is separate) |
| `eggpool backup` | Create a timestamped `.zip` backup of config, `.env`, and database |
| `eggpool recover [path]` | Restore from a backup archive (interactive menu if no path) |
| `eggpool uninstall` | Remove binary, config, database, and shell PATH entries; `--deploy-artifacts` also removes the systemd unit, logrotate config, watchdog + backup cron blocks, and backup script |

All commands accept `--config /path/to/config.toml` (defaults to `config.toml`; resolution: `--config` > `$EGGPOOL_CONFIG` > `~/.config/eggpool/config.toml` > `./config.toml`).
Running `eggpool` with no arguments prints the help message.

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
