# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Catalog non-destructive refresh contract.** `ModelCatalogCache.update_from_account()` now accepts `authoritative: bool = False, allow_withdrawals: bool = False`; both flags default to `False` so a failed, empty, or partial refresh cannot silently de-pool a healthy account. Operators opt into the old "refresh is the source of truth" behavior with `ModelsConfig.catalog_withdrawal_policy` (`preserve_until_health` default, `confirmed_once`, `confirmed_twice`). Under the default policy, health is the only de-pooling mechanism. `CatalogService._fetch_and_process_account()` returns an `AccountCatalogOutcome` enum (`SUCCESS_AUTHORITATIVE`, `SUCCESS_PARTIAL`, `SUCCESS_EMPTY`, `FAILED`, `SKIPPED`) plus an `AccountCatalogUpdateResult` summary row so the cache layer can stay non-destructive while operators still get full audit trail. `_log_refresh_summary` emits a single INFO line enumerating per-outcome counts after every cycle so catalog uncertainty shows up without enabling debug logging.
- **`eggpool accounts explain --gates`** (`src/eggpool/cli_full.py`) renders the per-account gate breakdown (config, credentials, health, circuit, provider id registry/catalog match, provider-supports-protocol, model support row/availability/freshness, provider-metadata-exists, protocol_match, local_quota_gate, final_eligible) as a compact text table. Backed by `Router.explain_account_eligibility(include_gates=True)` and `Router._collect_gate_status(...)`; the breakdown is informational — the canonical decision still comes from `_classify_eligibility`.
- **Same-tier account fairness rotor**: deterministic round-robin rotation for effectively tied same-tier accounts prevents routing starvation. Configurable via `[routing] fairness_mode`, `fairness_epsilon`, and `fairness_scope`. Defaults to `round_robin` with `provider_model_protocol` scope. Fairness decisions are recorded in `routing_decisions.score_components_json` under the `fairness` key for operator diagnostics. The `fairness` payload now includes the `scope` field, and each `top_candidates` entry carries `rank_before_fairness`, `rank_after_fairness`, and `fairness_band_member` for full traceability. The `FairnessRotor` position map is capped at 4096 entries (`_ROTOR_HARD_CAP`); when reached the entire map is cleared and rotation restarts from 0.
- **Bidirectional OpenAI ↔ Anthropic protocol transcoding.** When `[transcoder] enabled = true`, requests from clients using one protocol can be forwarded to upstream accounts that speak only the other. Initial scope is text-only requests and responses, plus streaming SSE. Tool calls, vision, and extended thinking land in a follow-up release. See `docs/transcoding.md` for the full translation table.
- New `eggpool stats transcoding [--period 1d|7d|30d]` subcommand for transcoding observability.
- New "Transcoding" card on the `/runtime` dashboard page.
- Structured INFO log per transcoded request with `request_id`, protocol direction, account, and loss-warning count.
- Boot-time INFO line when `[transcoder] enabled = true` so operators see the configuration at startup.
- **`routing_decisions.score_components_json` column (migration `0035`)** carries the per-account score breakdown captured by `QuotaFairScorer` at the moment the coordinator chose the selected account. The dashboard can now answer "why account A over account B?" without rescoring from quota tables. Includes `quota_score`, `inflight_penalty`, `health_penalty`, `final_score`, `weight`, `active_request_count`, `reserved_microdollars`, per-window `cost_*` and `capacity_*` microdollar values, `tier`, `requires_transcode`, and the top 5 near-tie candidates.
- **`eggpool accounts explain --model <id> [--provider P] [--protocol P] [--scores]`** subcommand (`src/eggpool/cli_full.py`) renders a Rich table listing every registered account with its live eligibility verdict and a stable `reason_code` (`disabled`, `auth_failed`, `quota_exhausted`, `cooldown`, `rate_limited`, `circuit_open`, `wrong_provider`, `no_protocol`, `protocol_mismatch`, `no_model`, `model_stale`, `ok`). With `--scores`, eligible accounts are also scored and the output includes priority, weight, active request count, reserved microdollars, and routing score. Re-evaluated on every invocation against the live registry + catalog so operators can diagnose routing skew without restarting the service.
- **`GET /api/stats/routing/eligibility`** JSON counterpart (auth-gated via the existing stats-route dependency list, `src/eggpool/api/stats.py`) returns the same per-account verdict list as a JSON document for programmatic dashboards and alerting.

