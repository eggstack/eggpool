# Deployment Guide

Production deployment instructions for the OpenCode Go Aggregator.

## Prerequisites

- Linux server (Debian/Ubuntu recommended)
- Python 3.12+
- `uv` package manager
- Root or sudo access (for systemd)

## Installation

### 1. Create system user

```bash
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/gorouter gorouter
sudo mkdir -p /var/lib/gorouter /var/log/gorouter /etc/gorouter
sudo chown gorouter:gorouter /var/lib/gorouter /var/log/gorouter
sudo chown root:gorouter /etc/gorouter
sudo chmod 750 /var/lib/gorouter /var/log/gorouter
sudo chmod 755 /etc/gorouter
```

### 2. Install application

```bash
# Clone repository
cd /opt
sudo git clone https://github.com/dbowm91/gorouter.git
sudo chown -R root:gorouter /opt/gorouter

# Install dependencies
cd /opt/gorouter
sudo -u gorouter uv sync --no-dev
```

### 3. Configure

```bash
# Copy example configuration
sudo cp config.example.toml /etc/gorouter/config.toml
sudo cp deploy/env.example /etc/gorouter/env
sudo chown root:gorouter /etc/gorouter/config.toml /etc/gorouter/env
sudo chmod 640 /etc/gorouter/config.toml /etc/gorouter/env

# Edit configuration
sudo nano /etc/gorouter/config.toml

# Set API keys
sudo nano /etc/gorouter/env
```

Update `/etc/gorouter/config.toml`:

```toml
[server]
host = "0.0.0.0"  # Listen on all interfaces for LAN access
port = 8080

[database]
path = "/var/lib/gorouter/usage.sqlite3"

[dashboard]
enabled = true
```

### 4. Install systemd unit

```bash
sudo cp deploy/gorouter.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### 5. Install logrotate

```bash
sudo cp deploy/logrotate.conf /etc/logrotate.d/gorouter
```

### 6. Start service

```bash
# Validate configuration
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator check-config --config /etc/gorouter/config.toml

# Run initial migrations
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator migrate --config /etc/gorouter/config.toml

# Enable and start
sudo systemctl enable gorouter
sudo systemctl start gorouter

# Check status
sudo systemctl status gorouter
```

## Configuration Changes

GoRouter does not support live configuration reload. **All
configuration changes require a full restart.** The systemd unit
intentionally omits `ExecReload` so `systemctl reload gorouter`
fails cleanly instead of silently doing nothing.

To apply any change to `/etc/gorouter/config.toml` or
`/etc/gorouter/env`:

```bash
# Apply changes
sudo systemctl restart gorouter

# Verify the service is up
sudo systemctl status gorouter

# Inspect the most recent logs
sudo journalctl -u gorouter -n 100 --no-pager
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
sudo journalctl -u gorouter -f
```

View recent logs:

```bash
sudo journalctl -u gorouter --since "1 hour ago"
```

## Troubleshooting

### Service fails to start

```bash
# Check logs
sudo journalctl -u gorouter --since "5 minutes ago"

# Validate config
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator check-config --config /etc/gorouter/config.toml

# Check file permissions
ls -la /etc/gorouter/
ls -la /var/lib/gorouter/
```

### Database locked errors

If you see `database is locked` errors:

1. Check that only one instance is running: `pgrep -f go-aggregator`
2. Ensure WAL mode is enabled in config
3. Increase `busy_timeout_ms` in config

### Cannot connect from other machines

1. Verify `server.host = "0.0.0.0"` in config
2. Check firewall rules (see `docs/firewall.md`)
3. Verify the port is listening: `ss -tlnp | grep 8080`
