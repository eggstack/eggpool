# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-06-25

### Added

- **Daemon mode**: `eggpool serve --daemon` spawns a detached supervisor
  and returns the shell promptly. The child runs the normal foreground
  `serve` command (Granian supervisor + worker); `--daemon` is never
  forwarded. Flags: `--log-file PATH`, `--quiet`, `--as-root`. Default
  log destination is `~/.local/state/eggpool/eggpool.log`.
- **Fast-path CLI**: `eggpool ensure-running` and `eggpool croncheck`
  are dispatched without importing Click via `eggpool.fastcli`. Both
  modules are stdlib-only, keeping cron watchdog ticks cheap on
  Raspberry Pi-class hardware.
- **`eggpool runtime-status`**: compact terminal health summary from
  the running server (process topology, memory, background tasks,
  database health, in-flight requests). Supports `--json` for
  scripting.
- **Grouped timeseries dashboard**: stacked-bar chart on `/timeseries`
  with groupable dimensions (`provider_model`, `provider`, `model`,
  `account`), top-N + Other folding, per-bucket detail table, and
  interactive controls for period, bucket, group_by, metric, and
  limit. Backed by `/api/timeseries/grouped`.
- **Metrics dashboard**: reliability page (attempt success/retry
  breakdown, `retry_category` distribution, pending health, operational
  events), routing page (per-`(model, provider)` decision aggregates,
  account selection counts, exclusion taxonomy), traces page
  (auth-gated recent request metadata), latency phase decomposition
  (`upstream_connect_ms`, `upstream_read_ms`, `coordinator_overhead_ms`).
- **CSS tooltip system**: pure-CSS `[data-tooltip]` bubbles on heatmap
  cells, column headers, topbar controls, and status badges. No
  JavaScript; survives overview auto-refresh `innerHTML` swap.
- **Upstream-authoritative suppression**: local quota estimates are
  advisory by default (`local_quota_mode = "score_only"`). Above-capacity
  accounts stay eligible; only upstream-observed failures (429/402/5xx/auth)
  and explicit operator disablement suppress routing. Opt in to legacy
  behavior via `local_quota_mode = "hard_cap"`.
- **Runtime/ops metrics**: `/api/stats/runtime` endpoint exposes
  process topology, memory, background task state, database health,
  and in-flight request counts. `/runtime` dashboard page renders
  these metrics.
- **Attempt analytics**: per-attempt aggregates including latency
  percentiles, byte totals, retry rate, and `retry_category`
  distribution. Every `request_attempts` row carries
  `provider_id/model_id/protocol/retry_category/release_reason/bytes_received/latency_ms/streamed/is_retry_outcome`.
- **Routing analytics**: per-`(model, provider)` decision aggregates,
  account-level selection counts, and per-`(account, reason)` exclusion
  counts. Every routing decision persisted to `routing_decisions` in
  the same transaction as the `request_attempts` INSERT.
- **Operational health**: `crash_recovery`, `stale_request_finalizer`,
  and `reservation_reconcile` safety-net events recorded as
  `operational_events` rows.
- **Pricing provenance**: `source_detail` and `source_confidence`
  columns on `model_price_snapshots` for dashboard attribution.
  Migration `0031_price_snapshot_provenance.sql`.
- **Pricing alias registry**: maps upstream model IDs to external
  catalog IDs with `exact`/`curated_alias`/`ambiguous_skip` confidence.
  Migration `0030_model_pricing_aliases.sql`. Seeded idempotently at
  startup.
- **Install/deploy simplification**: `eggpool deploy systemd --install`
  personal mode auto-detects user, binary, and config paths.
  `eggpool deploy backup-cron` and `eggpool deploy all` for complete
  lifecycle management.
- `eggpool stats recompute-costs [--dry-run|--apply] [--limit N]`
  operator escape hatch for fixing inflated cost totals after resolver
  upgrades.
- `eggpool init-config` writes bundled `config.example.toml` to
  current directory or target path.

### Fixed

