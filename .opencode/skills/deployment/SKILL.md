---
name: deployment
description: Deployment and operations for the EggPool project. Use when deploying, configuring, or troubleshooting the service. Covers systemd, configuration changes, operational scripts, and production hardening.
---

# Deployment and Operations

## Production Deployment

See `docs/deployment.md` for full instructions. Quick reference:

```bash
# Validate configuration
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool check-config --config /etc/eggpool/config.toml

# Run database migrations
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool migrate --config /etc/eggpool/config.toml

# Enable and start
sudo systemctl enable eggpool
sudo systemctl start eggpool

# Check status
sudo systemctl status eggpool
```

## Configuration Changes

**All configuration changes require a full restart.** The systemd unit intentionally omits `ExecReload` so `systemctl reload eggpool` fails cleanly. This includes changes to `routing_priority`, `collapse_models`, `expose_mode`, `model_overrides`, and any other config field.

```bash
sudo systemctl restart eggpool
sudo systemctl status eggpool
sudo journalctl -u eggpool -n 100 --no-pager
```

## Operational Scripts

### Database Invariant Checker

```bash
GOROUTER_DB_PATH=/var/lib/eggpool/usage.sqlite3 \
  uv run python scripts/check_database.py
```

Exit codes:
- `0` = all invariants pass
- `1` = invariant violation
- `2` = configuration or database access error

### Deployment Smoke Test

```bash
GOROUTER_BASE_URL=http://127.0.0.1:11300 \
GOROUTER_API_KEY=... \
GOROUTER_OPENAI_MODEL=gpt-4 \
GOROUTER_ANTHROPIC_MODEL=claude-3-5-sonnet \
  uv run python scripts/smoke_test.py
```

All four environment variables are required.

### Direct Upstream Authentication Verifier

```bash
GOROUTER_UPSTREAM_BASE_URL="https://api.openai.com" \
GOROUTER_TEST_UPSTREAM_KEY=... \
GOROUTER_OPENAI_MODEL="gpt-4" \
GOROUTER_ANTHROPIC_MODEL="claude-3-5-sonnet" \
  uv run python scripts/verify_upstream_auth.py
```

Operator-only; not run in CI. Bypasses EggPool to confirm the configured key works directly upstream.

For per-provider verification with the contract rendered from `config.toml`:

```bash
uv run python scripts/verify_upstream_auth.py \
  --config config.toml \
  --provider minimax \
  --verbose
```

The verifier consumes `[providers.<id>.verify] probe_model` and `probe_protocol` when neither `--openai-model` nor `--anthropic-model` is supplied. CLI flags always win. Bearer-prefixed API keys (e.g., `Bearer sk-...`) are rejected before any network call so the operator gets an actionable error rather than a misleading upstream 401.

## Systemd Unit

- Intentionally omits `ExecReload`; all config changes require `sudo systemctl restart eggpool`
- Uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/eggpool`
- Graceful shutdown: stops accepting new connections, waits for in-flight requests (up to 30s), closes connections, exits cleanly

## Filesystem Layout

```
/etc/eggpool/
├── config.toml          # Main configuration file
└── env                  # Environment variables (API keys)

/var/lib/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

/opt/eggpool/
├── .venv/               # Python virtual environment
└── src/                 # Application source code
```

## Troubleshooting

### Service fails to start

```bash
sudo journalctl -u eggpool --since "5 minutes ago"
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool check-config --config /etc/eggpool/config.toml
```

### Database locked errors

1. Check that only one instance is running: `pgrep -f eggpool`
2. Ensure WAL mode is enabled in config
3. Increase `busy_timeout_ms` in config

### Cannot connect from other machines

1. Verify `server.host = "0.0.0.0"` in config
2. Check firewall rules (see `docs/firewall.md`)
3. Verify the port is listening: `ss -tlnp | grep 11300`

## Security

- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default; enable with `security.persist_redacted_error_detail = true`
- When enabled, persisted `error_detail` is restricted to a strict diagnostic key allowlist; arbitrary provider payload fields are dropped
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification
