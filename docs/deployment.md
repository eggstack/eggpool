# Deployment Guide

Two deployment modes: **personal use** (quick, current user) and
**production** (separate user, hardened). Pick the one that fits.

## Personal Use (Recommended for LAN/Raspberry Pi)

Runs under your current user with your existing config. Not intended
for public-facing deployments.

### 1. One-shot install

```bash
curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash
eggpool onboard
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install
```

The installer script clones the repo (or uses an existing clone) to
`~/eggpool`, installs `uv` if missing, finds a Python 3.11+ interpreter,
and installs `eggpool` as a global command. It detects an existing
`eggpool` on PATH and refuses to silently reinstall — pass `--force`
or `--upgrade` for intentional updates. It seeds
`~/.config/eggpool/config.toml` from the example template without
overwriting an existing file and prints the resolved config path.

After install, `eggpool onboard` walks the operator through provider
connections, configuration validation, and an optional server start.
The systemd step then takes over: `eggpool deploy systemd --install`
generates a unit tailored to your system (correct binary path,
absolute config path, `User=`/`Group=` set to the invoking user) and
writes it to `/etc/systemd/system/eggpool.service`.

### 2. Manual install (alternative)

From a clone:

```bash
git clone https://github.com/eggstack/eggpool.git && cd eggpool
uv sync --no-dev
uv tool install .
uv tool update-shell
export PATH="$HOME/.local/bin:$PATH"
eggpool onboard
```

`uv tool install .` builds a wheel from the cloned source and
installs it into an isolated venv at `~/.local/share/uv/tools/eggpool/`,
then symlinks `eggpool` into `~/.local/bin/`. This is the same end
state as `pipx install eggpool` — `eggpool` works as a bare command
from any directory once `~/.local/bin` is on your PATH.

### 3. Raspberry Pi / microSD deployment

EggPool is designed to run on small single-board computers including
Raspberry Pi. The following guidance helps optimize for microSD storage
longevity.

#### Storage recommendations

- Use a **high-endurance microSD card** (64GB+ rated for continuous writes)
- Prefer a **USB SSD** for sustained multi-session workloads
- Avoid cheap microSD cards for production use — they wear out quickly
  under continuous write patterns

#### Low-wear metrics configuration

The default `balanced` mode buffers analytics writes and flushes every
30 seconds. For microSD deployments, switch to `low_wear` mode:

```toml
[metrics]
write_mode = "low_wear"
flush_interval_s = 120
max_buffered_events = 250
timeseries_bucket_s = 300
trace_sample_rate = 0.05
aggregate_only = true
```

This configuration:
- Flushes analytics every 2 minutes instead of 30 seconds
- Uses 5-minute time buckets instead of 1-minute
- Samples only 5% of detailed traces
- Skips optional detailed analytics rows

**Trade-off**: Dashboard data will be less fresh and detailed traces
will be incomplete, but database write frequency is significantly
reduced.

#### Log management

Keep logs under logrotate to prevent unbounded growth:

```bash
eggpool deploy logrotate --install
```

#### Avoid frequent vacuuming

Do not run `eggpool db vacuum` frequently on flash media. SQLite's WAL
mode handles concurrent access well without manual vacuuming. Only
vacuum when you need to reclaim space after large data deletions.

#### Backups

EggPool creates automatic daily backups by default. The `automatic_backup`
supervised task runs in-process and produces restore-compatible `.zip`
archives every 24 hours with count-based retention (default 14). Backups
use `sqlite3.Connection.backup()` for consistent snapshots and atomic
archive publication.

The default backup directory depends on the installation type:

- **Production** (`/var/lib/eggpool` exists): `/var/lib/eggpool/backups`
- **Personal**: `~/backups/eggpool/` (or `$XDG_BACKUP_HOME/eggpool`)

The production systemd unit grants write access to both `/var/lib/eggpool`
and `/var/lib/eggpool/backups`, so automatic backups work out of the box.
Override with `[backup].directory` if needed.

The `eggpool deploy backup-cron` path remains available for operators
who prefer external scheduling or want backups even when the server
process is not running:

```bash
eggpool deploy backup-cron --install
```

Note that buffered analytics may lose at most the configured flush
window (`flush_interval_s` seconds) of data after abrupt power loss.
Correctness-critical request state is always persisted immediately.