### Changed

- `RequestCoordinator` now carries `upstream_protocol` alongside `protocol` on `ProxyRequestContext`. Behaviour is identical when `[transcoder] enabled = false`.
- **`ModelCatalogCache.update_from_account()` semantic shift — now non-destructive by default.** Both `authoritative` and `allow_withdrawals` keyword arguments default to `False`, so a per-account catalog update that omits previously-known support rows no longer silently de-pools those rows. Updates that already pass `authoritative=True, allow_withdrawals=True` continue to behave as before (and are still the only way to remove support via a refresh). All in-tree callers that intentionally destroy support — the destructive `prune_unused()` cleanup step and the explicit `update_from_account(...)` calls in `tests/unit/test_catalog.py` / `tests/integration/test_catalog_unresolved_models.py` — now pass both flags explicitly so the test suite continues to assert legacy destructive behavior. See `architecture/README.md` § Catalog Refresh Semantics.
- **`RequestCoordinator._select_and_persist_attempt()` lock scope.** The runtime publication step (`Router.increment_active_request_count` + `QuotaEstimator.add_reservation`) now runs INSIDE `_select_lock` AFTER the durable transaction commits but BEFORE the lock releases. The two contexts are written as explicit nested `async with` blocks (outer `_select_lock`, inner `_db.transaction()`). Note: collapsing them back into the previous compound `async with self._select_lock, self._db.transaction():` form would NOT by itself re-introduce the stale-score race on context-exit-order grounds — Python exits context managers right-to-left, so the transaction would still commit before the lock released. The actual bug was that the runtime publication block lived INSIDE the transaction body, so active-count and reserved-cost state were published before the durable transaction committed. The explicit nested form makes it hard to accidentally place publication inside the transaction while still keeping publication under `_select_lock`. The key invariant is block placement (publication must be outside the DB transaction body but still inside `_select_lock`), not context-exit order. The compensation chain (decrement → finalize-as-cancelled → release health slot → set `client_metadata["post_commit_interrupted"]` → re-raise) is preserved and still catches `BaseException` (including `CancelledError` / `SystemExit` / `KeyboardInterrupt`, all re-raised without being swallowed).
- `RoutingScore` gains diagnostic fields (`reserved_microdollars`, `cost_5h_microdollars`, `cost_7d_microdollars`, `cost_30d_microdollars`, `capacity_5h_microdollars`, `capacity_7d_microdollars`, `capacity_30d_microdollars`, `active_request_count`) so the scorer can return enough state to populate `score_components_json` without a second pass over the quota tables.
- `RoutingDecisionTrace` gains `score_components: Mapping[str, Any] | None` plus `to_score_components_json()`; `RoutingDecisionRepository.create()` accepts an optional `score_components_json` argument (defaults to `'{}'` for backward compatibility with rows inserted by code paths that have not yet been migrated).
- **`score_components_json` payload adds per-window utilization ratios and a tie-break summary.** The diagnostic JSON now carries `util_5h`, `util_7d`, `util_30d` (None when capacity is unconfigured) plus a `tie_break` dict naming the decisive factor between the chosen account and the runner-up (`tier`, `quota`, `inflight`, `transcode`, `near_tie`, `exact_tie`, `no_runner_up`) so the dashboard can surface a concrete cause without re-scoring.
- **`eggpool accounts explain` hydrates the catalog from SQLite.** The command now opens the database, runs migrations on a fresh install, and calls `ModelCatalogCache.hydrate_from_db(db)` (a new read-only helper on the cache module) to populate the in-memory model / provider / account-support tables from `models`, `provider_model_metadata`, and `account_models` rows before classification. The previous implementation constructed an empty cache and would have reported every account as ineligible even if the catalog-service shape had been right.
- **`eggpool accounts explain` no longer imports `rich`.** The undeclared `rich` dependency was replaced with plain `click.echo` columnar output. `reason_detail` strings now embed the account name, provider id, configured protocols, requested model id, and stale-window seconds so operators can act directly on the diagnosis.
- **`eggpool accounts status` now prints `routing_priority`.** The per-line output gained a `priority=N` field derived from the account's provider, alongside `provider`, `enabled`, `weight`, and the api-key-env set state.
- **`eggpool accounts explain` runs migrations on fresh installs.** The inner `_run_explain` coroutine now calls `MigrationRunner(db).run()` before hydrating `ModelCatalogCache.hydrate_from_db(db)`, so a brand-new (unmigrated) database path no longer crashes with `sqlite3.OperationalError: no such table: models` / `provider_model_metadata` / `account_models`. With no catalog rows yet, accounts surface a `no_model` verdict instead of the SQL error. The command still performs no outbound provider refresh.

