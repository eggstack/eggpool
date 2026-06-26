# Automatic Runtime Backups Plan

## Purpose

EggPool already has backup and restore lifecycle helpers and separate cron-oriented backup deployment snippets. The next implementation pass should add a default in-process automatic backup task that runs under the existing FastAPI/TaskSupervisor runtime, produces restore-compatible lifecycle backup archives, and retains the last 14 successful backups by default.

The goal is operational resilience for personal and small-SBC deployments. A user running EggPool continuously on a Raspberry Pi or similar host should get daily restorable snapshots without having to remember to install a separate backup cron job. The feature must be conservative about write amplification, safe against live SQLite writes, and compatible with the current restore path.

## Current repository state

The application already has a supervised background task model. `src/eggpool/app.py` creates a `TaskSupervisor`, registers catalog refresh, retention cleanup, checkpointing, usage-window refresh, stale request finalization, and the update checker, then starts all tasks during lifespan startup. This is the correct place to register an `automatic_backup` task.

The lifecycle backup implementation already lives under `src/eggpool/lifecycle/backup.py`. It creates timestamped `.zip` archives named `eggpool-backup-YYYYMMDD-HHMMSS.zip`, writes a `META` member, and supports restore of config, optional env, and SQLite files. This should be the canonical archive format for automatic backups.

The deploy backup cron path currently uses shell scripts that produce `.tar.gz` archives via `sqlite3 .backup` and prune by age. That script is runtime-consistency aware, but it is not the same archive format as lifecycle restore. The automatic backup task should consolidate on the lifecycle `.zip` format and should not introduce a third backup format.

## Design constraints

The automatic task must not raw-copy a live SQLite WAL database as its primary mechanism. The current lifecycle `create_backup()` archives the resolved database file and WAL/SHM companions directly. That is acceptable for stopped-service or simple operator-triggered backups, but it is not the preferred default for an always-on runtime task.

The implementation should create a consistent SQLite snapshot first, then place that snapshot into the existing restore-compatible archive. For runtime backups, the archive should normally contain one consistent `usage.sqlite3` member and should not include `usage.sqlite3-wal` or `usage.sqlite3-shm` unless a future explicit raw-copy mode is requested. SQLite's backup API or an equivalent SQLite-consistent snapshot should be used.

The task must never block startup for a long period. It should run after a startup delay, not immediately. It must not prevent server readiness if a backup fails. Failures should be logged and reflected in the background task monitor state, but the server should continue serving.

Retention should be count-based, not age-based. The default requested behavior is every 24 hours and retain the last 14. Count-based retention is deterministic when devices are offline for days and avoids unbounded backup growth.

All writes should be atomic at the archive level. Write to a temporary path in the backup directory, close it, then atomically rename into the final `eggpool-backup-*.zip` path. If the process loses power midway, the final backup filename should not point at a partial archive.

## Configuration surface

Add a new config model in `src/eggpool/models/config.py`:

```python
class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_s: int = Field(default=86_400, ge=0)
    retain_count: int = Field(default=14, ge=1)
    startup_delay_s: int = Field(default=300, ge=0)
    directory: str | None = None
    include_env: bool = True
```

Then add this field to `AppConfig`:

```python
backup: BackupConfig = Field(default_factory=BackupConfig)
```

Semantics:

`enabled = true` registers the background task. `enabled = false` disables all in-process automatic backup behavior.

`interval_s = 86400` runs once per 24 hours. Setting `interval_s = 0` should disable registration as a compatibility escape hatch, even if `enabled` is true.

`retain_count = 14` keeps the last 14 completed backup archives matching the lifecycle filename family in the selected backup directory.

`startup_delay_s = 300` waits five minutes before the first automatic backup attempt. This avoids competing with migrations, crash recovery, startup catalog refresh, and early dashboard/API traffic.

`directory = null` uses a runtime-aware default. For personal installs this can continue to use `default_backup_dir()` (`$XDG_BACKUP_HOME/eggpool` or `~/backups/eggpool`). For production/systemd-hardened installs, prefer a directory inside the data directory unless the operator explicitly configures `/var/backups/eggpool` and grants write permission. See deployment notes below.

