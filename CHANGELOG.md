# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
