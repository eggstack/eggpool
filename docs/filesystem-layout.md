# Filesystem Layout

Standard directories for a production deployment on Linux.

## Directory Structure

```
/etc/gorouter/
├── config.toml          # Main configuration file
└── env                  # Environment variables (API keys)

/var/lib/gorouter/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

/var/log/gorouter/
└── gorouter.log         # Application log (if using file logging)

/opt/gorouter/
├── .venv/               # Python virtual environment
└── src/                 # Application source code
```

## Permissions

| Path | Owner | Mode | Description |
|------|-------|------|-------------|
| `/etc/gorouter/` | `root:root` | `0755` | Configuration directory |
| `/etc/gorouter/config.toml` | `root:gorouter` | `0640` | Configuration file |
| `/etc/gorouter/env` | `root:gorouter` | `0640` | Environment file (contains secrets) |
| `/var/lib/gorouter/` | `gorouter:gorouter` | `0750` | Data directory |
| `/var/lib/gorouter/*.sqlite3` | `gorouter:gorouter` | `0640` | Database files |
| `/var/log/gorouter/` | `gorouter:gorouter` | `0750` | Log directory |
| `/opt/gorouter/` | `root:gorouter` | `0755` | Application directory |

## Notes

- The `env` file must be readable by the `gorouter` user but not world-readable.
- The database directory must be writable by the `gorouter` user.
- SQLite WAL mode allows concurrent reads during writes.
- The systemd unit uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/gorouter`.
- Backups should copy `/var/lib/gorouter/usage.sqlite3*` and `/etc/gorouter/config.toml`.