`include_env = true` includes the resolved environment file when one is known and exists. The implementation must not fail if there is no env file.

## Runtime backup module

Add a new module:

```text
src/eggpool/background/backup.py
```

Suggested public functions:

```python
async def automatic_backup_loop(
    *,
    config: AppConfig,
    db: Database,
    config_path: Path,
    env_path: Path | None,
) -> None: ...

async def create_runtime_backup(
    *,
    db: Database,
    config_path: Path,
    env_path: Path | None,
    output_dir: Path,
    install_method: str,
    include_env: bool,
) -> Path: ...

def prune_backups(*, backup_dir: Path, retain_count: int) -> list[Path]: ...
```

`automatic_backup_loop()` should:

1. Sleep for `startup_delay_s` before first attempt.
2. Attempt `create_runtime_backup()`.
3. On success, run `prune_backups()`.
4. Log the created archive path and pruned paths.
5. Catch ordinary exceptions, log them, and continue.
6. Re-raise `asyncio.CancelledError`.
7. Sleep `interval_s` between attempts.

The loop should use the same cancellation style as the other background loops. It should not use cron-like wall-clock scheduling in the first pass; a simple interval loop is adequate and lower risk.

## SQLite snapshot implementation

Do not require the external `sqlite3` CLI for the in-process task. The runtime already uses Python and `aiosqlite`; requiring the shell binary would regress portability and duplicate the cron-script dependency.

Preferred implementation:

1. Create a temporary staging directory under the selected backup directory, for example `.eggpool-backup-staging-<pid>-<timestamp>`.
2. Use a separate SQLite connection to run the online backup into `staging/usage.sqlite3`. With stdlib `sqlite3`, open the live DB as source and destination snapshot as target, then call `source.backup(dest)`. Because this is synchronous, wrap the call in `asyncio.to_thread()` so the event loop is not blocked.
3. Use a short-to-moderate busy timeout when opening the SQLite source. Match or derive from `config.database.busy_timeout_ms`.
4. After backup completes, close both connections before archive creation.
5. Build a `BackupContents` pointing at the real config path, the staged snapshot DB path, optional env path, and install method. Ensure the archive metadata still records the real live `db_path`, not the temporary snapshot path, so restore targets the correct DB location.

The last requirement likely means the existing `BackupContents` is not quite enough, because it currently uses the same `db_path` for both source member selection and metadata target. Implement one of these clean options:

Option A, preferred: refactor lifecycle backup into a lower-level archive writer that accepts separate source paths and restore target metadata.

```python
@dataclass(frozen=True)
class BackupSource:
    config_source: Path
    db_source: Path
    env_source: Path | None
    config_target: Path
    db_target: Path
    env_target: Path | None
    install_method: str
```

Then implement both CLI/manual and runtime backup through this common writer.

Option B, acceptable for smaller change: add optional `metadata_config_path`, `metadata_db_path`, and `metadata_env_path` fields to `BackupContents`. Default them to the source paths so existing callers keep the same behavior. Runtime backup can pass `db_path=staged_snapshot` while `metadata_db_path=live_db_path`.

Whichever option is chosen, add tests that prove restore metadata points back at the live DB path, not the temporary staging path.

## Archive creation and atomicity

Modify lifecycle archive creation so final archive publication is atomic:

1. Create backup directory if needed.
2. Generate final target name with existing timestamp naming.
3. Create a temporary archive path in the same directory, such as `.eggpool-backup-YYYYMMDD-HHMMSS.zip.tmp`.
4. Write the zip completely.
5. Close the zip file.
6. Replace/rename the temp file to the final path with `Path.replace()` or `os.replace()`.
7. If any step fails, unlink the temp file best-effort and leave any previous backups untouched.

Keep compression as `ZIP_STORED` unless a separate decision is made. The current rationale is acceptable: the contents are small, restore should be simple, and compression trades CPU for small storage gains on SBCs.

## Retention

Add `prune_backups(backup_dir, retain_count)` that:

