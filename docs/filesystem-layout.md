# Filesystem Layout

EggPool supports two filesystem layouts: a **personal** layout for
private LAN deployments (default) and a **production** layout with a
dedicated system user (opt-in via `eggpool deploy systemd --install
--production`).

## Personal layout (default)

Personal deployments honor the XDG Base Directory specification. The
defaults are resolved by `eggpool.deploy_user.default_config_dir`,
`default_data_dir`, and `default_state_dir`.

```
~/.config/eggpool/
├── config.toml          # Main configuration file
└── .env                 # Environment variables (API keys), optional

~/.local/share/eggpool/
├── usage.sqlite3        # SQLite database
├── usage.sqlite3-wal    # WAL journal
└── usage.sqlite3-shm    # Shared memory file

~/.local/state/eggpool/
├── eggpool.pid          # Supervisor PID file
├── eggpool.log          # Daemon log (when run via `serve --daemon`)
└── cron.log             # Watchdog cron output (when using `deploy cron`)
```

The CLI's config-path precedence is `--config PATH` > `$EGGPOOL_CONFIG`
> `~/.config/eggpool/config.toml` (when present) > `./config.toml`
(source-checkout default). The resolver lives in
`eggpool.deploy_user.resolve_config_path()`.

### Personal permissions

| Path | Owner | Mode |
|------|-------|------|
| `~/.config/eggpool/` | invoking user | `0755` |
| `~/.config/eggpool/config.toml` | invoking user | `0644` |
| `~/.config/eggpool/.env` | invoking user | `0600` |
| `~/.local/share/eggpool/` | invoking user | `0755` |
| `~/.local/share/eggpool/*.sqlite3` | invoking user | `0644` |
| `~/.local/state/eggpool/` | invoking user | `0755` |

`eggpool deploy systemd --install` creates the directories above as the
deploy user before writing the unit file.

## Production layout

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
└── src/eggpool/         # Application source code

/var/backups/eggpool/    # Daily-backup destination (production)
/usr/local/bin/eggpool-backup  # Production backup script
/etc/cron.d/eggpool-backup     # Production backup cron entry
```

### Production permissions

| Path | Owner | Mode | Description |
|------|-------|------|-------------|
| `/etc/eggpool/` | `root:eggpool` | `0755` | Configuration directory |
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
- The production systemd unit uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/eggpool`.
- Backups should copy `usage.sqlite3*` plus the active configuration. For personal installs the default target is `~/backups/eggpool/`; for production it is `/var/backups/eggpool/`.
