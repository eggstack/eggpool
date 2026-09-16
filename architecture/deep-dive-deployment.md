# Deep Dive: Deployment & Operations

Back to [Architecture](README.md)

## Purpose

Production deployment, systemd integration, operational scripts, and the tools needed to run the native EggPool executable.

## Deployment Artifacts

### `deploy/`

| File | Purpose |
|------|---------|
| `eggpool.service` | Systemd unit file |
| `eggpool-logrotate.conf` | Logrotate configuration |
| `env.example` | Production env example |

### `rust/src/operations/deploy.rs`

Rust-owned systemd/logrotate/cron snippets for CLI output. Deployment
rendering and process commands are implemented in `rust/src/operations/`.

### `rust/src/operations/lifecycle.rs`

Detached startup, safe stop/restart, independent EggPool identity proof, and
the watchdog start workflow are composed here from `process.rs`, `paths.rs`,
and `control.rs`. Restart-after-mutation is also owned here, so operations do
not depend on the CLI runtime adapter. The CLI adapter in
`rust/src/runtime.rs` retains prompts, human output, and exit-code mapping.

## Installation

### `scripts/install.sh`

One-shot installer for the current Rust wheel. It recognizes existing package
manager and standalone installations, refuses ambiguous ownership, checks the
supported OS/architecture before mutation, and preserves configuration.

## Operational tooling

The `scripts/` directory contains release, package-boundary, installer,
portability, and qualification tooling. The most relevant commands are
`qualify_quick_installer.py`, `validate_runtime_package_boundary.py`,
`validate_release_workflow.py`, `build_release_artifacts.py`,
`validate_release_artifacts.py`, and `verify_published_release.py`.

## Systemd Integration

The deployed unit lives at `deploy/eggpool.service`. CLI-rendered snippets
are owned by `rust/src/operations/deploy.rs` (`render_production_systemd` and
related renderers for personal/production layouts, logrotate, and cron). The
checked-in unit and the renderer output are related but not byte-identical
artifacts: the deployed file carries the production hardening set
(`Environment=HOME/PATH/PIPX_HOME/PIPX_BIN_DIR/EGGPOOL_LOG_FILE`,
`ProtectHome`, extended `ReadWritePaths`, `StartLimitInterval`/`StartLimitBurst`,
and the SIGHUP/reload comment), while renderers cover personal and production
variants with their own `ExecStart`/`ReadWritePaths` shapes. Do not copy a unit
from this document; read `deploy/eggpool.service` or regenerate via the CLI.

## Configuration

### `config.toml`

Runtime configuration. Selected sections (see `config.example.toml` and
`rust/src/config.rs` for the full contract):
- `[server]` — host, port, and compatibility/diagnostic settings
- `[upstream]` — default upstream settings
- `[database]` — SQLite path, WAL mode
- `[routing]` — fairness mode/epsilon/scope, plus `[routing.wire_negotiation]` and `[routing.trace]`
- `[models]` — collapse_models, catalog withdrawal
- `[providers.<id>]` — per-provider config
- `[transcoder]` — protocol transcoding
- `[limits]` — request/media/token bounds
- `[pricing]` — accounting-only pricing catalogs
- `[readiness_probe]` — readiness gating
- `[model_info]` — source enablement
- `[update_checker]` — background freshness probes
- `[dashboard]` — theme, auth policy
- `[metrics]` — buffering, flush modes
- `[security]` — header redaction and exact trusted reverse-proxy peers
- `[backup]` — automatic backup schedule
- `[model_overrides]` / `[model_capabilities]` / `[model_routers]` — per-model and semantic routing policy

### `.env`

API key storage. Never committed.

## Live Reload

`eggpool rehash` applies supported changes without restart:
- Control socket at `<runtime_dir>/eggpool.sock`, resolved by the native path helpers from `$EGGPOOL_RUNTIME_DIR`, suitable `$XDG_RUNTIME_DIR`, private state, and UID-scoped `/tmp` fallbacks. The server requires the runtime directory to be an owner-only `0o700` directory and the socket to be an owner-only `0o600` socket.
- Live vs. restart-required is owned solely by `rust/src/config_reload_policy.rs::classify_transition`. Mutation paths classify before atomic replacement and `rust/src/reload.rs` reclassifies server-side before generation publication; mixed transitions are wholly restart-required. Do not maintain a separate field list here.
- JSON output pinned at 9 keys

## Monitoring

### Dashboard

Self-updating HTML dashboard with 50 named theme files plus a built-in default (51 choices in `THEME_NAMES`):
- `/` — Overview
- `/models` — Model catalog
- `/runtime` — Live metrics
- `/cache` — Request shaping

### JSON API

Comprehensive stats endpoints under `/api/stats/`.

### CLI Diagnostics

- `eggpool accounts explain` — routing eligibility
- `eggpool modelinfo show/list/refresh` — model info
- `eggpool stats` — statistics commands
- `eggpool runtime-status` — runtime metrics

### Manual SBC characterization

Use the existing runtime snapshot after a short fixed stabilization window and
pair it with standard process/socket tools. A provider-backed run requires a
representative SBC and real configured accounts, uses synthetic non-sensitive
requests, and keeps upstream latency separate from EggPool-local timing. It is
descriptive and non-gating; unavailable dimensions are recorded as `not
measured`, with no benchmark, soak, hardware-CI, or performance-threshold
infrastructure. See [Plan 126](../plans/126-provider-backed-sbc-characterization.md)
for the completed closure record.

## Backup

Automatic backup task (zip archives):
- Config files
- Database
- Scheduled via `[backup]` config
- Disabled in the copyable low-wear SBC profile unless explicitly enabled
- Runtime snapshot/archive work uses bounded native task scheduling

## Key Invariants

- `eggpool rehash` serializes reload transactions (one at a time)
- `reload_in_progress` uses the stable reload-busy exit code
- `eggpool connect`/`logout` don't silently restart
- Daemon mode is default for `eggpool serve`
- `--verbose` for foreground mode
