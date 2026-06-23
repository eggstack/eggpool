# Deployment Guide

Production deployment instructions for the EggPool.

## Quick install (pipx)

The fastest way to install EggPool on a production server is via `pipx`:

```bash
# Install pipx if not available
sudo apt install pipx || pip install pipx
pipx ensurepath

# Install eggpool
pipx install eggpool

# Verify installation
eggpool --version
```

Then proceed to the Configuration section below. The bundled themes and
provider templates ship inside the package — no extra files required.

For pipx installs, use `eggpool init-config` to create a starter config:

```bash
sudo mkdir -p /etc/eggpool
sudo eggpool init-config /etc/eggpool/config.toml
```

## Prerequisites

- Linux server (Debian/Ubuntu recommended)
- Python 3.11+
- `uv` package manager (for source installs) or `pipx` (for pip installs)
- Root or sudo access (for systemd)

## Installation (source checkout)

### 1. Create system user

```bash
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/eggpool eggpool
sudo mkdir -p /var/lib/eggpool /var/log/eggpool /etc/eggpool
sudo chown eggpool:eggpool /var/lib/eggpool /var/log/eggpool
sudo chown root:eggpool /etc/eggpool
sudo chmod 750 /var/lib/eggpool /var/log/eggpool
sudo chmod 755 /etc/eggpool
```

### 2. Install application

```bash
# Clone repository
cd /opt
sudo git clone https://github.com/eggstack/eggpool.git
sudo chown -R root:eggpool /opt/eggpool

# Install dependencies (run as root so uv can write to the tree)
cd /opt/eggpool
sudo uv sync --no-dev

# Ensure the eggpool user can read and execute the environment
sudo chown -R root:eggpool /opt/eggpool
sudo chmod -R o+rX /opt/eggpool
```

### 3. Configure

```bash
# Copy example configuration
sudo cp config.example.toml /etc/eggpool/config.toml
sudo cp deploy/env.example /etc/eggpool/env
sudo chown root:eggpool /etc/eggpool/config.toml /etc/eggpool/env
sudo chmod 640 /etc/eggpool/config.toml /etc/eggpool/env

# Edit configuration
sudo nano /etc/eggpool/config.toml

# Set API keys
sudo nano /etc/eggpool/env
```

Update `/etc/eggpool/config.toml`:

```toml
[server]
host = "0.0.0.0"  # Listen on all interfaces for LAN access
port = 11300

[database]
path = "/var/lib/eggpool/usage.sqlite3"

[dashboard]
enabled = true
```

### 4. Install systemd unit

```bash
sudo cp deploy/eggpool.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### 5. Install logrotate

```bash
sudo cp deploy/eggpool-logrotate.conf /etc/logrotate.d/eggpool
```

### 6. Start service

```bash
# Validate configuration (env vars must be exported first)
sudo -u eggpool bash -c 'set -a; source /etc/eggpool/env; set +a; /opt/eggpool/.venv/bin/eggpool --config /etc/eggpool/config.toml check-config'

# Run initial migrations
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool --config /etc/eggpool/config.toml migrate

# Enable and start
sudo systemctl enable eggpool
sudo systemctl start eggpool

# Check status
sudo systemctl status eggpool
```

## Configuration Changes

EggPool does not support live configuration reload. **All
configuration changes require a full restart.** The systemd unit
intentionally omits `ExecReload` so `systemctl reload eggpool`
fails cleanly instead of silently doing nothing.

To apply any change to `/etc/eggpool/config.toml` or
`/etc/eggpool/env`:

```bash
# Apply changes
sudo systemctl restart eggpool

# Verify the service is up
sudo systemctl status eggpool

# Inspect the most recent logs
sudo journalctl -u eggpool -n 100 --no-pager
```

The `restart` workflow applies to every config change, including:

- Account list, weights, and offsets
- API keys (the env file)
- Upstream URL and timeouts
- Quota windows and routing strategy
- Log level
- Database path
- Bind address and port

## Graceful Shutdown

The service handles SIGTERM gracefully:
- Stops accepting new connections
- Waits for in-flight requests to complete (up to 30 seconds)
- Closes HTTP client connections
- Disconnects from SQLite
- Exits cleanly

## Logs

View service logs:

```bash
sudo journalctl -u eggpool -f
```

View recent logs:

```bash
sudo journalctl -u eggpool --since "1 hour ago"
```

## Troubleshooting

### Service fails to start

```bash
# Check logs
sudo journalctl -u eggpool --since "5 minutes ago"

# Validate config
sudo -u eggpool bash -c 'set -a; source /etc/eggpool/env; set +a; /opt/eggpool/.venv/bin/eggpool --config /etc/eggpool/config.toml check-config'

