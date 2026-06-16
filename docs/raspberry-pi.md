# Raspberry Pi Installation Guide

Deploy the OpenCode Go Aggregator on a Raspberry Pi for always-on LAN access.

## Requirements

- Raspberry Pi 4 (4GB+ RAM recommended) or Pi 5
- Raspberry Pi OS (Debian-based) or Ubuntu Server
- 32GB+ microSD card
- Network connectivity (Ethernet recommended)

## Initial Setup

### 1. Flash the OS

```bash
# Using Raspberry Pi Imager or dd
# Enable SSH during imaging or create /boot/ssh
```

### 2. System updates

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl build-essential
```

### 3. Install Python 3.12+

```bash
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

### 4. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

## Install Application

### 1. Clone and install

```bash
sudo mkdir -p /opt/gorouter
sudo chown $USER /opt/gorouter
git clone https://github.com/dbowm91/gorouter.git /opt/gorouter

cd /opt/gorouter
uv sync --no-dev
```

### 2. Create system user

```bash
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/gorouter gorouter
sudo mkdir -p /var/lib/gorouter /var/log/gorouter /etc/gorouter
sudo chown gorouter:gorouter /var/lib/gorouter /var/log/gorouter
sudo chown root:gorouter /etc/gorouter
sudo chmod 750 /var/lib/gorouter /var/log/gorouter
sudo chmod 755 /etc/gorouter
```

### 3. Configure

```bash
sudo cp /opt/gorouter/config.example.toml /etc/gorouter/config.toml
sudo cp /opt/gorouter/deploy/env.example /etc/gorouter/env
sudo chown root:gorouter /etc/gorouter/config.toml /etc/gorouter/env
sudo chmod 640 /etc/gorouter/config.toml /etc/gorouter/env

# Edit with your settings
sudo nano /etc/gorouter/config.toml
sudo nano /etc/gorouter/env
```

Key config for Pi:

```toml
[server]
host = "0.0.0.0"
port = 8080

[database]
path = "/var/lib/gorouter/usage.sqlite3"
# Pi has slower storage; increase timeouts
busy_timeout_ms = 10000

[upstream]
# Reduce connections for limited resources
max_connections = 50
max_keepalive = 10

[models]
# Refresh less frequently to reduce load
refresh_interval_s = 7200
```

### 4. Install systemd unit

```bash
sudo cp /opt/gorouter/deploy/gorouter.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### 5. Install logrotate

```bash
sudo cp /opt/gorouter/deploy/logrotate.conf /etc/logrotate.d/gorouter
```

### 6. Run migrations and start

```bash
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator migrate --config /etc/gorouter/config.toml
sudo systemctl enable gorouter
sudo systemctl start gorouter
```

## Pi-Specific Optimizations

### Reduce SD card wear

```bash
# Add to /etc/fstab:
# tmpfs /var/log/gorouter tmpfs defaults,noatime,nosuid,mode=0750,size=50M,uid=gorouter,gid=gorouter 0 0
```

### Monitor temperature

```bash
# Check CPU temperature
vcgencmd measure_temp

# Monitor continuously
watch -n 5 vcgencmd measure_temp
```

### Limit logging

The systemd unit already includes `ProtectSystem=strict` and `ReadWritePaths=/var/lib/gorouter`. Logs go to the systemd journal by default, which is stored in RAM until rotated.

## Verifying from OpenCode

1. Find the Pi's IP address: `hostname -I`
2. Configure OpenCode to use `http://<pi-ip>:8080`
3. Set the local API key in OpenCode's configuration
4. Test: `curl http://<pi-ip>:8080/v1/healthz`
5. Open dashboard: `http://<pi-ip>:8080/`

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u gorouter --since "5 minutes ago"
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator check-config --config /etc/gorouter/config.toml
```

### Slow performance

1. Check CPU temperature (throttling at 80°C)
2. Use Ethernet instead of WiFi
3. Increase SQLite busy timeout
4. Reduce upstream connection pool size

### SD card full

```bash
# Check disk usage
df -h /var/lib/gorouter

# Trim old data (if retention is configured)
# The service handles this automatically via dashboard.retain_request_stats_days
```

## Phase 17 deployment validation

Run these checks on the target Pi before exposing it to LAN
traffic. Each one is a release-gate for the deployment-readiness
work in `plans/phase-17-deployment-readiness-corrections.md`.

### 1. Systemd unit hardening

```bash
# Unit must parse without errors.
sudo systemd-analyze verify /etc/systemd/system/gorouter.service

# Confirm ExecReload is intentionally absent: any reload attempt
# must fail with "Job type reload is not applicable".
sudo systemctl reload gorouter || true
```

### 2. Read-only database checker

```bash
sudo -u gorouter GOROUTER_DB_PATH=/var/lib/gorouter/usage.sqlite3 \
  /opt/gorouter/.venv/bin/python /opt/gorouter/scripts/check_database.py