1. Calls the existing `list_backups()` to find lifecycle-format backups.
2. Keeps the newest `retain_count` entries.
3. Deletes older entries, newest-first list ordering already handled by `list_backups()`.
4. Ignores unknown files and partial temp files.
5. Logs but does not fail the whole backup if one stale archive cannot be deleted.

Update `BACKUP_FILENAME_RE` if needed. The current regex only matches exact `eggpool-backup-YYYYMMDD-HHMMSS.zip`, while `create_backup()` can create `eggpool-backup-YYYYMMDD-HHMMSS-1.zip` on collision. Either stop creating suffixed filenames by making timestamp generation unique enough, or update the regex to include optional numeric suffixes. Retention must account for whatever filename family `create_backup()` can produce.

## App wiring

In `src/eggpool/app.py`, after creating the `TaskSupervisor` and before `await supervisor.start_all()`, register the task:

```python
if config.backup.enabled and config.backup.interval_s > 0:
    from eggpool.background.backup import automatic_backup_loop

    supervisor.register(
        "automatic_backup",
        lambda: automatic_backup_loop(
            config=config,
            db=db,
            config_path=Path(config_path) if config_path else resolved_config_path,
            env_path=resolve_env_path(...),
        ),
    )
```

The exact config path resolution needs care. `create_app(config_path=target)` receives the path from the Granian target loader. If `create_app(config=...)` is used in tests, there may be no config path. In that case, either use `EGGPOOL_CONFIG`, `config.toml`, or disable automatic backup for config-object-only tests unless a path is provided.

Do not register automatic backup for `config.database.path == ":memory:"`. Log a clear warning if backup is enabled with an in-memory database, or silently skip it in tests. Prefer explicit logging in production code.

## Env-file resolution

Automatic backups should preserve the environment file when one is part of the deployment. Current deployment code can derive an env path via `resolve_env_path(config_path=...)`. Reuse that logic rather than inventing another path convention.

If `include_env` is false, pass `env_path=None` regardless of discovery. If `include_env` is true but no env file exists, proceed with config and DB only.

Never serialize live environment variables into a backup archive. Only include an env file that already exists on disk and is explicitly part of the deployment layout.

## Deployment cleanup

The production systemd unit currently uses `ProtectSystem=strict` and only grants `ReadWritePaths=/var/lib/eggpool`. That means an in-process automatic backup cannot write to `/var/backups/eggpool` unless the unit is changed.

Choose one implementation policy:

Policy A, preferred for minimal hardening changes: default automatic backups to a `backups/` subdirectory under the EggPool data directory, for example `/var/lib/eggpool/backups` in production and `~/.local/share/eggpool/backups` or `~/backups/eggpool` for personal installs. This keeps the current systemd write boundary valid.

Policy B: keep `/var/backups/eggpool` as the production default and update `SYSTEMD_UNIT` to include:

```ini
ReadWritePaths=/var/lib/eggpool /var/backups/eggpool
```

If Policy B is chosen, also update root deploy snippets and docs so operators know the service itself writes backups.

The existing `eggpool deploy backup-cron` should remain for hosts that prefer external scheduling, but docs should describe it as optional. Longer-term, the cron script should call a Python CLI backup command that uses the same lifecycle archive writer instead of producing `.tar.gz` archives.

## CLI and operator UX

Keep existing manual backup and recover behavior intact. If a `backup` CLI command already exists elsewhere in the file, update it to use the new atomic archive writer. If no direct `eggpool backup` command is currently exposed despite lifecycle helpers existing, add one as a separate small step or ensure the existing recover/uninstall call sites are unaffected.

Add or update a command such as:

```text
eggpool backup --output-dir PATH
```

This command should use the same runtime-safe SQLite snapshot path when the DB exists and is not `:memory:`. Manual backup while the server is running should be safe by default.

Do not make automatic backup restoration automatic. Restore should remain an explicit operator action through `eggpool recover` or equivalent. The background task only creates and prunes backups.

## Observability

The task will automatically appear in `BackgroundTaskMonitor` snapshots because the supervisor tracks task names, running state, restart count, and error class. Use the exact task name `automatic_backup` so dashboard/runtime metrics can display it clearly.