## [0.3.5] - 2026-06-27

### Changed

- **README rewrite**: condensed the README from 870 lines to a concise, user-focused overview with quick start, CLI reference, configuration summary, API endpoints, and a documentation table linking to dedicated docs. Detailed content (deployment, providers, proxy, backup, model limits, Raspberry Pi, firewall, filesystem layout, network diagnostics) now lives in `docs/` and is linked from the README where it makes sense.

## [0.3.4] - 2026-06-27

### Fixed

- **Exclusion taxonomy empty-state**: the Routing page's doughnut chart now shows `<p class="empty">No exclusion data in this period.</p>` instead of an invisible Chart.js ring when no exclusions have been recorded in the selected period. The previous behaviour rendered a zero-data doughnut whose legend was visible but the chart itself was not, producing a "key but no graph" artefact.
- **`circuit_breaker` classification**: `SUPPRESSIVE_EXCLUSION_REASONS` now includes `circuit_breaker`, the only exclusion reason the coordinator actually writes to `exclude_reasons_json`. Previously every real-world exclusion landed in the `unknown` bucket because the frozenset only contained the legacy `circuit_open` name.
- **Catalog empty-data list is now classified `SUCCESS_EMPTY`, not `SUCCESS_AUTHORITATIVE`.** `CatalogService._fetch_and_process_account()` now distinguishes between "no model list in the response payload" (the existing `SUCCESS_EMPTY` branch on `result.response == {}`) and "model list returned but zero normalizable items after filter" (a new branch that fires when `normalize_models(...) == []` with `result.error is None`). Prior to this fix a fully empty but healthy upstream response was reported as authoritative and could mask a regression in the upstream `/v1/models` payload; the per-cycle summary line in operators' logs now correctly enumerates these as `empty=N`.

### Added

- **Sticky dashboard topbar**: `header.topbar` is `position: sticky; top: 0; z-index: 5` with a subtle backdrop blur, so the page navigation stays visible while scrolling on desktop. Mobile layout is unchanged (the topnav disclosure still wraps cleanly under 480px).
- **Footer update indicator**: periodic PyPI check (default 24h interval, 15s timeout) drives a footer pill that appears only when a newer `eggpool` release is available. The pill shows the current and latest versions side-by-side and the one-liner command (`eggpool update`) in an inline-code block. Clicking the command copies it to the clipboard via the bundled `dashboard.js` (Clipboard API with `execCommand("copy")` fallback); a transient "copied!" indicator confirms success. The new `src/eggpool/update_checker.py` module is the single source of truth for PyPI lookups — both the dashboard background task and the `eggpool update` CLI share `async_check_for_update()` so the two paths cannot drift.
- **`/api/stats/update` endpoint**: auth-gated JSON snapshot of the latest `UpdateChecker` state (`current_version`, `latest_version`, `update_available`, `last_checked_at`, `last_error`). Returns an empty payload if the checker has not yet produced a snapshot. Always auth-gated regardless of `dashboard.public`.
- **Runtime dispatch overhead and load metrics**: `DispatchOverheadRecorder` (`src/eggpool/runtime_dispatch.py`) records `time.perf_counter_ns() - context.started_monotonic_ns` immediately before `client.send(...)` in both `_execute_non_streaming` and `_execute_streaming`, on every upstream attempt (retries included). Bounded `deque(maxlen=100)`, thread-safe, integer-nanosecond storage — no body, model ID, account name, auth header, or client IP ever enters the buffer. `RuntimeMetricsService.snapshot()` gains two top-level sections: `dispatch_overhead` (avg/min/max/p50/p95 over the last 100 attempts) and `load` (`os.getloadavg` 1m/5m/15m + normalized per-core; `available: false` on platforms without it). The Runtime dashboard drops the configured-thread and process-count cards in favor of `Active threads`, `Load average`, and `Dispatch overhead`; process-count anomalies surface as a warning-only panel. `eggpool runtime-status` and `docs/deployment.md` document the new metrics.

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
