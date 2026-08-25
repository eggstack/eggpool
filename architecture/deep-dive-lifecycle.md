# Deep Dive: Lifecycle Management

Back to [Overview](overview.md)

## Purpose

Backup, restore, and uninstall orchestration for EggPool installations.

## Module Structure

```
src/eggpool/lifecycle/
├── __init__.py
├── backup.py      # Backup and restore helpers (~593 lines)
└── uninstall.py   # Uninstall orchestration (~778 lines)
```

## Key Components

### Backup (`backup.py`)

Creates timestamped `.zip` archives containing the complete installation state.

**Archive contents:**
- `config.toml` — the live configuration
- `.env` (optional) — the environment / API-key file
- `usage.sqlite3` — the SQLite database
- `usage.sqlite3-wal` — WAL journal if present
- `usage.sqlite3-shm` — shared-memory file if present
- `META` — plain-text metadata (version, install method, timestamp)

**Archive naming:** `eggpool-backup-YYYYMMDD-HHMMSS.zip` — lexicographic and chronological order agree.

**Storage:** Uncompressed (`zipfile.ZIP_STORED`) because contents are already small. The `.zip` suffix is used because it's hand-restorable on any platform without `tar` or `gzip`.

Runtime backups use `sqlite3.Connection.backup()` for a consistent snapshot.
Snapshotting, full-file archive construction, atomic publication, and staging
cleanup run together through `asyncio.to_thread()` so large backups do not
block EggPool's canonical event loop. Retention pruning is also off-loop.

**Metadata format:**
```
format_version = 1
created_at = '2025-01-15T10:30:00+00:00'
install_method = 'pipx'
config_path = '/path/to/config.toml'
db_path = '/path/to/usage.sqlite3'
members = ["config.toml", "usage.sqlite3"]
```

**Restore process:**
1. Validate `META` against current installation
2. Extract `config.toml` and `.env`
3. Restore SQLite database (stop service first)
4. Restart service

### Uninstall (`uninstall.py`)

Reverses installation by detecting the installer method and cleaning up.

**Install methods detected:**
| Method | Detection | Cleanup |
|--------|-----------|---------|
| `pipx` | Binary path under `pipx/venvs` or `pipx/shared` | `pipx uninstall eggpool` |
| `uv-tool` | Binary path under `uv/tools` | `uv tool uninstall eggpool` |
| `source` | Project root detection | Deletes source checkout directory |
| `manual` | Fallback | Manual cleanup |

**PATH cleanup:**
- Detects eggpool-attributable entries (containing `eggpool` in PATH/export lines, `uv tool update-shell` directives, or `# Added by eggpool` comment blocks)
- Removes matching PATH entries from shell profiles
- Supports bash and zsh (default rc files: `.zshrc`, `.bashrc`, `.bash_profile`, `.profile`)

**Safety:**
- Interactive confirmation before any destructive action
- Detects and warns about running processes
- Cleans up PID files, log files, state directory

## CLI Integration

```bash
eggpool backup              # Create timestamped backup
eggpool backup --output /path/to/backup.zip  # Custom output path
eggpool uninstall           # Interactive uninstall
```

## Key Invariants

- Backup is atomic — partial archives are never created
- Uninstall detects install method automatically — no operator knowledge required
- PID file and running process detection prevents data loss
- PATH cleanup is idempotent — safe to run multiple times
- Both modules are testable without terminal interaction

## Configuration

These modules read configuration from the standard `config.toml` and state directory. No separate config section.

## Related

- [deep-dive-core.md](deep-dive-core.md) — Config file helpers
- [deep-dive-deployment.md](deep-dive-deployment.md) — Systemd service management
- [deep-dive-database.md](deep-dive-database.md) — SQLite backup considerations