Add structured operational logging for:

- automatic backup skipped because database is in-memory
- automatic backup started
- automatic backup succeeded with archive path and size
- retention pruned N archives
- backup failed with exception

Do not write an operational event into SQLite on every successful daily backup in the first pass unless dashboard UX specifically needs it. Recording backup success into the same DB being backed up is not harmful at daily cadence, but it creates a recursive state mutation and is not necessary for a first implementation. Logs plus task monitor are enough.

## Tests

Add unit tests for lifecycle backup behavior:

1. Archive creation writes a valid final `.zip` and no temp file remains.
2. Failed archive creation removes temp files and does not create a final archive.
3. Runtime backup metadata records the live DB target path, not the staged snapshot path.
4. Retention keeps newest 14 and deletes older lifecycle-format archives.
5. Retention ignores unknown files and partial `.tmp` files.
6. Filename parser accepts collision-suffixed names if the writer can emit them.

Add SQLite consistency tests:

1. Create a live SQLite DB in WAL mode.
2. Insert data and leave WAL mode active.
3. Run `create_runtime_backup()`.
4. Restore or open the archived `usage.sqlite3` member and verify the expected rows exist.
5. Confirm no WAL/SHM members are required in the runtime snapshot archive.

Add app wiring tests:

1. With default config and file-backed DB, `automatic_backup` is registered.
2. With `[backup].enabled = false`, it is not registered.
3. With `[backup].interval_s = 0`, it is not registered.
4. With `database.path = ":memory:"`, it is skipped and does not crash startup.
5. Existing background tasks still register as before.

Add config tests:

1. Default backup config is enabled, 86400 seconds, retain 14, startup delay 300.
2. Invalid negative interval/startup delay is rejected.
3. Invalid retain count below 1 is rejected.
4. Unknown keys under `[backup]` are rejected because `extra="forbid"`.

## Documentation updates

Update README or docs deployment sections to state:

EggPool now creates automatic daily backups by default.

Default cadence is every 24 hours after an initial startup delay.

Default retention is the last 14 successful backups.

Backups are restore-compatible lifecycle `.zip` archives.

`eggpool deploy backup-cron` is optional and mainly for operators who prefer external scheduling or want backups even when the server process is not running.

Document the `[backup]` config section with examples:

```toml
[backup]
enabled = true
interval_s = 86400
retain_count = 14
startup_delay_s = 300
# directory = "/path/to/backups"
include_env = true
```

Include a caution that env files may contain provider API keys. Backups should be stored with restrictive permissions and not uploaded casually.

## Suggested implementation sequence

1. Add `BackupConfig` and config tests.
2. Refactor lifecycle backup writer to support atomic archive publication and separate source/target metadata.
3. Add runtime-safe SQLite snapshot creation using stdlib `sqlite3.Connection.backup()` through `asyncio.to_thread()`.
4. Add count-based retention.
5. Add `src/eggpool/background/backup.py` and tests for the loop's one-iteration helper if implemented.
6. Wire `automatic_backup` into `app.py` behind config gates.
7. Update deployment docs and, if needed, systemd `ReadWritePaths` depending on chosen backup directory policy.
8. Reconcile or clearly document the existing `deploy backup-cron` path.
9. Run `ruff`, `pyright`, and the targeted pytest suite.

## Acceptance criteria

A default server start with a file-backed DB registers an `automatic_backup` supervised task.

The first automatic backup attempt occurs only after `startup_delay_s`, not during migrations/startup.

The task creates restore-compatible `eggpool-backup-*.zip` archives every `interval_s` seconds.

Runtime-created backup archives contain a consistent SQLite snapshot and can be restored by the existing restore path.

The selected backup directory retains only the newest `retain_count` lifecycle backup archives.

Backup failure does not stop the server or poison unrelated background tasks.

No external `sqlite3` binary is required for in-process automatic backups.

Production systemd hardening and the automatic backup default directory are compatible.

Tests cover config defaults, disabled modes, retention, archive atomicity, SQLite snapshot consistency, and app registration.