### 4. Cron fallback (no systemd)

For systems without systemd, install a personal crontab watchdog
instead of the systemd unit:

```bash
eggpool deploy cron --install
```

This writes a `@reboot` + `*/5 * * * *` block to the invoking user's
crontab (or `SUDO_USER`'s crontab under sudo). Each line calls
`eggpool ensure-running`, the stdlib-only fast-path CLI:

```
# BEGIN EggPool watchdog (managed by eggpool deploy cron)
@reboot /home/user/.local/bin/eggpool --config /home/user/.config/eggpool/config.toml ensure-running >> /home/user/.local/state/eggpool/cron.log 2>&1
*/5 * * * * /home/user/.local/bin/eggpool --config /home/user/.config/eggpool/config.toml ensure-running >> /home/user/.local/state/eggpool/cron.log 2>&1
# END EggPool watchdog
```

Absolute paths only — the watchdog does not depend on cron PATH or on
the operator's interactive shell environment. `ensure-running`
atomically checks-and-starts without ever spawning a duplicate
instance, and uses the stdlib-only fast-path CLI so the cron tick is
cheap enough to run every five minutes on Raspberry Pi-class hardware.
See `plans/lightweight-cli-watchdog.md` for the design rationale.

### 5. Manual run (debug foreground)

For development or quick trials:

```bash
eggpool serve             # foreground (Granian logs to terminal)
eggpool serve --daemon    # detached supervisor; log -> ~/.local/state/eggpool/eggpool.log
```

`serve --daemon` validates the config, refuses to start a second
instance, then spawns a detached child and returns promptly. See
[Daemon Mode](#daemon-mode) below for the full contract.

### 6. Backups (automatic + optional cron)

EggPool creates automatic daily backups by default under the
`automatic_backup` supervised task. No additional setup is required.

In production, backups are written to `/var/lib/eggpool/backups` (the
systemd unit grants write access to this directory). For personal
installs, the default is `~/backups/eggpool/`. Override with
`[backup].directory` if needed.

If you prefer external cron-based scheduling (e.g. backups when the
server is not running), install the backup cron separately:

```bash
eggpool deploy backup-cron --install
```

This writes `/usr/local/bin/eggpool-backup` (a sqlite3-based snapshot
script) and a `0 2 * * *` crontab entry. Backups land in
`~/backups/eggpool/` and retain 30 days of archives.

### 7. Verify

```bash
eggpool accounts status
eggpool croncheck
curl http://localhost:11300/v1/healthz
```

### 8. Other deploy commands

```bash
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy logrotate --install

sudo env "PATH=$PATH" "$(command -v eggpool)" deploy all --install
```

`deploy logrotate --install` writes `/etc/logrotate.d/eggpool` and
runs `logrotate -d` to validate the syntax (it no longer tries to
restart a possibly-missing `logrotate.service`). `deploy all --install`
covers systemd + logrotate + the watchdog cron; backup-cron is
intentionally separate.

Without `--install`, each command prints the snippet and manual
instructions for you to copy-paste.

### 9. Configuration changes

Live reload is not supported. Restart after any config change:

```bash
sudo systemctl restart eggpool
```

### 10. Logs

```bash
sudo journalctl -u eggpool -f
```

---

## Configuration path resolution

Every CLI command resolves `--config` against this precedence (single
source of truth: `eggpool.deploy_user.resolve_config_path()`):

1. `--config PATH` (highest)
2. `$EGGPOOL_CONFIG` environment variable
3. `~/.config/eggpool/config.toml` (XDG default for installed copies)
4. `./config.toml` (CWD fallback for source checkouts)

For the environment file:

1. `$EGGPOOL_ENV` (explicit override)
2. `<config-dir>/.env` next to the resolved config
3. `~/.config/eggpool/.env` (XDG default)

After install, drop the `--config` flag by exporting
`$EGGPOOL_CONFIG` in your shell rc. The install script prints the
exact line to add.

---

## Filesystem layout (personal)

```
~/.config/eggpool/
├── config.toml          # Main configuration file
└── .env                 # Environment variables (API keys)

~/.local/share/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

~/.local/state/eggpool/
├── eggpool.pid          # Supervisor PID file
├── eggpool.log          # Daemon log (serve --daemon default)
└── cron.log             # Watchdog cron output
```

The XDG defaults honor `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`, and
`$XDG_STATE_HOME`. The resolvers (`default_config_dir()`,
`default_data_dir()`, `default_state_dir()`, `default_config_path()`,
`default_env_path()`) live in `src/eggpool/deploy_user.py`.

---

## Filesystem layout (production)

```
/etc/eggpool/
├── config.toml          # Configuration
└── env                  # API keys

/var/lib/eggpool/
├── usage.sqlite3        # Database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

/opt/eggpool/
├── .venv/               # Python virtual environment
└── src/                 # Application source
```

`eggpool deploy systemd --install --production` automates the full
production layout (dedicated system user, directory permissions,
hardened unit) and runs migrations as the `eggpool` user. Manual
production installation is documented below.

---

## Production Deployment

For public-facing or multi-user deployments. Uses a dedicated
`eggpool` system user with proper file permissions and a hardened
systemd unit.

**Not recommended for personal LAN use** — the personal-use path
above is simpler and sufficient for single-user setups.

### Automated production install

```bash
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy systemd --install --production
```

This runs the full manual sequence below (system user, directory
permissions, config seeding, migrations, hardened unit) in one
command. Use `--install --production` together; `--production` alone
just prints the snippets.

### Manual production install

For operators who prefer to wire each step by hand:

```bash
# 1. Create system user
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/eggpool eggpool
sudo mkdir -p /var/lib/eggpool /var/log/eggpool /etc/eggpool
sudo chown eggpool:eggpool /var/lib/eggpool /var/log/eggpool
sudo chown root:eggpool /etc/eggpool
sudo chmod 750 /var/lib/eggpool /var/log/eggpool
sudo chmod 755 /etc/eggpool

# 2. Install application
cd /opt
sudo git clone https://github.com/eggstack/eggpool.git
sudo chown -R root:eggpool /opt/eggpool
cd /opt/eggpool
sudo uv sync --no-dev
sudo chown -R root:eggpool /opt/eggpool
sudo chmod -R o+rX /opt/eggpool

# 3. Configure
sudo cp config.example.toml /etc/eggpool/config.toml
sudo cp deploy/env.example /etc/eggpool/env
sudo chown root:eggpool /etc/eggpool/config.toml /etc/eggpool/env
sudo chmod 640 /etc/eggpool/config.toml /etc/eggpool/env

sudo nano /etc/eggpool/config.toml
sudo nano /etc/eggpool/env

# Minimal config:
#
# [server]
# host = "0.0.0.0"
# port = 11300
#
# [database]
# path = "/var/lib/eggpool/usage.sqlite3"

# 4. Validate and start
sudo -u eggpool bash -c 'set -a; source /etc/eggpool/env; set +a; /opt/eggpool/.venv/bin/eggpool check-config --config /etc/eggpool/config.toml'
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool migrate --config /etc/eggpool/config.toml

# 5. Install the hardened systemd unit
sudo cp deploy/eggpool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable eggpool
sudo systemctl start eggpool
sudo systemctl status eggpool

# 6. Logrotate (no longer requires systemctl restart logrotate)
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy logrotate --install

# 7. Automated backup (production /etc/cron.d/eggpool-backup)
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy backup-cron --install --production
```

### Systemd unit features

- `ProtectSystem=strict` — read-only system directories
- `ReadWritePaths=/var/lib/eggpool` — data directory writable
- `NoNewPrivileges`, `PrivateTmp`, `RestrictNamespaces` — hardened
- `User=eggpool`, `Group=eggpool` — never runs as root
- No `ExecReload` — config changes require `systemctl restart`
- `Restart=on-failure` with `RestartSec=5` and a 300s burst cap

---

## Daemon Mode

`eggpool serve --daemon` is a one-shot detach helper for personal /
SBC deployments. It validates the configuration, refuses to start a
second instance, spawns a detached child running the normal
foreground `serve` command, and returns promptly with a short
success message pointing at the log file.

The parent only validates the config and refuses to start a second
instance. The detached child runs the foreground supervisor (Granian +
worker) unchanged. The `--daemon` flag is **never** forwarded to the
child; detachment is purely a parent-side concern. The child owns
its own PID file lifecycle via `runtime.write_pid_file()` /
`runtime.clear_pid_file()`.

### Detach mechanics

- `start_new_session=True` so the child survives shell exit and signals to the parent CLI do not propagate
- `stdin=subprocess.DEVNULL` to detach from the calling terminal
- `stdout`/`stderr` redirected to a log file (or `/dev/null` when `--quiet` is set without `--log-file`)
- Default log file: `~/.local/state/eggpool/eggpool.log` (resolvable via `eggpool.runtime_paths.default_log_file()`); override with `--log-file PATH` or `$EGGPOOL_LOG_FILE`. A log file beats `/dev/null` by default because a silent background failure is hard to diagnose
- The `subprocess.Popen` handle is intentionally not awaited by the CLI parent; the parent returns as soon as the child has been spawned

### PID file resolution

PID file path resolution lives in `eggpool.runtime_paths.default_pid_file()` and is the single source of truth shared by `serve`, `serve --daemon`, `croncheck`, `ensure-running`, `stop`, `restart`, systemd, and the cron watchdog. Precedence:

1. `$EGGPOOL_PID_FILE` (if set)
2. `$XDG_RUNTIME_DIR/eggpool.pid` (if `XDG_RUNTIME_DIR` is set)
3. `~/.local/state/eggpool/eggpool.pid` (state dir auto-created)
4. `/tmp/eggpool-<UID>.pid` (UID-scoped fallback)

The `eggpool.constants.PID_FILE` constant is now a `_PIDFileProxy`
that resolves through `default_pid_file()` on every read, so the
constant inherits the same resolver for backwards compatibility
with code that imports it directly.

### Root-user guard

`serve --daemon` refuses to daemonize when the effective UID is 0
unless `--as-root` is passed. This prevents accidentally starting a
personal deployment as root; the explicit flag exists for
intentional system-wide installs. systemd production deployments
should run foreground `serve` under the systemd unit (with `User=`
set) and must not use `--daemon`.

### Process model

`eggpool serve` runs as a single supervisor process that invokes
Granian with `workers=1`. The result is two processes under the
canonical name `eggpool`: the supervisor and the Granian worker.
Granian is launched with `process_name="eggpool"`, so both show up
as `eggpool` in `ps` / `top` / `pgrep` rather than as a generic
`python` entry. There is no multi-worker scaling — the knob
operators tune is per-worker concurrency.

The supervisor owns the PID file via
`runtime.write_pid_file()` / `runtime.clear_pid_file()`. The
FastAPI lifespan does not touch the PID file. `eggpool serve` also
refuses to start a second instance: it checks the PID file and, if
no live PID is recorded, probes `GET /v1/healthz` over `127.0.0.1`
(the bind address `0.0.0.0` / `::` is rewritten to a loopback
address for the probe). Either a live PID or a 200 from the probe
causes the new `serve` to exit non-zero so a stale PID file is
never silently overwritten.

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

## Watchdog cron

```bash
eggpool deploy cron --install                # default 5-minute interval
eggpool deploy cron --install --interval 10  # 10-minute poll cadence
eggpool deploy cron --uninstall              # strip the BEGIN/END-marked block
```

The generated block uses absolute binary, config, and log paths:

```
# BEGIN EggPool watchdog (managed by eggpool deploy cron)
@reboot <binary> --config <config> ensure-running >> <log> 2>&1
*/N * * * * <binary> --config <config> ensure-running >> <log> 2>&1
# END EggPool watchdog
```

`ensure-running` is the stdlib-only fast-path CLI — it does not
import the heavy application graph on every cron tick, so a
5-minute cadence is cheap on Raspberry Pi-class hardware. The block
is bracketed by `# BEGIN EggPool watchdog` / `# END EggPool watchdog`
markers so uninstall only strips the eggpool-owned lines and leaves
unrelated cron entries untouched.

---

## Runtime diagnostics

```bash
eggpool runtime-status                 # compact terminal summary
eggpool runtime-status --json          # machine-readable JSON
```

`eggpool runtime-status` calls the local `/api/stats/runtime` endpoint
(via `urllib.request`, no heavy imports) and prints a one-page overview
of the running process.  It is intended for operators debugging
systemd, cron, or daemon deployments.

When the server is not running the command exits non-zero with a clear
message.  It does not start the server.

The output covers:

- **Server** — PID, PPID, uptime, Python version, platform, configured threads.
- **Load** — OS load average (1m, 5m, 15m) and CPU-normalized 1m when available. Returns `N/A` on platforms without `os.getloadavg`.
- **Dispatch overhead** — avg / p95 / p99 / max latency (ms) over the last 100 upstream attempts, plus sample count. Empty until the first attempt completes. Measures EggPool-local pre-dispatch work only (validation, routing, persistence, reservations) — upstream connect/TTFT/streaming/finalization are excluded.
- **Processes** — observed EggPool process count vs expected; a warning
  is printed when the observed count exceeds expected by more than one.
- **Memory** — RSS, VMS, open FD count, thread count.
- **Background tasks** — per-task running/done/cancelled state,
  iteration count, restart count, last error class and timestamp.
- **Database** — path, WAL mode, file/WAL/SHM sizes, contention
  counters (cumulative lock wait, max lock wait, write/read ops).
- **Routing** — active requests, pending count, active reservations,
  reserved microdollars, health states, active backoff rows.

All probes are best-effort; failed probes return `null` rather than
causing the command to fail. Probe diagnostics are exposed in the JSON
payload as `probe_errors`, capped at 16 entries with each message
truncated, so repeated host or permissions failures cannot produce an
unbounded response.

### Checking from cron

The watchdog cron uses `eggpool ensure-running`, which is a separate
fast-path command.  Do not use `eggpool runtime-status` in cron
entries — it imports more of the stack and is not designed for
high-frequency polling.

Backup cron is a separate command (`eggpool deploy backup-cron`) —
do not mix the two.

---

## Cleanup

```bash
eggpool uninstall --yes                       # binary, config, data, PATH
eggpool uninstall --yes --deploy-artifacts    # also systemd, logrotate, cron
```

`eggpool uninstall` detects the install method (pipx / uv tool /
source / manual), asks for confirmation, then reverses the install:
it stops any running server, removes the binary via the matching
installer (or directly, for source installs), deletes the
configuration and SQLite database, and scrubs `eggpool`-related PATH
entries from the user's shell rc files (previewed and confirmed
before any write).

Pass `--deploy-artifacts` to also remove the system-level deploy
artifacts that `eggpool deploy` created: the systemd unit, the
logrotate config, the watchdog and backup crontab blocks, and the
backup script. Files under `/etc` and `/usr/local/bin` are removed
via `sudo`. Without `--deploy-artifacts` the uninstall command
prints manual cleanup commands and leaves those files in place.

Existing backups under `~/backups/eggpool/` are always left in
place — uninstall does not touch them.

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
sqlite3 ~/.local/share/eggpool/usage.sqlite3 "SELECT COUNT(*) FROM requests WHERE status = 'pending';"
sqlite3 ~/.local/share/eggpool/usage.sqlite3 "SELECT COUNT(*) FROM reservations WHERE status = 'active';"
```

Non-zero counts that grow over time indicate finalization failures.
The stale-request finalizer background task (runs every 60s) should
automatically clean these up.  If it is not keeping up, check the
logs for `Stale request finalizer` messages.

You can also use `eggpool runtime-status` to see the pending count and
oldest pending request age without querying SQLite directly.

### Unexpected process count

`eggpool runtime-status` reports `eggpool_process_count` and
`expected_worker_process_count`.  On a standard deployment the expected
count is 2 (one Granian supervisor + one worker).

If the observed count exceeds expected:

1. Check for leftover processes from a previous crash:
   `pgrep -af eggpool`
2. Kill orphaned processes manually if confirmed stale.
3. Ensure only one `eggpool serve` or systemd unit is active.

### High RSS memory

Memory usage is visible in `eggpool runtime-status` under the Memory
section.  On Linux the snapshot reads current RSS from
`/proc/self/stat`; on macOS it falls back to `ru_maxrss` (a
high-water mark).

The following in-memory growth axes are bounded by design; see
`plans/memory.md` for the full design:

- `QuotaEstimator.account_model_ewma` and `global_model_ewma` are
  LRU-capped at `EWMA_HARD_CAP = 4096` and `GLOBAL_EWMA_HARD_CAP = 1024`
  entries respectively (hardcoded, not configurable).
- `ModelCatalogCache` deduplicates `_models` and `_provider_models`,
  and `_account_support` is a `frozenset[str]` (no per-call `.copy()`).
- `CatalogResolverPipeline.TTLCache` is bounded per catalog by
  `max_entries` (default `4096`, configurable per `[pricing.catalogs.<name>]`).
- `OutboundClientManager._per_host_requests` / `_per_host_errors`
  are capped at `MAX_TRACKED_HOSTS = 256` (coldest-total eviction;
  `evictions_total` is exposed in the manager snapshot).
- `AccountRuntimeState.model_availability` and
  `HealthManager.AccountHealth.disabled_models` are pruned at every
  `AccountRegistry.sync_accounts` / `health_disabled_models_prune`
  sweep against the currently-advertised model set.

If RSS still grows continuously after the above:

1. Check for leaked pending requests (see above).
2. Verify WAL checkpointing is working: `PRAGMA wal_checkpoint(PASSIVE)`.
3. Consider restarting the process to reclaim memory — the database
   file itself does not grow from in-memory leaks.

### WAL file growth

The WAL (`-wal`) and SHM (`-shm`) file sizes are reported by
`eggpool runtime-status` under the Database section.

A WAL file that grows without bound indicates checkpoints are not
keeping up:

1. Ensure `PRAGMA journal_mode = WAL` is active (shown in runtime
   status under `wal_mode_live`).
2. Check that `busy_timeout_ms` is sufficient (default 5000ms; raise
   to 10000ms on Raspberry Pi).
3. Run `PRAGMA wal_checkpoint(TRUNCATE)` manually to reclaim space.
4. If WAL growth is persistent, consider increasing
   `server.threads` to reduce write contention.

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

---

## Deploy commands reference

| Command | Description |
|---------|-------------|
| `eggpool deploy systemd` | Print the personal + production systemd units |
| `eggpool deploy systemd --install` | Install the personal unit (runs as the invoking user); refuses direct-root without `--as-root` |
| `eggpool deploy systemd --install --production` | Install the hardened production layout (dedicated user, `/etc/eggpool`, `/var/lib/eggpool`) |
| `eggpool deploy logrotate` | Print the logrotate config |
| `eggpool deploy logrotate --install` | Install `/etc/logrotate.d/eggpool` and validate via `logrotate -d` |
| `eggpool deploy cron` | Print the **watchdog** cron fragment (not the backup) |
| `eggpool deploy cron --install` | Install `@reboot` + `*/N * * * *` `ensure-running` block into the invoking user's crontab. `--interval N` (1-59, default 5) |
| `eggpool deploy cron --uninstall` | Strip the `# BEGIN EggPool watchdog` block from the invoking user's crontab |
| `eggpool deploy backup-cron` | Print the daily backup cron entry + script |
| `eggpool deploy backup-cron --install` | Install personal backup (user cron + `~/backups/eggpool/`) |
| `eggpool deploy backup-cron --install --production` | Install production backup (`/etc/cron.d/eggpool-backup` + `/var/backups/eggpool`) |
| `eggpool deploy all` | Print systemd + logrotate + watchdog cron snippets |
| `eggpool deploy all --install` | Install systemd + logrotate + watchdog cron (backup-cron is separate) |
| `eggpool runtime-status` | Print compact runtime health summary from the running server |
| `eggpool runtime-status --json` | Print machine-readable JSON for scripting/monitoring |
| `eggpool backup` | Create a timestamped `.zip` backup archive (default `~/backups/eggpool/`) |
| `eggpool recover [path]` | Restore from a backup archive (interactive menu if no path) |
| `eggpool uninstall` | Detect install method, preview PATH edits, remove binary + config + data + shell-rc entries |
| `eggpool uninstall --deploy-artifacts` | Also remove systemd unit, logrotate config, watchdog + backup cron blocks, backup script |
| `eggpool croncheck` | Check if server is running (exit 0/1) — fast-path, no heavy imports |
| `eggpool ensure-running` | Atomically check-and-start the server; no-op when already alive — fast-path, no heavy imports |
