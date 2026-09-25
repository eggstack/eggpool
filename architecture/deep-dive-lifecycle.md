# Deep Dive: Backup, Restore, and Uninstall

Back to [Architecture](README.md)

`rust/src/operations/backup.rs` and `rust/src/operations/deploy.rs` own
backup, restore, deployment, recovery, and uninstall behavior, with install
provenance in `rust/src/operations/provenance.rs` and the installable-release
catalog in `rust/src/operations/catalog.rs`. They operate on
the resolved configuration, environment file, SQLite state, runtime paths, and
managed service files without depending on a repository-local application.
Systemd/logrotate/cron rendering and the update executor live in the
[deployment deep dive](deep-dive-deployment.md); this file owns the
backup/restore/uninstall/provenance/catalog contracts.

## Backup and restore (`backup.rs`)

`BackupService` captures the resolved config path, the runtime SQLite path,
the adjacent `.env`, and the output directory (`[backup].directory` or the
default backup dir). Archive constants: `BACKUP_FORMAT_VERSION = 1`,
`META` / `config.toml` / `.env` / `usage.sqlite3` basenames, bounded member
sizes and total bytes, 60s snapshot budget.

- Create: validate config/database (and `.env` only when
  `[backup].include_env`) sources, ensure a private output directory, snapshot
  the database through SQLite's online `backup_to()` into a private staging
  dir, validate the snapshot (`validate_sqlite_snapshot`), then publish via
  `write_archive()`: a `Stored` (uncompressed) ZIP with `META` TOML plus
  config/database/env members, finalized create-new
  (`create_new(true)` temp then rename, `eggpool-backup-YYYYMMDD-HHMMSS.zip`
  with numeric collision suffixes). Partial archives are never presented as
  complete; staging is always removed. `prune()` retention is best-effort
  after successful publication.
- Validate/restore: `validate_archive()` / `validated_archive()` /
  `prepare_restore()` check central-directory duplicates, member bounds,
  manifest, targets, staged config, and staged database before any target
  path is touched. `recover()` requires the caller to have stopped the
  service; `atomic_restore()` writes config/database (and env targets) via
  owner-only `0o600` atomic replace and revalidates, returning
  `RestoreRollback` with the original state retained on failure.
- See `docs/backup-restore.md` for the operator contract.

## Uninstall and recovery (`deploy.rs`)

`uninstall()` (`UninstallTargets`, `KeepFlags`, `cli.rs::UninstallArgs`
`--yes` / `--keep-data` / `--keep-config` / `--keep-path` /
`--deploy-artifacts`) detects the owning package-manager or standalone
installation via `provenance.rs` before removing managed artifacts and never
guesses ownership. It requires confirmation for destructive actions, checks
for a running service, removes only explicitly resolved EggPool targets with
atomic filesystem operations where possible, and preserves operator
configuration/database paths unless the operator explicitly selects their
removal. Managed cron/systemd/logrotate install and removal use pure
renderers (`render_personal_systemd`, `render_production_systemd`,
`render_logrotate`, `render_watchdog_cron`, `render_backup_cron`) plus
`write_atomic()` and argv-based commands. Recovery repairs
only states covered by the startup reconciliation contract.

## Install provenance (`provenance.rs`)

`InstallProvenance::detect()` works from the executable path and on-disk
distribution metadata (`PackageMetadata`, `DirectUrlMetadata`), never from
bare `PATH` names. Variants: `UvTool`, `Pipx`, `PipEnvironment`,
`StandaloneRust`, `SourceCheckout`, `Ambiguous` (bounded secret-free
evidence). Ownership answers gate update mutation and uninstall scope.

## Release catalog (`catalog.rs`)

`ReleaseCatalog::embedded()` loads
`rust/assets/catalog/installable-releases.json` (`release-catalog.v1`).
`CatalogRelease` carries `ReleaseVersion`, `ReleaseEra` (`Python` historical
/ `Rust` current), package/Python requirements, `yanked`/`unavailable`,
`supported_target_classes`, and `rollback_compatible`. `resolve()` honors
`ReleaseTarget::Latest` (rust-only latest stable) vs `ReleaseTarget::Exact`
(explicit version; historical Python only through an explicit
package-manager request), rejects yanked/unavailable entries, and gates Rust
releases by platform target class. Latest/default resolution is Rust-only.

## Safety invariants

- no database reset or schema downgrade is implicit;
- package-manager ownership is never guessed;
- managed service and path changes are atomic where possible;
- failures report an actionable recovery path without exposing secrets.
