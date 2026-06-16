---
name: deployment
description: Deployment and operations for the opencode-go-aggregator project. Use when deploying, configuring, or troubleshooting the service. Covers systemd, configuration changes, operational scripts, and production hardening.
---

# Deployment and Operations

## Production Deployment

See `docs/deployment.md` for full instructions. Quick reference:

```bash
# Validate configuration
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator check-config --config /etc/gorouter/config.toml

# Run database migrations
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator migrate --config /etc/gorouter/config.toml

# Enable and start
sudo systemctl enable gorouter
sudo systemctl start gorouter

# Check status
sudo systemctl status gorouter
```

## Configuration Changes

**All configuration changes require a full restart.** The systemd unit intentionally omits `ExecReload` so `systemctl reload gorouter` fails cleanly.

```bash
sudo systemctl restart gorouter
sudo systemctl status gorouter
sudo journalctl -u gorouter -n 100 --no-pager
```

## Operational Scripts

### Database Invariant Checker

```bash
GOROUTER_DB_PATH=/var/lib/gorouter/usage.sqlite3 \
  uv run python scripts/check_database.py
```

Exit codes:
- `0` = all invariants pass
- `1` = invariant violation
- `2` = configuration or database access error

### Deployment Smoke Test

```bash
GOROUTER_BASE_URL=http://127.0.0.1:8080 \
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

Operator-only; not run in CI. Bypasses GoRouter to confirm the configured key works directly upstream.

## Systemd Unit

- Intentionally omits `ExecReload`; all config changes require `sudo systemctl restart gorouter`
- Uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/gorouter`
- Graceful shutdown: stops accepting new connections, waits for in-flight requests (up to 30s), closes connections, exits cleanly

## Filesystem Layout

```
/etc/gorouter/
├── config.toml          # Main configuration file
└── env                  # Environment variables (API keys)

/var/lib/gorouter/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

/opt/gorouter/
├── .venv/               # Python virtual environment
└── src/                 # Application source code
```

## Troubleshooting

### Service fails to start

```bash
sudo journalctl -u gorouter --since "5 minutes ago"
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator check-config --config /etc/gorouter/config.toml
```

### Database locked errors

1. Check that only one instance is running: `pgrep -f go-aggregator`
2. Ensure WAL mode is enabled in config
3. Increase `busy_timeout_ms` in config

### Cannot connect from other machines

1. Verify `server.host = "0.0.0.0"` in config
2. Check firewall rules (see `docs/firewall.md`)
3. Verify the port is listening: `ss -tlnp | grep 8080`

## Security

- Local client credentials (`Authorization`, `X-Api-Key`, `Proxy-Authorization`) are stripped before upstream forwarding
- Only the selected account's bearer token is injected
- Persisted `error_detail` is fail-closed by default
- Never store API keys in SQLite
- Never log prompts, completions, or API keys
- Use constant-time comparison for API key verification