# Exit 0 = all invariants pass.
# Exit 1 = invariant violation (read the message).
# Exit 2 = configuration or schema-version error.
echo "checker exit: $?"
```

### 3. Configuration changes require restart

```bash
# Confirm a no-op restart is fast and clean.
sudo systemctl restart gorouter
sudo systemctl status gorouter --no-pager
sudo journalctl -u gorouter -n 20 --no-pager
```

### 4. Streaming smoke test

```bash
GOROUTER_BASE_URL=http://127.0.0.1:8080 \
GOROUTER_API_KEY=$(sudo grep ^GO_AGGREGATOR_API_KEY /etc/gorouter/env | cut -d= -f2-) \
GOROUTER_OPENAI_MODEL="<your openai model>" \
GOROUTER_ANTHROPIC_MODEL="<your anthropic model>" \
  /opt/gorouter/.venv/bin/python /opt/gorouter/scripts/smoke_test.py
```

`GOROUTER_OPENAI_MODEL` and `GOROUTER_ANTHROPIC_MODEL` must be
real model IDs advertised by the upstream catalog. The script
exercises non-streaming and streaming requests for both
protocol families and reports a one-line status per check.

### 5. Restart-required workflow

Make one deliberate config change to verify the restart-only
workflow:

```bash
# Change a non-load-bearing setting, e.g. log level.
sudo sed -i 's/^log_level = "INFO"/log_level = "DEBUG"/' /etc/gorouter/config.toml
sudo systemctl restart gorouter
sudo journalctl -u gorouter -n 50 --no-pager | grep -i "log level\|debug\|info"
```

The change should be visible in the logs after the restart,
confirming the restart-only configuration workflow.

### 6. Soak test

Run the application under representative load for an extended
period to confirm there are no resource leaks, schema drift,
or credential exposure:

```bash
# A short synthetic soak (5 minutes) using respx-style mocks is
# already covered by tests/integration/test_soak.py in CI. On
# the target Pi, run a longer live soak driven by a simple
# load generator (e.g., a shell loop hitting /v1/chat/completions
# with a representative prompt) for at least 30 minutes.

# Watch for:
#   - Database file growth that matches expected traffic.
#   - No growing active-request counts (dashboard -> accounts).
#   - No 'quota exhausted' storms from a single account
#     (which would indicate cooldown regression).
#   - No secrets in the systemd journal.
sudo journalctl -u gorouter --since "30 minutes ago" | \
  grep -E "sk-[A-Za-z0-9]+|Bearer [A-Za-z0-9._-]+|api_key=" \
  && echo "FAIL: secrets in logs" || echo "OK: no secrets in logs"
```

### 7. Database invariant checker post-soak

```bash
sudo -u gorouter GOROUTER_DB_PATH=/var/lib/gorouter/usage.sqlite3 \
  /opt/gorouter/.venv/bin/python /opt/gorouter/scripts/check_database.py
echo "post-soak checker exit: $?"
```

Exit 0 means the soak did not leave the database in a
violating state.

### 8. Direct upstream authentication verification

Before diagnosing GoRouter behavior, confirm that the
configured key actually authenticates against each upstream
endpoint family. The bundled verifier bypasses the proxy and
calls the upstream endpoints directly with the same
`Authorization: Bearer` header that GoRouter emits. The
verifier is **not** part of automated CI execution.

```bash
# Set the four required variables in the current shell, then
# invoke the verifier.
GOROUTER_UPSTREAM_BASE_URL="https://api.openai.com" \
GOROUTER_TEST_UPSTREAM_KEY="$GO_AGGREGATOR_OPENCODE_KEY" \
GOROUTER_OPENAI_MODEL="<your openai model>" \
GOROUTER_ANTHROPIC_MODEL="<your anthropic model>" \
  /opt/gorouter/.venv/bin/python /opt/gorouter/scripts/verify_upstream_auth.py
```

`GOROUTER_TEST_UPSTREAM_KEY` is read from the environment; do
not pass it on the command line so it does not appear in shell
history or process listings. If the operator manually exported
the key in an interactive shell, clear that history entry:

```bash
history -d <line_number>
# or
history -c && history -w
```

Operational sequence:

1. Run the verifier against each endpoint family first. A
   non-zero exit means the upstream rejects the key or the
   model id; GoRouter cannot fix that.
2. Run `scripts/smoke_test.py` using the same model ids.
3. If direct succeeds but the proxy fails, inspect
   header transformation and routing.
4. If both fail, treat it as upstream model or key
   compatibility rather than a proxy defect.

The model examples in the environment variables above are
illustrative; the operator must supply current, real model
IDs known to be advertised by the upstream catalog.

## Acceptance checklist

Before declaring the Pi deployment ready:

- [ ] `systemctl status gorouter` shows the service as
      `active (running)`.
- [ ] `systemd-analyze verify` returns 0.
- [ ] `check_database.py` returns exit code 0.
- [ ] The smoke test reports `OK` for every check.
- [ ] The 30-minute soak leaves no secrets in the journal
      and the database invariant checker still returns 0.
- [ ] A no-op `systemctl restart gorouter` completes in
      under five seconds and the service comes back healthy.