- 503 saturation after several minutes of streaming load. Streaming
  request finalization is now wrapped in
  `asyncio.shield(asyncio.wait_for(..., timeout=10))` so ASGI task
  cancellation cannot kill the finalizer while it holds the SQLite
  connection lock. A periodic `stale_request_finalizer` background
  task force-finalizes any request that has been `pending` longer than
  `upstream.read_timeout_s` and reconciles the in-memory active-count
  and quota-reservation caches. Startup `_crash_recovery` no longer
  time-gates its sweep — a process restart is treated as a definitive
  boundary, so every leaked pending request and every active
  reservation from the previous process is recovered on boot.
- MiMo-style cost inflation via provider-aware pricing resolution.
  The resolver now correctly handles cached-token-heavy models where
  upstream metadata reports different pricing than the external catalog.
- Stale `format_tokens` assertion after unit-scaling rewrite.
- MiniMax and GeneralCompute provider contract alignment: auth headers,
  URL composition, and static model seeding now match the documented
  contracts.

### Changed

- `eggpool serve` runs as a single supervisor process invoking Granian
  with `workers=1`. The supervisor owns the PID file; the FastAPI
  lifespan no longer touches it.
- `eggpool restart` delegates to `runtime.restart_server` instead of
  inlining subprocess logic.
- AGENTS.md trimmed to point to skills for details; CLI commands
  table expanded to cover all 35+ commands.

## [0.2.2] - 2026-06-25

### Fixed

- Catalog no longer accumulates orphans when an upstream provider
  withdraws a model. `ModelCatalogCache.update_from_account` now
  records the per-account `(model_id, provider_id)` keys it
  advertises and clears stale per-provider rows on the next
  refresh; `prune_unused()` drops entries from `_models` and
  `_account_support` that have no remaining reference, and
  `CatalogService.refresh()` calls it after every per-account
  gather. The "Skipping unresolved model during catalog
  persistence" warning now fires once per model id per process
  and is demoted to DEBUG on subsequent cycles, so a persistent
  unresolved upstream name no longer spams the log.
- A new reconciliation pass runs at the end of
  `_persist_catalog` to align the durable catalog with the live
  cache. Models that are no longer advertised by any account are
  deleted; rows with historical request or reservation history
  are relinked to a shared `__deprecated__` placeholder while
  the original id is preserved in the new
  `requests.original_model_id` and `reservations.original_model_id`
  columns. Orphan `provider_model_metadata` rows and disabled
  `account_models` rows with no request history are also
  removed. Migration `0023_deprecated_model_placeholder.sql`
  inserts the placeholder and adds the two new columns.
  Stats queries use `COALESCE(original_model_id, model_id)` so
  dashboard widgets continue to attribute historical usage to
  the real model name.

## [0.2.1] - 2026-06-24

### Fixed

- `eggpool serve` returning 500 with `TypeError: 'NoneType' object is not
  callable` on Python 3.14 / spawn-based multiprocessing start methods.
  Granian workers re-import `eggpool.cli` in a fresh interpreter, so the
  module-level `_app` set by the parent process was `None` in the worker
  and the `target_loader` returned `None` as the ASGI callback. `_app_loader`
  now rebuilds the `FastAPI` app from the config path inside each worker.
  Follow-up to the [0.1.4] module-level loader fix.

## [0.2.0] - 2026-06-24

### Added

- `eggpool backup` CLI command that bundles `config.toml`, `.env`, and the
  SQLite database (with `-wal`/`-shm`) into a timestamped `.zip` archive.
  Default location is `~/backups/eggpool/`; override with `--output-dir`.
  Honor `XDG_BACKUP_HOME` for the default.
- `eggpool recover [path]` CLI command that restores a backup archive. With
  no path, opens an interactive `TerminalMenu` selector over the default
  backup directory. Stages restored files alongside the current ones and
  rolls back on failure.
