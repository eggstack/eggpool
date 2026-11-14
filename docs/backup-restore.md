# Backup and Restore

## Automatic Backups

EggPool can create automatic daily backups through the opt-in
`automatic_backup` supervised background task. When enabled, it produces
restore-compatible `.zip` archives every 24 hours (after a 5-minute startup
delay) and retains the last 14 by default.

```toml
# Optional: automatic backups are disabled by default
[backup]
enabled = true  # Opt in; disabled in the low-wear SBC example.
interval_s = 86400         # every 24 hours
retain_count = 14          # keep last 14 backups
startup_delay_s = 300      # wait 5 min after boot before first backup
# directory = "/path/to/backups"  # override default location
include_env = true         # include .env file (may contain API keys)
```

Unless `[backup].directory` is set, `$XDG_BACKUP_HOME/eggpool` is used when
`$XDG_BACKUP_HOME` is set. Otherwise, EggPool uses
`/var/lib/eggpool/backups` when `/var/lib/eggpool` exists, or
`~/backups/eggpool/` for personal installs.

The automatic task uses the tokio-rusqlite `backup_to` API for consistent
snapshots and writes archives atomically (write-to-temp + rename). Snapshot,
archive-copy, and staging cleanup work uses bounded native task scheduling.
No external `sqlite3` binary is required.

The `eggpool deploy backup-cron` path remains available for operators
who prefer external scheduling or want backups even when the server
process is not running.

## Manual Backup

| File | Description | Frequency |
|------|-------------|-----------|
| `/etc/eggpool/config.toml` | Configuration | On change |
| `/etc/eggpool/env` | API keys | On change |
| `/var/lib/eggpool/usage.sqlite3*` | Database | Daily |

## Backup

### Using the CLI (recommended)

`eggpool backup` creates a timestamped `.zip` archive containing `config.toml`,
the SQLite database snapshot, and the config-adjacent `.env` when
`include_env` is enabled. The archive stores a single `usage.sqlite3` database
member, not separate WAL or SHM files. A non-adjacent file selected by
`$EGGPOOL_ENV` and credentials supplied only through the process environment are
not included; back them up separately.

The database snapshot includes local users, budget rules and audit records,
active budget holds, settled user/model usage, and secret-free personal
upstream bindings. Credentials are not stored in the database. Restoring the
snapshot preserves committed budget state. Pending requests and active holds
follow the normal crash-recovery rules on the next server start.

```bash
# Default location: ~/backups/eggpool/
eggpool backup

# Custom location
eggpool backup --output-dir /var/backups/eggpool

# Override config path
eggpool --config /etc/eggpool/config.toml backup
```

Output filenames follow `eggpool-backup-YYYYMMDD-HHMMSS.zip`. The
active config file is used to discover the database path, so no extra
flags are required in the common case. The archive layout is:

```
eggpool-backup-20260624-120000.zip
├── META           (format version, install method, member list)
├── config.toml
├── usage.sqlite3
└── .env           (if present)
```

### Manual backup without the CLI

Prefer `eggpool backup --output-dir <dir>`: it stages a consistent
database snapshot plus `config.toml`/`.env` into one atomic ZIP. Only if
the CLI is unavailable, copy the config files directly and snapshot the
database to a cold copy first (never copy a live `-wal`/`-shm` pair):

```bash
BACKUP_DIR="/var/backups/eggpool/$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BACKUP_DIR"
sudo cp /etc/eggpool/config.toml "$BACKUP_DIR/"
sudo cp /etc/eggpool/env "$BACKUP_DIR/"
sudo systemctl stop eggpool
sudo cp /var/lib/eggpool/usage.sqlite3 "$BACKUP_DIR/usage.sqlite3"
sudo systemctl start eggpool
```

### Automated backup (cron)

Use the supported installer instead of hand-writing scripts:

```bash
sudo env "PATH=$PATH" "HOME=$HOME" "$(command -v eggpool)" deploy backup-cron --install
```

It writes `/usr/local/bin/eggpool-backup` (a wrapper that execs
`eggpool backup`) and a daily `0 2 * * *` entry, keeping 30 days of
archives. See [Deployment](deployment.md) for the personal vs
production paths.

## Restore

### Using the CLI (recommended)

`eggpool recover` reads an archive produced by `eggpool backup` and
restores its contents to the locations recorded in `META`.

```bash
# Interactive: pick from ~/backups/eggpool/
eggpool recover

# Explicit path
eggpool recover ~/backups/eggpool/eggpool-backup-20260624-120000.zip
```

Stop any foreground `eggpool serve --verbose` process before recovery. The CLI
stops a daemon when its PID file is present, but does not detect a foreground
server without one. After a successful restore, it leaves the server stopped
and prints a reminder to restart it so the restored configuration is loaded.
If a restore step fails, EggPool attempts to restore the original files
atomically. If that rollback also fails, the CLI reports a rollback error.
Keep the server stopped until the database and configuration are consistent.

### Manual restore

#### Stop the service

