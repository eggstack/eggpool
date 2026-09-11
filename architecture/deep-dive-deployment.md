# Deep Dive: Deployment & Operations

Back to [Overview](overview.md)

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

## Installation

### `scripts/install.sh`

One-shot installer for the current Rust wheel. It recognizes existing package
manager and standalone installations, refuses ambiguous ownership, checks the
supported OS/architecture before mutation, and preserves configuration.

## Operational tooling

The `scripts/` directory contains release, package-boundary, installer,
portability, and qualification tooling. The most relevant commands are
`qualify_quick_installer.py`, `validate_m12_retirement.py`,
`validate_m12_package_boundary.py`, `validate_release_workflow.py`,
`build_cutover_artifacts.py`, and `verify_published_release.py`.

## Systemd Integration

```ini
[Unit]
Description=EggPool
Documentation=https://github.com/eggstack/eggpool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=eggpool
Group=eggpool
WorkingDirectory=/var/lib/eggpool
ExecStart=/usr/local/bin/eggpool --config /etc/eggpool/config.toml serve
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=5
TimeoutStopSec=30
KillSignal=SIGTERM
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/eggpool /var/lib/eggpool/backups
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallFilter=@system-service
SystemCallArchitectures=native
EnvironmentFile=/etc/eggpool/env

[Install]
WantedBy=multi-user.target
```

## Configuration

### `config.toml`

Runtime configuration. Key sections:
- `[server]` — host, port, runtime threads
- `[upstream]` — default upstream settings
- `[database]` — SQLite path, WAL mode
- `[routing]` — fairness mode/epsilon/scope
- `[models]` — collapse_models, catalog withdrawal
- `[providers.<id>]` — per-provider config
- `[transcoder]` — protocol transcoding

- `[model_info]` — source enablement
- `[dashboard]` — theme, auth policy
- `[metrics]` — buffering, flush modes
- `[backup]` — automatic backup schedule
- `[security]` — header redaction and exact trusted reverse-proxy peers

### `.env`

API key storage. Never committed.

## Live Reload

`eggpool rehash` applies supported changes without restart:
- Control socket at `<runtime_dir>/eggpool.sock`, resolved by the native path helpers from `$EGGPOOL_RUNTIME_DIR`, suitable `$XDG_RUNTIME_DIR`, private state, and UID-scoped `/tmp` fallbacks. The server requires the runtime directory to be an owner-only `0o700` directory and the socket to be an owner-only `0o600` socket.
- LIVE fields: provider/account/routing families, transcoder, cache, subset of models, retention durations
- RESTART_REQUIRED: everything else
- JSON output pinned at 9 keys

## Monitoring

### Dashboard

Self-updating HTML dashboard with 50 themes:
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

- Systemd unit is byte-for-byte identical to bundled deploy artifact
- `eggpool rehash` serializes reload transactions (one at a time)
- `reload_in_progress` uses the stable reload-busy exit code
- `eggpool connect`/`logout` don't silently restart
- Daemon mode is default for `eggpool serve`
- `--verbose` for foreground mode
