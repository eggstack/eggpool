# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
