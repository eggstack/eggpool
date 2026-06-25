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

Increase timeouts for slower SD card storage:

```toml
[database]
busy_timeout_ms = 10000

[upstream]
max_connections = 50
max_keepalive = 10

[models]
refresh_interval_s = 7200
```

## Process Model

EggPool's default process model is Pi-friendly: one `eggpool serve`
supervisor process plus one Granian worker, with a single event-loop
thread in the worker. Both processes appear as `eggpool` in `ps` /
`top` (no generic `python` entry), so the total footprint is two
processes and one thread before considering any upstream outbound
connections.

The single tuning knob for per-worker concurrency is `[server].threads`
(int, default `1`, max `64`), which maps to Granian `runtime_threads`.
The default is correct for Pi 4 / Pi 5; raise it only if your workload
genuinely needs more concurrency than a single event loop can deliver:

```toml
[server]
threads = 2
```

The PID file lives at `$XDG_RUNTIME_DIR/eggpool.pid` (or
`/tmp/eggpool.pid` if `XDG_RUNTIME_DIR` is unset) and is owned by the
supervisor. If `eggpool serve` ever exits non-zero with a message
about an existing instance, that is the duplicate-instance guard
catching a live PID or a successful `/v1/healthz` probe — check
`pgrep -f eggpool` before retrying.

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
