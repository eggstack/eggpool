# Deployment Guide

Two deployment modes: **personal use** (quick, current user) and
**production** (separate user, hardened). Pick the one that fits.

## Personal Use (Recommended for LAN/Raspberry Pi)

Runs under your current user with your existing config. Not intended
for public-facing deployments.

### 1. Install

```bash
pipx install eggpool
eggpool onboard
```

Or from source (no pipx):

```bash
git clone https://github.com/eggstack/eggpool.git && cd eggpool
uv sync --no-dev
uv tool install .
uv tool update-shell
export PATH="$HOME/.local/bin:$PATH"
eggpool --config config.toml onboard
```

`uv tool install .` builds a wheel from the cloned source and installs it
into an isolated venv at `~/.local/share/uv/tools/eggpool/`, then
symlinks `eggpool` into `~/.local/bin/`. This is the same end state as
`pipx install eggpool` — `eggpool` works as a bare command from any
directory once `~/.local/bin` is on your PATH.

### 2. Start on boot

The `--install` flag writes the systemd unit, enables the service,
and starts it — all in one command:

```bash
sudo eggpool deploy systemd --install
```

This generates a unit file tailored to your system (correct binary
path, config path, data directory) and sets it up automatically.

Without `--install`, the command prints copy-paste instructions
instead.

### 3. Verify

```bash
sudo systemctl status eggpool
curl http://localhost:11300/v1/healthz
```

### Other deploy commands

```bash
# Set up logrotate
sudo eggpool deploy logrotate --install

# Set up daily backup cron (user cron, ~/backups/eggpool/)
sudo eggpool deploy cron --install

# Set up everything at once
sudo eggpool deploy all --install
```

Without `--install`, each command prints the snippet and manual
instructions for you to copy-paste.

### Configuration changes

Live reload is not supported. Restart after any config change:

```bash
sudo systemctl restart eggpool
```

### Logs

```bash
sudo journalctl -u eggpool -f
```

### Alternative: cron (no systemd)

For systems without systemd, `deploy cron` sets up a user cron
entry that checks if the server is running and restarts it:

```bash
# Check every 5 minutes, restart if stopped
 eggpool croncheck || eggpool serve &
```

This is a personal-use fallback — prefer systemd when available.

---

## Production Deployment

For public-facing or multi-user deployments. Uses a dedicated
`eggpool` system user with proper file permissions and a hardened
systemd unit.

**Not recommended for personal LAN use** — the personal-use path
above is simpler and sufficient for single-user setups.

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
cd /opt
sudo git clone https://github.com/eggstack/eggpool.git
sudo chown -R root:eggpool /opt/eggpool
cd /opt/eggpool
sudo uv sync --no-dev
sudo chown -R root:eggpool /opt/eggpool
sudo chmod -R o+rX /opt/eggpool
```

### 3. Configure

```bash
sudo cp config.example.toml /etc/eggpool/config.toml
sudo cp deploy/env.example /etc/eggpool/env
sudo chown root:eggpool /etc/eggpool/config.toml /etc/eggpool/env
sudo chmod 640 /etc/eggpool/config.toml /etc/eggpool/env

sudo nano /etc/eggpool/config.toml
sudo nano /etc/eggpool/env
```

Minimal config:

```toml
[server]
host = "0.0.0.0"
port = 11300

[database]
path = "/var/lib/eggpool/usage.sqlite3"
```

### 4. Validate and start

```bash
sudo -u eggpool bash -c 'set -a; source /etc/eggpool/env; set +a; /opt/eggpool/.venv/bin/eggpool check-config --config /etc/eggpool/config.toml'
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool migrate --config /etc/eggpool/config.toml

# Install the hardened systemd unit
sudo cp deploy/eggpool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable eggpool
sudo systemctl start eggpool
sudo systemctl status eggpool
```

### 5. Logrotate

```bash
sudo cp deploy/eggpool-logrotate.conf /etc/logrotate.d/eggpool
```

### 6. Automated backup

```bash
sudo cp deploy/eggpool.service /etc/systemd/system/
# Or use the deploy command:
sudo eggpool deploy cron
```

### Filesystem layout

```
/etc/eggpool/
├── config.toml          # Configuration
└── env                  # API keys

/var/lib/eggpool/
├── usage.sqlite3        # Database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory

