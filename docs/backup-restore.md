# Backup and Restore

## Automatic Backups

EggPool can create automatic daily backups through the opt-in
`automatic_backup` supervised background task. When enabled, it produces
restore-compatible `.zip` archives every 24 hours (after a 5-minute startup
delay) and retains the last 14 by default.

```toml
# Optional — automatic backups are disabled by default
[backup]
enabled = true  # Opt in; disabled in the low-wear SBC example.
interval_s = 86400         # every 24 hours
retain_count = 14          # keep last 14 backups
startup_delay_s = 300      # wait 5 min after boot before first backup
# directory = "/path/to/backups"  # override default location
include_env = true         # include .env file (may contain API keys)
```

The default backup directory depends on the installation type:

- **Production** (`/var/lib/eggpool` exists): `/var/lib/eggpool/backups`
- **Personal**: `~/backups/eggpool/` (or `$XDG_BACKUP_HOME/eggpool`)

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

EggPool ships with `eggpool backup` that bundles `config.toml`, `.env`,
and the SQLite database (plus `-wal`/`-shm` if present) into a single
timestamped `.zip` archive.

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

Prefer `eggpool backup --output-dir <dir>` — it stages a consistent
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
sudo env "PATH=$PATH" "$(command -v eggpool)" deploy backup-cron --install
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

The CLI stops the running server (if any), stages the restored files
alongside the current ones, swaps them into place, and restarts the
server on success. If any restore step fails, the original files are
preserved under `<data-dir>/rollback-<timestamp>/` and the server is
left stopped so the operator can intervene.

### Manual restore

#### Stop the service

```bash
sudo systemctl stop eggpool
```

#### Restore configuration

```bash
sudo cp backup/config.toml /etc/eggpool/config.toml
sudo cp backup/env /etc/eggpool/env
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

## Uninstall

To remove EggPool entirely, run:

```bash
eggpool uninstall
```

The command interactively confirms each removal step:

1. Removes the binary via the install method that was detected
   (`pipx uninstall eggpool`, `uv tool uninstall eggpool`, or local
   cleanup for source/manual installs).
2. Deletes the active `config.toml`, `.env`, and the SQLite database.
3. Removes `eggpool` entries from the user's shell rc (`~/.zshrc`,
   `~/.bashrc`, etc.), including the marker block installed by
   `scripts/install.sh` and any `uv tool update-shell` lines.
4. Leaves existing backups under `~/backups/eggpool/` in place.

Pass `--deploy-artifacts` to also remove systemd, logrotate, and cron
artifacts (otherwise they are left in place):

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