- `eggpool uninstall` CLI command that detects the install method
  (`pipx` / `uv tool` / `source` / `manual`) and removes the binary,
  active config, `.env`, database, and shell-rc entries. Supports
  `--yes`, `--keep-config`, `--keep-data`, and `--keep-path`. Prints
  instructions for manual removal of systemd, logrotate, and cron
  artifacts (these are never removed automatically).
- New `eggpool.lifecycle` module (`backup`, `uninstall`, `__init__`)
  housing the lifecycle helpers.

## [0.1.7] - 2026-06-24

### Changed

- Install script fallback (no pipx) now uses `uv tool install .` instead
  of `uv sync`, so `eggpool` works as a bare command from any directory
  after install — matching the pipx experience. Adds `uv tool update-shell`
  to persist `~/.local/bin` on PATH.
- Post-install prompt (`install_prompt.py`) uses bare `eggpool --config`
  when the command is on PATH; falls back to `uv run --directory` when
  not yet available. Prints an actionable error when neither is present.
- Install instructions (README, deployment docs) updated to reflect bare
  `eggpool --config <path>` invocation pattern.

## [0.1.6] - 2026-06-23

### Fixed

- `eggpool serve` and `eggpool check-config` now suggest running
  `eggpool onboard` or `eggpool connect` when the config file is missing,
  instead of showing a bare error.

## [0.1.5] - 2026-06-23

### Fixed

- Install script now caps Python version at 3.14. Pyo3 (used by Granian)
  does not yet support Python 3.15.

## [0.1.4] - 2026-06-23

### Fixed

- Fix `eggpool serve` crash on Linux/macOS: Granian worker processes
  failed to start due to unpicklable local closure in `target_loader`.
  Moved `_app_loader` to module level for multiprocessing compatibility.
- Install script now invokes pipx through the detected Python version
  (`python3.x -m pipx`) to avoid using the wrong interpreter when
  system Python differs from the detected version.

## [0.1.3] - 2026-06-23

### Changed

- `eggpool onboard` now creates a minimal config and generates a server
  API key on fresh installs, eliminating the need for `init-config`.
- Install script recommends `eggpool onboard` instead of `init-config`.
- `init-config` shows a helpful warning when config exists, recommending
  `eggpool onboard` for provider setup.

### Fixed

- Onboard flow now works deterministically on fresh installs without
  requiring manual config creation first.

## [0.1.2] - 2026-06-23

### Fixed

- Create minimal config when `config.toml` is missing during `eggpool
  onboard`, so fresh installs no longer fail with "Failed to update config".
- Fix `update` command misidentifying source installs as pipx (causing
  wrong upgrade method).

### Changed

- Add `--install` flag to `deploy` subcommands for automated setup.
- Rewrite deployment docs with personal-use and production sections.

## [0.1.1] - 2026-06-23

### Added

- `eggpool deploy` subcommands: `systemd`, `logrotate`, `cron`, `all`.
- Dynamic deploy snippets based on detected install paths.

## [0.1.0] - 2026-06-23

### Added

- Multi-provider aggregation across OpenAI- and Anthropic-compatible
  upstreams with quota-aware routing.
- SQLite-backed request, token, latency, error, and cost statistics.
- Multi-page HTML dashboard (overview, accounts, models, latency, pings,
  events, timeseries, bandwidth) with 50+ Halloy themes.
- CLI commands: `serve`, `check-config`, `migrate`, `onboard`,
  `connect`, `connect list`, `logout`, `accounts list`,
  `accounts status`, `models refresh`, `db vacuum`, `dashboard public`,
  `rehash`, `restart`, `stop`, `update`, `getkey`, `newkey`, `edit`,
  `configsetup opencode`, `configsetup claude-code`, `set`,
  `init-config`, and the `deploy` group (`systemd`, `logrotate`,
  `cron`, `all`).
- Operational scripts: `install.sh`, `install_prompt.py`,
  `check_database.py`, `smoke_test.py`, `verify_upstream_auth.py`.

### Notes

- See the README and `docs/deployment.md` for install, configuration,
  and deployment.