/opt/eggpool/
├── .venv/               # Python virtual environment
└── src/                 # Application source
```

### Systemd unit features

- `ProtectSystem=strict` — read-only system directories
- `ReadWritePaths=/var/lib/eggpool` — data directory writable
- `NoNewPrivileges`, `PrivateTmp`, `RestrictNamespaces` — hardened
- No `ExecReload` — config changes require `systemctl restart`

### Process model

`eggpool serve` runs as a single supervisor process that invokes
Granian with `workers=1`. The result is two processes under the
canonical name `eggpool`: the supervisor and the Granian worker.
Granian is launched with `process_name="eggpool"`, so both show up
as `eggpool` in `ps` / `top` / `pgrep` rather than as a generic
`python` entry. There is no multi-worker scaling — the knob operators
tune is per-worker concurrency.

The PID file is owned by the supervisor and lives at
`$XDG_RUNTIME_DIR/eggpool.pid` on Linux (falling back to
`/tmp/eggpool.pid`). The supervisor writes `os.getpid()` before
`Granian.serve()` and clears the file in a `finally` block; the
FastAPI lifespan does not touch it. `eggpool serve` also refuses to
start a second instance: it checks the PID file and, if no live PID
is recorded, probes `GET /v1/healthz` over `127.0.0.1` (the bind
address `0.0.0.0` / `::` is rewritten to a loopback address for the
probe). Either a live PID or a 200 from the probe causes the new
`serve` to exit non-zero so a stale PID file is never silently
overwritten.

The primary tuning knob is `[server].threads` (int, default `1`,
min `1`, max `64`), which sets Granian `runtime_threads` — the
number of event-loop threads in the worker. The default of one
thread is intentional; raise it on capable hardware:

```toml
[server]
threads = 4
```

`eggpool restart` delegates to `runtime.restart_server`, which calls
`runtime.send_sigterm` against the supervisor recorded in the PID
file and then `runtime.start_server` (a `subprocess.Popen` of a new
supervisor). There is no inline subprocess logic in the CLI command
itself.

---

## Troubleshooting

### Service fails to start

```bash
sudo journalctl -u eggpool --since "5 minutes ago"
# or for personal use:
sudo journalctl -u eggpool -n 50 --no-pager
```

### Database locked errors

1. Ensure only one instance: `pgrep -f eggpool`
2. Confirm WAL mode in config
3. Increase `busy_timeout_ms` (try `10000` on Pi)

### Cannot connect from LAN

1. Verify `server.host = "0.0.0.0"` in config
2. Check firewall: `ss -tlnp | grep 11300`
3. See `docs/firewall.md`

### Leaked request detection

If the proxy starts returning 503s after running successfully for
several minutes, check for leaked pending requests:

```bash
sqlite3 ~/.eggpool/usage.sqlite3 "SELECT COUNT(*) FROM requests WHERE status = 'pending';"
sqlite3 ~/.eggpool/usage.sqlite3 "SELECT COUNT(*) FROM reservations WHERE status = 'active';"
```

Non-zero counts that grow over time indicate finalization failures.
The stale-request finalizer background task (runs every 60s) should
automatically clean these up.  If it is not keeping up, check the
logs for `Stale request finalizer` messages.

### Tuning the stale-request finalizer

The finalizer uses the upstream `read_timeout_s` (default 300s) as the
pending-request threshold.  You can override this in `config.toml`:

```toml
[upstream]
read_timeout_s = 300  # seconds; also used as stale-request threshold
```

Lowering this value makes the finalizer more aggressive but increases
the risk of interrupting legitimate slow requests.  The default matches
the upstream timeout so no request that is still making progress
should be touched.

### Startup crash recovery

A process restart is a definitive boundary: any request that was still
`pending` in the previous process is marked `interrupted` and its
active reservations are released, regardless of how recently they were
created.  Check the startup log for `Crash recovery: marked N stale
requests` to confirm a clean recovery after a crash or forced
restart.  See `plans/eggpoolfix.md` for the full safety-net design.

### Deploy commands reference

| Command | Description |
|---------|-------------|
| `eggpool deploy systemd` | Print systemd unit + instructions |
| `eggpool deploy systemd --install` | Write unit, enable, start |
| `eggpool deploy logrotate` | Print logrotate config |
| `eggpool deploy logrotate --install` | Write logrotate config |
| `eggpool deploy cron` | Print backup cron entry |
| `eggpool deploy cron --install` | Write backup script + cron entry |
| `eggpool deploy all` | Print all snippets |
| `eggpool deploy all --install` | Install everything |
| `eggpool backup` | Create a timestamped backup archive |
| `eggpool recover [path]` | Restore from a backup archive (interactive if no path) |
| `eggpool uninstall` | Remove binary, config, database, and shell PATH entries |
| `eggpool croncheck` | Check if server is running (exit 0/1) |
