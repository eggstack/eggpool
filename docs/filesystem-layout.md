# Filesystem Layout

Standard directories for a production deployment on Linux.

## Directory Structure

```
/etc/eggpool/
├── config.toml          # Main configuration file
└── env                  # Environment variables (API keys)

/var/lib/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

/var/log/eggpool/
└── eggpool.log         # Application log (if using file logging)

/opt/eggpool/
├── .venv/               # Python virtual environment
└── src/                 # Application source code
```

## Permissions

| Path | Owner | Mode | Description |
|------|-------|------|-------------|
| `/etc/eggpool/` | `root:root` | `0755` | Configuration directory |
| `/etc/eggpool/config.toml` | `root:eggpool` | `0640` | Configuration file |
| `/etc/eggpool/env` | `root:eggpool` | `0640` | Environment file (contains secrets) |
| `/var/lib/eggpool/` | `eggpool:eggpool` | `0750` | Data directory |
| `/var/lib/eggpool/*.sqlite3` | `eggpool:eggpool` | `0640` | Database files |
| `/var/log/eggpool/` | `eggpool:eggpool` | `0750` | Log directory |
| `/opt/eggpool/` | `root:eggpool` | `0755` | Application directory |

## Notes

- The `env` file must be readable by the `eggpool` user but not world-readable.
- The database directory must be writable by the `eggpool` user.
- SQLite WAL mode allows concurrent reads during writes.
- The systemd unit uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/eggpool`.
- Backups should copy `/var/lib/eggpool/usage.sqlite3*` and `/etc/eggpool/config.toml`.
