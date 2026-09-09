# O006 Closure — Database Backup, Recovery, and Automatic Backup

Status: closed

Implementation commit: [`6af52fb`](https://github.com/eggstack/eggpool/commit/6af52fb)

Plan: [O006 — database, backup, recovery, and automatic backup](../../implementation/operations/006-database-backup-recovery-and-automatic-backup.md)

## Acceptance summary

O006 is implemented in the Rust candidate. The four owned command paths now
execute Rust services, use the canonical F004 migration source, and share one
M8 process supervisor callback for automatic backups. No Python fallback,
schema fork, reset-on-error path, second scheduler, or public control surface
was added.

## Archive contract

| Contract | Rust implementation | Evidence |
|---|---|---|
| Format and naming | ZIP format version 1, `ZIP_STORED`, `eggpool-backup-YYYYMMDD-HHMMSS[-N].zip` | `operations::backup::write_archive`; `operations_o006::backup_uses_sqlite_snapshot_and_collision_safe_stored_archive` |
| Members | `META`, `config.toml`, `usage.sqlite3`, and optional `.env`; member names are fixed and absolute host paths stay in metadata only | O006 archive test; O001 member contract |
| SQLite consistency | Existing serialized `Database` connection uses rusqlite online backup API into a private staging file; raw live-WAL copying is not used | `Database::backup_to`; O006 archive test |
| Publication and retention | Private staging, synced archive, create-new collision-safe hard-link publication; newest matching archives are retained after successful backup | `BackupService::create` and `prune` |
| Permissions | Backup/staging directories are mode `0700`; archive, staged files, config, env, and restored DB are mode `0600` on Unix | `set_private_dir`/`set_private_file`; O006 service tests |
| Metadata | Format version, UTC creation timestamp, install method, reviewed restore targets, and exact member manifest | `metadata_text`; archive validation |

The Rust candidate intentionally does not copy `-wal`/`-shm` sidecars when an
online SQLite snapshot is available: their state is incorporated into the
validated snapshot. This preserves the frozen restore shape without restoring
stale journal state.

## Migration and vacuum parity

`migrate` opens the configured database through the existing serialized DB
layer, verifies the embedded canonical migration checksums, applies only
pending numbered migrations transaction-by-transaction, reports applied/current
schema values, and never resets the database. `db vacuum` uses the same
connection gate and configured SQLite busy timeout through the dedicated
`Database::vacuum` method, with guaranteed close handling on success/failure.

Fresh/current/partial/checksum and busy/maintenance behavior remains owned by
the F004 database layer; O006 adds no schema or downgrade engine.

## Restore safety and fault matrix

| Fault/state | Result |
|---|---|
| Missing, corrupt, truncated, wrong-version, malformed-META archive | Rejected before target selection or server stop |
| Traversal, absolute/unknown/duplicate member, symlink/special member, archive bomb, or manifest mismatch | Rejected by fixed allowlist, member caps, mode checks, and exact manifest comparison |
| Invalid staged config or SQLite integrity/migration ledger incompatibility | Rejected before final paths are touched |
| Running server | Validated first, then stopped through O003 identity-gated lifecycle; recovery never auto-restarts it |
| Replacement failure | Existing targets are copied to private safety state; deterministic config/env/database replacement is compensated, and safety state is retained if compensation fails |
| Successful replacement | New config and database are revalidated, permissions restored, then safety state is removed |
| Repeated backup timestamp | Numeric suffix is selected without overwriting an existing archive |

The focused Rust suite covers valid restore, traversal rejection, corrupt
database rejection through staged SQLite validation, and collision handling.
The implementation uses private temporary files with create-new allocation and
atomic target rename; no archive member is extracted as an arbitrary path.

## Automatic task registration

`ProcessRuntime::with_config_path_and_config` registers the real
`automatic_backup` callback in the existing `TaskCallbackRegistry`. The
callback re-reads the current config per tick, calls `BackupService::create`,
and runs retention only after successful publication. Task enablement,
interval, startup delay, reload staging, singleton execution, cancellation,
and shutdown remain M8 `RuntimeTaskSupervisor` behavior. The capability
inventory reports `automatic_backup` registered; `metrics_flush` and
`update_checker` remain the only deferred M9 callbacks.

Evidence: `operations_o006::vacuum_and_automatic_backup_use_existing_database_and_supervisor_boundaries`, the existing R006/R008/R011-R013 task/reload suites, and the supervisor's staged-diff tests.

## Dependency and security review

The only new dependency is `zip = 2.4.2` with default features disabled. It is
used solely for stored ZIP create/read; encryption, compression codecs, and
general filesystem helpers are not enabled. SQLite backup support is the
existing `tokio-rusqlite` crate with its `backup` feature. No new database
tables, network client, scheduler, or subprocess was introduced.

The archive reader rejects non-reviewed names, directories, encryption,
duplicates, special-file modes, oversized members, invalid manifests, unsafe
metadata targets, symlink-controlled paths, and corrupt SQLite content. Error
messages retain only bounded categories and do not print archive bodies,
configuration contents, API keys, or proxy credentials. Config/env restore
permissions are private, and failed backup publication removes its temporary
file.

## Verification evidence

Commands actually run:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings   PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --lib and every rust/tests/*.rs binary serially PASS
rtk uv run pytest tests/unit/test_lifecycle_backup.py tests/unit/test_background_backup.py tests/integration/test_database_maintenance.py tests/migration_rs -q --tb=short --maxfail=1 PASS (173 passed, 3 skipped)
rtk git diff --check                                                            PASS
```

The aggregate Rust command was run with each test binary serially because the
existing O004 tests share a process-global mutation lock across binaries; the
deterministic serial aggregate passed. A parallel `cargo test --all-targets`
attempt was not used as closure evidence because that pre-existing test
isolation race caused two O004 test failures unrelated to O006.

## Unresolved findings and planning transition

No unresolved O006 correctness, security, data-loss, compatibility, resource,
or lifecycle finding remains. Broad OS/SBC qualification, live-provider tests,
deployment artifacts, update behavior, M9 aggregate qualification, Rust public
distribution, and Python retirement remain later-plan scope.

O006 is removed from the ready queue and recorded as closed. O007 is promoted
to the sole dependency-ready plan because its hard dependency is now accepted.
O008, O009, and O010 remain queued behind their direct predecessors. M10 is not
unblocked by O006; it still requires accepted O010 closure.

