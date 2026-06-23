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


# ---------------------------------------------------------------------------
# Dynamic snippet builders for personal-use `--install` mode.
# These generate content tailored to the current user's system.
# ---------------------------------------------------------------------------


def build_personal_systemd_unit(
    binary_path: str,
    config_path: str,
    data_dir: str,
    env_path: str | None = None,
) -> str:
    """Generate a systemd unit for personal (single-user) use.

    Unlike ``SYSTEMD_UNIT``, this omits the dedicated ``eggpool`` user,
    security hardening directives, and production paths.  It targets
    the invoking user's own environment and is not intended for
    public-facing deployments.
    """
    env_line = f"\nEnvironmentFile={env_path}" if env_path else ""

    return f"""\
[Unit]
Description=EggPool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary_path} --config {config_path} serve
WorkingDirectory={data_dir}
# Configuration changes require: systemctl restart eggpool
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM{env_line}

[Install]
WantedBy=multi-user.target
"""


def build_personal_backup_script(config_path: str, db_path: str) -> str:
    """Generate a backup script for personal (single-user) use.

    Unlike ``CRON_BACKUP_SCRIPT``, this runs as the current user without
    ``sudo -u eggpool`` and uses the user's own config and database paths.
    """
    return f"""\
#!/bin/bash
#
# EggPool daily backup script (personal use).
# Installed by `eggpool deploy cron --install`.
#
# Snapshots configuration and SQLite database under
# ~/backups/eggpool, retaining the last 30 days of archives.

set -euo pipefail

BACKUP_DIR="$HOME/backups/eggpool"
KEEP_DAYS=30
DB_PATH="{db_path}"
CONFIG_PATH="{config_path}"

mkdir -p "$BACKUP_DIR"

BACKUP_NAME="eggpool-$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

# Configuration
cp "$CONFIG_PATH" "$BACKUP_PATH/"

# Database snapshot via SQLite backup for consistency
sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH/usage.sqlite3'"

# Archive and remove the working tree
tar czf "$BACKUP_DIR/$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "$BACKUP_PATH"

# Prune old archives
find "$BACKUP_DIR" -name "eggpool-*.tar.gz" -mtime +$KEEP_DAYS -delete
"""


def build_personal_backup_cron() -> str:
    """Generate a user cron entry for daily backup (personal use).

    Unlike ``CRON_BACKUP_FILE``, this is a user cron entry (no ``root``
    user field) targeting ``~/backups/eggpool``.
    """
    return """\
# EggPool daily backup (personal use — user cron, not /etc/cron.d/)
0 2 * * * /usr/local/bin/eggpool-backup
"""


def build_personal_logrotate() -> str:
    """Generate a logrotate config for personal use.

    Identical to ``LOGROTATE_CONF`` — the log path is the same.
    """
    return LOGROTATE_CONF
