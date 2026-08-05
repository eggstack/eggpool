# Deep Dive: Deployment & Operations

Back to [Overview](overview.md)

## Purpose

Production deployment, systemd integration, operational scripts, and the tools needed to run EggPool in production.

## Deployment Artifacts

### `deploy/`

| File | Purpose |
|------|---------|
| `eggpool.service` | Systemd unit file |
| `eggpool-logrotate.conf` | Logrotate configuration |
| `env.example` | Production env example |

### `src/eggpool/deploy/`

Bundled systemd/logrotate/cron snippets for CLI output. `eggpool.service` is byte-for-byte identical to `eggpool.deploy.SYSTEMD_UNIT`.

## Installation

### `scripts/install.sh`

One-shot installer:
1. Clones repo
2. Installs uv
3. Installs EggPool with dev dependencies

### `scripts/install_prompt.py`

Interactive install prompt for guided setup.

## Operational Scripts

| Script | Purpose |
|--------|---------|
| `scripts/smoke_test.py` | Deployment smoke test (requires 4 env vars) |
| `scripts/check_database.py` | Database invariant checker (exit 0/1/2) |
| `scripts/verify_upstream_auth.py` | Direct upstream auth verifier |
| `scripts/validate_routing.py` | Routing validation |
| `scripts/repro_high_concurrency_streams.py` | High-concurrency stream reproducer |
| `scripts/test_model_info_identity.sh` | Model-info identity test runner |
| `scripts/debug_model_info_openrouter.sh` | OpenRouter debug helper |

## Systemd Integration

```ini
[Unit]
Description=EggPool LLM Proxy
After=network.target

[Service]
Type=simple
User=eggpool
WorkingDirectory=/home/eggpool
ExecStart=/home/eggpool/.local/bin/eggpool serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Configuration

### `config.toml`

Runtime configuration. Key sections:
- `[server]` — host, port, workers
- `[upstream]` — default upstream settings
- `[database]` — SQLite path, WAL mode
- `[routing]` — fairness mode/epsilon/scope
- `[models]` — collapse_models, catalog withdrawal
- `[providers.<id>]` — per-provider config
- `[transcoder]` — protocol transcoding
- `[compression]` — request shaping
- `[cache]` — synthetic cache controls
- `[model_info]` — source enablement
- `[dashboard]` — theme, auth policy
- `[metrics]` — buffering, flush modes
- `[backup]` — automatic backup schedule
- `[security]` — header redaction and exact trusted reverse-proxy peers

### `.env`

API key storage. Never committed.

## Live Reload

`eggpool rehash` applies supported changes without restart:
- Control socket at `~/.local/state/eggpool/eggpool.sock`
- LIVE fields: provider/account/routing families, transcoder, compression, cache, subset of models, retention durations
- RESTART_REQUIRED: everything else
- JSON output pinned at 9 keys

## Monitoring

### Dashboard

Self-updating HTML dashboard with 50+ themes:
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

## Backup

Automatic backup task (zip archives):
- Config files
- Database
- Scheduled via `[backup]` config

## Key Invariants

- Systemd unit is byte-for-byte identical to bundled deploy artifact
- `eggpool rehash` serializes reload transactions (one at a time)
- `reload_in_progress` exits with code 4 (`EXIT_RELOAD_BUSY`)
- `eggpool connect`/`logout` don't silently restart
- Daemon mode is default for `eggpool serve`
- `--verbose` for foreground mode