```bash
sudo systemctl stop eggpool
```

#### Restore configuration

```bash
sudo cp backup/config.toml /etc/eggpool/config.toml
sudo cp backup/.env /etc/eggpool/env
sudo chown root:eggpool /etc/eggpool/config.toml /etc/eggpool/env
sudo chmod 640 /etc/eggpool/config.toml /etc/eggpool/env
```

#### Restore database

```bash
# Remove current database
sudo rm -f /var/lib/eggpool/usage.sqlite3*

# Restore from backup
sudo cp backup/usage.sqlite3 /var/lib/eggpool/usage.sqlite3
sudo chown eggpool:eggpool /var/lib/eggpool/usage.sqlite3
```

#### Start the service

```bash
sudo systemctl start eggpool
```

### Verify

```bash
# Check service status
sudo systemctl status eggpool

# Check health
curl -s http://localhost:11300/v1/healthz

# Check dashboard
curl -s http://localhost:11300/
```

## Database Migration

After restoring a backup from an older version, run migrations:

```bash
sudo systemctl stop eggpool
sudo -u eggpool /usr/local/bin/eggpool --config /etc/eggpool/config.toml migrate
sudo systemctl start eggpool
```

Migrations are idempotent and safe to run multiple times.

Migrations 55 to 63 added local user identity, browser sessions, policy
profiles, user and instance budgets, the future-member default profile, and
audit tables. The embedded migration inventory now reaches schema 66.
Backups include usernames, password hashes, personal-token digests,
session and CSRF digests, policy and budget state, bounded usage records,
and secret-free audit facts. They do not contain plaintext passwords, API
tokens, session cookies, or CSRF tokens. A backup taken during requests
captures committed pending rows and active budget holds. On startup after
restore, pending requests are interrupted and active holds are released
without adding their usage, as they are after a crash.

Migrations run with `eggpool migrate` or during normal server startup. Existing
provider accounts and keys remain in TOML and the environment. A schema-54
backup migrates forward through current schema 66, preserving legacy requests,
usage history, quarantine, and catalog state. Requests made with the historical
shared key remain unattributed as `legacy_shared_key`; they are not assigned
to a fabricated user. Until an administrator bootstraps local access, the
database reports `legacy_unmigrated`.

Migrations are forward-only. An older executable may reject a database at
schema 66. For rollback, restore a protected backup made before the upgrade
and use the matching older binary. Do not point an older executable at the
upgraded database. Capture the protected backup before running `migrate`.

## Uninstall

To remove EggPool entirely, run:

```bash
eggpool uninstall
```

The command confirms removal, then removes the resolved executable and selected
configuration, data, and state paths. It does not call `uv` or `pipx`, and it
does not elevate privileges. For a package-manager-owned install, also remove
the package with its owning manager. Run the command with permission to remove
every selected path.

By default it also removes `eggpool` entries from the user's shell rc files,
including the marker block installed by `scripts/install.sh`. The personal
backup directory `~/backups/eggpool/` is outside the default data path and is
left in place. In production, backups under `/var/lib/eggpool/backups` are part
of the data directory and are removed unless `--keep-data` is used.

Pass `--deploy-artifacts` to also remove the known systemd, logrotate, and
cron artifacts. The command does not invoke `sudo`; run it with the required
permissions. Without this flag, deployment artifacts are left in place.

```bash
eggpool uninstall --yes --deploy-artifacts
```

Flags:

| Flag | Effect |
|------|--------|
| `--yes` | Skip every confirmation prompt |
| `--keep-config` | Leave `config.toml` and `.env` in place |
| `--keep-data` | Leave the SQLite database in place |
| `--keep-path` | Skip the shell-rc cleanup step |
| `--deploy-artifacts` | Also remove systemd, logrotate, and cron artifacts |

Examples:

```bash
# Fully automated uninstall (no prompts)
eggpool uninstall --yes

# Keep backups and config, only remove the binary and shell PATH entries
eggpool uninstall --keep-config --keep-data --yes
```

## Power Loss and Data Durability

EggPool uses SQLite with WAL mode and `synchronous = NORMAL` for a
balance of performance and durability.

### Correctness-critical state

The following state is persisted immediately during request processing:

- Request creation and final status
- Reservation creation and release
- Attempt creation and completion
- Upstream error/suppression/backoff state

This state survives power loss with the same durability as the
underlying storage device.

### Buffered analytics

When using `write_mode = "balanced"` or `write_mode = "low_wear"`,
analytics data (timeseries, bandwidth, model/account aggregates) is
buffered in memory and flushed periodically to the `usage_rollups`
table.

After abrupt power loss, at most `flush_interval_s` seconds of
analytics data may be lost. The default flush interval is 30 seconds
for `balanced` mode and 120 seconds for `low_wear` mode.

Correctness-critical request state is never affected by analytics
buffering.

### Recovery

After power loss, EggPool automatically recovers:

1. Pending requests are marked as interrupted
2. Active reservations are released
3. In-memory state is rebuilt from the database

No manual intervention is required.