# Check file permissions
ls -la /etc/eggpool/
ls -la /var/lib/eggpool/
```

### Database locked errors

If you see `database is locked` errors:

1. Check that only one instance is running: `pgrep -f eggpool`
2. Ensure WAL mode is enabled in config
3. Increase `busy_timeout_ms` in config

### Cannot connect from other machines

1. Verify `server.host = "0.0.0.0"` in config
2. Check firewall rules (see `docs/firewall.md`)
3. Verify the port is listening: `ss -tlnp | grep 11300`

## Operational Scripts

### Database invariant checker

```bash
GOROUTER_DB_PATH=/var/lib/eggpool/usage.sqlite3 \
  uv run python scripts/check_database.py
```

The checker opens the database **read-only** (via a `file:...?mode=ro`
URI) so it cannot change journal mode, create WAL files, apply
migrations, or mutate the schema. It first inspects `_migrations`
and reports a clear error if the on-disk schema is older or newer
than the checker expects. The documented exit codes are:

- `0` = all invariants pass
- `1` = invariant violation (output to stderr)
- `2` = configuration or database access error (output to stderr)

### Deployment smoke test

```bash
GOROUTER_BASE_URL=http://127.0.0.1:11300 \
GOROUTER_API_KEY=... \
GOROUTER_OPENAI_MODEL=gpt-4 \
GOROUTER_ANTHROPIC_MODEL=claude-3-5-sonnet \
  uv run python scripts/smoke_test.py
```

All four environment variables are required so stale generic IDs
cannot produce misleading deployment failures. The script
exercises the dashboard endpoints, the models listing, and one
non-streaming plus one streaming call for each of the
OpenAI-compatible and Anthropic-compatible protocol families.
It uses `httpx.Client.stream()` so headers and chunks are
received in real time and validates at least one known SSE
marker per protocol. No request bodies, response bodies, or
secrets are logged or echoed.

`GOROUTER_SKIP_LIVE=1` skips the live calls (used by the unit
test harness). `GOROUTER_TEST_STREAM_CANCEL=1` closes the
response after the first nonempty chunk to exercise the client
cancellation path.

### Direct upstream authentication verifier

The bundled `scripts/verify_upstream_auth.py` script bypasses
the proxy and calls the upstream OpenAI-compatible and
Anthropic-compatible endpoints directly using
`Authorization: Bearer`. It is used to confirm that a
configured key authenticates against each endpoint family and
to distinguish upstream authentication / model compatibility
failures from EggPool-side proxy defects during live testing.
The script is **not** part of automated CI execution.

```bash
GOROUTER_UPSTREAM_BASE_URL="https://api.openai.com" \
GOROUTER_TEST_UPSTREAM_KEY=... \
GOROUTER_OPENAI_MODEL="gpt-4" \
GOROUTER_ANTHROPIC_MODEL="claude-3-5-sonnet" \
  uv run python scripts/verify_upstream_auth.py
```

Required environment:

- `GOROUTER_UPSTREAM_BASE_URL` - the upstream base URL.
- `GOROUTER_TEST_UPSTREAM_KEY` - the upstream key to verify;
  pass via environment variable, not on the command line, so
  it does not appear in shell history or process listings.
- `GOROUTER_OPENAI_MODEL` - a real OpenAI-protocol model id.
- `GOROUTER_ANTHROPIC_MODEL` - a real Anthropic-protocol model
  id.

Operational sequence:

1. Verify the key directly against each endpoint family
   (this script).
2. Run the EggPool smoke test using the same model ids.
3. If direct succeeds but the proxy fails, inspect EggPool
   header transformation and routing.
4. If both fail, treat it as upstream model or key
   compatibility rather than a proxy defect.

The model examples in the environment variables above are
illustrative; the operator must supply current, real model
IDs known to be advertised by the upstream catalog. The
verifier never enables HTTPX debug logging and never prints
the key, body, prompt, or completion. If an operator manually
exported a real key in an interactive shell, clear that
history entry (`history -d <line_number>` or
`history -c && history -w`).

### Persisted error-detail privacy

Error-detail persistence is disabled by default
(`security.persist_redacted_error_detail = false`). When
disabled, `error_detail` columns remain `NULL` and arbitrary
provider payloads never reach the database. When explicitly
enabled, EggPool stores only a bounded allowlist of sanitized
diagnostic fields. The persisted JSON is restricted to a small
diagnostic key set: `type`, `code`, `status`, `status_code`,
`error_type`, `kind`, `param`, `message`, `request_id`,
`trace_id`. Recognized sensitive and user-content keys are
retained as `[REDACTED]`. Arbitrary provider payload fields
(e.g. `payload`, `body`, `context`, `data`, `details`, `debug`)
are dropped entirely and never traversed into the output. The
returned string is bounded to 2048 characters. This is **not**
a lossless provider diagnostic; the allowlist intentionally
discards arbitrary provider detail to prevent accidental
retention of credentials, prompt content, or proprietary
request bodies.
