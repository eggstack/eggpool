"""Bundled deployment assets for `eggpool deploy`.

These constants are the canonical source of truth for the snippets
emitted by the ``eggpool deploy`` CLI group. The matching files under
``deploy/`` at the repository root are kept byte-for-byte identical so
both source-checkout operators and wheel-installed users see the same
content. To update any snippet, edit it here AND in ``deploy/``.
"""

from __future__ import annotations

SYSTEMD_UNIT = """\
[Unit]
Description=EggPool
Documentation=https://github.com/eggstack/eggpool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=eggpool
Group=eggpool
WorkingDirectory=/var/lib/eggpool
ExecStart=/opt/eggpool/.venv/bin/eggpool --config /etc/eggpool/config.toml serve
# Live configuration reload is not supported; changes require
# `sudo systemctl restart eggpool`. SIGHUP is intentionally not
# wired to any reload action.
Restart=on-failure
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=5

# Graceful shutdown
TimeoutStopSec=30
KillSignal=SIGTERM

# Security hardening (note: MemoryDenyWriteExecute removed; PyTorch-style
# runtimes in newer Python builds may legitimately need W^X relaxations).
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/eggpool
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
RestrictRealtime=yes
LockPersonality=yes

# Network
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# System call filtering
SystemCallFilter=@system-service
SystemCallArchitectures=native

# Environment
EnvironmentFile=/etc/eggpool/env

[Install]
WantedBy=multi-user.target
"""

LOGROTATE_CONF = """\
/var/log/eggpool/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
    maxsize 100M
}
"""

CRON_BACKUP_FILE = """\
# EggPool daily backup
#
# Runs /usr/local/bin/eggpool-backup every day at 02:00 as root. The
# script snapshots /etc/eggpool/{config.toml,env} and the SQLite
# database under /var/backups/eggpool, retaining the last 30 days.

0 2 * * * root /usr/local/bin/eggpool-backup
"""

CRON_BACKUP_SCRIPT = """\
#!/bin/bash
#
# EggPool daily backup script.
# Installed by `eggpool deploy cron` to /usr/local/bin/eggpool-backup.
#
# Snapshots configuration, environment, and SQLite database under
# /var/backups/eggpool and retains the last 30 days of archives.

set -euo pipefail

BACKUP_DIR="/var/backups/eggpool"
KEEP_DAYS=30
DB_PATH="/var/lib/eggpool/usage.sqlite3"
CONFIG_DIR="/etc/eggpool"

mkdir -p "$BACKUP_DIR"

BACKUP_NAME="eggpool-$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

# Configuration (config.toml + env)
cp "$CONFIG_DIR/config.toml" "$BACKUP_PATH/"
cp "$CONFIG_DIR/env" "$BACKUP_PATH/"

# Database snapshot via SQLite backup for consistency
sudo -u eggpool sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH/usage.sqlite3'"

# Archive and remove the working tree
tar czf "$BACKUP_DIR/$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "$BACKUP_PATH"

# Prune old archives
find "$BACKUP_DIR" -name "eggpool-*.tar.gz" -mtime +$KEEP_DAYS -delete
"""
