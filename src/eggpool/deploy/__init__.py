"""Bundled deployment assets for `eggpool deploy`.

These constants are the canonical source of truth for the snippets
emitted by the ``eggpool deploy`` CLI group. The matching files under
``deploy/`` at the repository root are kept byte-for-byte identical so
both source-checkout operators and wheel-installed users see the same
content. To update any snippet, edit it here AND in ``deploy/``.
"""

from __future__ import annotations

from typing import Any

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
    *,
    user: str | None = None,
    group: str | None = None,
) -> str:
    """Generate a systemd unit for personal (single-user) use.

    Unlike :data:`SYSTEMD_UNIT`, this omits the dedicated ``eggpool``
    user, security hardening directives, and production paths. It
    targets the invoking user's own environment and is not intended
    for public-facing deployments.

    ``user`` and ``group`` set the systemd ``User=`` and ``Group=``
    directives. They default to the invoking user's identity so the
    service runs as the operator rather than root. Pass ``user="root"``
    and ``group="root"`` to install a root-owned personal unit.
    """
    resolved_user = user if user else "eggpool-user"
    resolved_group = group if group else resolved_user
    env_line = f"\nEnvironmentFile={env_path}" if env_path else ""

    return f"""\
[Unit]
Description=EggPool
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={resolved_user}
Group={resolved_group}
ExecStart={binary_path} --config {config_path} serve
WorkingDirectory={data_dir}
Environment=EGGPOOL_CONFIG={config_path}
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


def build_personal_watchdog_cron(
    binary_path: str,
    config_path: str,
    log_path: str,
    interval_minutes: int = 5,
) -> str:
    """Generate a user crontab fragment for the EggPool watchdog.

    The fragment contains two ``eggpool ensure-running`` lines bracketed
    by ``BEGIN EggPool watchdog`` / ``END EggPool watchdog`` markers so
    uninstall can strip them without disturbing unrelated cron entries.
    The ``@reboot`` line is the immediate startup trigger and the
    ``*/N * * * *`` line is the periodic poll.

    ``interval_minutes`` must be in 1-59 (a cron ``*/N`` expression does
    not support zero and 60+ would alias every hour, not every minute).
    """
    if not 1 <= interval_minutes <= 59:
        raise ValueError(f"interval_minutes must be 1-59, got {interval_minutes}")

    cmd = f"{binary_path} --config {config_path} ensure-running"
    log_target = f"{log_path} 2>&1"
    return f"""\
# BEGIN EggPool watchdog (managed by eggpool deploy cron)
@reboot {cmd} >> {log_target}
*/{interval_minutes} * * * * {cmd} >> {log_target}
# END EggPool watchdog
"""


def build_personal_backup_block(binary_path: str) -> str:
    """Generate the user crontab fragment for the daily backup.

    Returned as a BEGIN/END-marked block so :func:`uninstall_cron_block`
    can remove only the eggpool-attributable lines without disturbing
    unrelated user cron entries.
    """
    return f"""\
# BEGIN EggPool backup (managed by eggpool deploy backup-cron)
0 2 * * * {binary_path}
# END EggPool backup
"""


def strip_managed_cron_blocks(text: str) -> str:
    """Remove EggPool-managed crontab blocks from ``text``.

    Strips lines inside any ``# BEGIN EggPool ...`` / ``# END EggPool ...``
    block (watchdog, backup, or future variants). Other lines are
    preserved verbatim. The function is intentionally tolerant: missing
    markers are no-ops, and partial markers leave the text unchanged.
    """
    out: list[str] = []
    inside_block = False
    block_was_open = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# BEGIN EggPool"):
            inside_block = True
            block_was_open = True
            continue
        if inside_block and stripped.startswith("# END EggPool"):
            inside_block = False
            continue
        if inside_block:
            continue
        out.append(line)
    if block_was_open and inside_block:
        return text
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def install_cron_block(
    block: str,
    *,
    user: str | None = None,
    runner: Any | None = None,
) -> None:
    """Install a BEGIN/END-marked cron block into the user's crontab.

    The runner is injectable for tests; default behaviour reads the
    current crontab via ``crontab -l`` (or ``crontab -u <user> -l`` when
    ``user`` is set), appends the block, and writes it back via
    ``crontab -`` (or ``crontab -u <user> -``). Pre-existing identical
    blocks are not duplicated.
    """
    import subprocess  # noqa: PLC0415

    run = runner if runner is not None else subprocess.run

    list_cmd = ["crontab", "-l"] if user is None else ["crontab", "-u", user, "-l"]
    list_result = run(list_cmd, capture_output=True, text=True, check=False)
    existing = list_result.stdout if list_result.returncode == 0 else ""

    if strip_managed_cron_blocks(existing).strip() == block.strip():
        return

    stripped_existing = strip_managed_cron_blocks(existing).rstrip()
    if stripped_existing and not stripped_existing.endswith("\n"):
        new_cron = stripped_existing + "\n\n" + block
    elif stripped_existing:
        new_cron = stripped_existing + "\n" + block
    else:
        new_cron = block

    set_cmd = ["crontab", "-"] if user is None else ["crontab", "-u", user, "-"]
    run(set_cmd, input=new_cron, text=True, check=True)


def remove_cron_block(
    *,
    user: str | None = None,
    runner: Any | None = None,
) -> str:
    """Remove every EggPool-managed crontab block and write the result back.

    Returns the new crontab text (post-strip) so tests can assert on it.
    """
    import subprocess  # noqa: PLC0415

    run = runner if runner is not None else subprocess.run

    list_cmd = ["crontab", "-l"] if user is None else ["crontab", "-u", user, "-l"]
    list_result = run(list_cmd, capture_output=True, text=True, check=False)
    existing = list_result.stdout if list_result.returncode == 0 else ""

    new_cron = strip_managed_cron_blocks(existing)
    if new_cron == existing:
        return existing

    set_cmd = ["crontab", "-"] if user is None else ["crontab", "-u", user, "-"]
    run(set_cmd, input=new_cron, text=True, check=True)
    return new_cron


def build_personal_logrotate() -> str:
    """Generate a logrotate config for personal use.

    Identical to ``LOGROTATE_CONF`` — the log path is the same.
    """
    return LOGROTATE_CONF
