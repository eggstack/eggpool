# Raspberry Pi Deployment

Run EggPool on a Raspberry Pi for always-on LAN access.

## Quick Start

```bash
# Install
pipx install eggpool

# Set up providers interactively
eggpool onboard

# Start on boot (writes systemd unit, enables, starts)
sudo eggpool deploy systemd --install

# Verify
sudo systemctl status eggpool
curl http://localhost:11300/v1/healthz
```

See [deployment.md](deployment.md) for full details on both personal
and production deployment paths.

## Requirements

- Raspberry Pi 4 (4GB+ RAM) or Pi 5
- Raspberry Pi OS (Debian-based) or Ubuntu Server
- 32GB+ microSD card (or USB SSD)
- Ethernet recommended over WiFi

## Pi Setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl build-essential
sudo apt install -y python3.11 python3.11-venv python3.11-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

Then follow the Quick Start above, or [deployment.md](deployment.md)
for the full flow.

## Pi-Specific Config

For a complete copyable low-wear configuration, use
[`config.sbc.example.toml`](../config.sbc.example.toml). It intentionally uses
a loopback bind, one database worker, disables optional diagnostics, and keeps
correctness-critical request state durable while allowing buffered analytics to
lag. Startup and `check-config` require a server API key before accepting a
non-loopback bind. If LAN access is needed, configure the key before changing
`[server].host` to `0.0.0.0` or another non-loopback address.

If you are adapting an existing configuration, increase timeouts for slower
SD card storage:

```toml
[database]
busy_timeout_ms = 10000

[upstream]
max_connections = 16
max_keepalive = 4

[models]
refresh_interval_s = 7200
```

Catalog discovery is separate from catalog rewriting: unchanged semantic
model/provider rows are not rewritten on each refresh. Successful refresh
freshness uses compact per-account state, and steady successful pings are
sampled internally while failures remain immediately durable.

## Recommended Performance Profile

The default config is tuned for Pi-class devices. The supported
single-event-loop default (`threads = 1`) uses asyncio task
concurrency for high throughput. If you need to explicitly set the
recommended profile:

```toml
[server]
threads = 1          # supported single-loop default
access_log = false   # optional: reduce I/O noise after initial setup

[database]
worker_threads = 1   # lean default; use 2 for a separate stats connection

[metrics]
write_mode = "low_wear"
flush_interval_s = 120

[routing.trace]
mode = "off"
sample_rate = 0.0
include_score_components = false
```

For minimum-footprint mode on very constrained devices:

```toml
[server]
threads = 1
access_log = false

[database]
worker_threads = 1

[routing.trace]
mode = "off"
sample_rate = 0.0
include_score_components = false
```

## Process Model

EggPool's default process model is Pi-friendly: one `eggpool serve`
supervisor process plus one Granian worker, with one required event-loop
thread in the worker. Both
processes appear as `eggpool` in `ps` / `top` (no generic `python`
entry), so the total footprint is two processes and one runtime thread before
considering any upstream outbound connections.

`[server].threads` maps to Granian `runtime_threads` and must remain `1`.
Values greater than one fail configuration validation because all
`asyncio.Lock` objects are loop-bound; multi-loop compatibility is not
supported:

```toml
[server]
threads = 1
```

The PID file path is resolved by `eggpool.runtime_paths.default_pid_file()` in this precedence: `$EGGPOOL_PID_FILE` → `$XDG_RUNTIME_DIR/eggpool.pid` → `~/.local/state/eggpool/eggpool.pid` → `/tmp/eggpool-<UID>.pid`, and is owned by the supervisor. If `eggpool serve` ever exits non-zero with a message about an existing instance, that is the duplicate-instance guard catching a live PID or a successful `/v1/healthz` probe — check `pgrep -f eggpool` before retrying.

## Reduce SD Card Wear

Log to tmpfs by adding to `/etc/fstab`:

```
tmpfs /var/log/eggpool tmpfs defaults,noatime,nosuid,mode=0750,size=50M,uid=eggpool,gid=eggpool 0 0
```

## Temperature Monitoring

```bash
vcgencmd measure_temp          # current temp
watch -n 5 vcgencmd measure_temp  # continuous
```

Thermal throttling starts at 80°C — use a heatsink or fan if sustained
loads are expected.

## Verify from LAN

1. Find Pi IP: `hostname -I`
2. Test: `curl http://<pi-ip>:11300/v1/healthz`
3. Dashboard: `http://<pi-ip>:11300/`
4. Point OpenCode at `http://<pi-ip>:11300`

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Won't start | `journalctl -u eggpool --since "5 min ago"` or `tail -50 /var/log/eggpool/eggpool.log` |
| Slow perf | CPU temp, use Ethernet, increase `busy_timeout_ms` |
| DB locked | `pgrep -f eggpool` — ensure only one instance |
| SD full | `df -h /var/lib/eggpool`, check retention config |

The startup log includes a structured operational profile line (Milestone
A6) with effective config: workers, threads, database connections, WAL
mode, routing trace settings, and background task counts. Use it to
confirm the recommended profile is active. Background task cadence
diagnostics (`last_tick_drift_s`, `configured_interval_s`) are visible
via `eggpool runtime-status --json` under each task's snapshot fields.

For a short before/after resource check, use the same database/config shape
and a fixed stabilization interval on each build, then record:

```bash
eggpool runtime-status --json
ps -o pid,rss,vsz,pcpu,etime,command -p <worker-pid>
lsof -nP -a -p <worker-pid> -iTCP -sTCP=ESTABLISHED
```

Compare at least three runs and keep upstream latency separate from the local
`local_pre_upstream` and `dispatch_overhead` fields. These checks are
descriptive and non-gating; measurements from macOS or an unconstrained
workstation must not be reported as Raspberry Pi results.
