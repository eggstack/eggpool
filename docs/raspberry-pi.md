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
