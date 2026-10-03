# Deep Dive: Backup, Restore, and Uninstall

Back to [Architecture](README.md). See also [overview.md §11](overview.md), [Database](deep-dive-database.md), and [Deployment](deep-dive-deployment.md).

## Ownership

`rust/src/operations/backup.rs` and `rust/src/operations/deploy.rs` own backup, restore, deployment, recovery, and uninstall, with install provenance in `provenance.rs` and the installable-release catalog in `catalog.rs`. They operate on resolved configuration, environment file, SQLite state, runtime paths, and managed service files. Systemd/logrotate/cron rendering and the update executor live in the deployment deep dive; this file owns the backup/restore/uninstall/provenance/catalog contracts.

## Backup and restore (`backup.rs`)

`BackupService::from_config` captures the resolved config path, the runtime SQLite path, the adjacent `.env`, and the output directory (`[backup].directory` or `default_backup_dir`). Archive constants: `BACKUP_FORMAT_VERSION = 1`, `META` / `config.toml` / `.env` / `usage.sqlite3` basenames, bounded member/total sizes, 60s snapshot budget (`BACKUP_TIMEOUT`):

- Create: validate config/database sources (and `.env` only when `[backup].include_env`), ensure a private output directory, snapshot via SQLite's online `backup_to()` into a private staging dir, validate the snapshot (`quick_check` on a read-only open), then `write_archive`: a staged atomic `Stored` (uncompressed) ZIP with `META` TOML plus config/database/env members at owner-only `0600` member permissions, finalized create-new (temp `create_new(true)` plus hard-link/rename to `eggpool-backup-YYYYMMDD-HHMMSS.zip` with numeric collision suffixes). Staging is always removed; `prune()` retention runs best-effort only after successful publication.
- Validate/restore: `validate_archive` / `validated_archive` / `prepare_restore` reject central-directory duplicates (bounded EOCD scan, since `zip` deduplicates its name view), member-count/type/size bounds, `META` manifest mismatches, unsafe absolute targets, symlink ancestors, and staged config/database failures before any live path is touched. `validated_archive` additionally allowlists `META` targets against the live service paths and fails closed when live paths are unknown.
- `recover` requires the caller to have stopped the service before replacement; the stopped-service workflow lives in CLI/lifecycle (`rust/src/runtime.rs::recover` stops via `stop()` before calling `recover`, and never restarts after success) and is not enforced inside `backup.rs` itself. `atomic_restore` writes config/database (and env) via owner-only `0o600` temp-plus-rename `write_atomic`, revalidates staged bytes first, and rolls back atomically (`RestoreRollback` retains original state on failure).
- See `docs/backup-restore.md` for the operator contract.

## Uninstall and recovery (`deploy.rs`)

`uninstall()` (`UninstallTargets`, `KeepFlags { data, config, path, deploy_artifacts }`, `--yes` / `--keep-data` / `--keep-config` / `--keep-path` / `--deploy-artifacts`) detects the owning package-manager or standalone installation via `provenance.rs` before removing managed artifacts and never guesses ownership. It requires confirmation, stops the owned service first, removes only explicitly resolved EggPool targets (atomic filesystem operations where possible), and preserves operator configuration/database paths unless explicitly selected. `remove_owned_tree` / `remove_known_file` / `remove_empty_directory` bound each removal. Managed cron/systemd/logrotate install and removal use pure renderers (`render_personal_systemd`, `render_production_systemd`, `render_logrotate`, `render_watchdog_cron`, `render_backup_cron`, `render_backup_script`) plus `write_atomic()` and argv-based commands (`resolve_rust_binary` pins the Rust executable). Recovery repairs only states covered by the startup reconciliation contract; there is no implicit database reset or schema downgrade.

## Install provenance (`provenance.rs`)

`InstallProvenance::detect()` works from the executable path and on-disk distribution metadata (`PackageMetadata`, `DirectUrlMetadata`), never from bare `PATH` names. Variants: `UvTool`, `Pipx`, `PipEnvironment`, `StandaloneRust`, `SourceCheckout`, `Ambiguous` (bounded secret-free evidence, `MAX_EVIDENCE_ITEMS`/`MAX_EVIDENCE_BYTES`). Ownership answers gate update mutation and uninstall scope.

## Release catalog (`catalog.rs`)

`ReleaseCatalog::embedded()` loads `rust/assets/catalog/installable-releases.json` (`release-catalog.v1`). `CatalogRelease` carries `ReleaseVersion`, `ReleaseEra` (`Python` historical / `Rust` current), package/Python requirements, `yanked`/`unavailable`, `supported_target_classes`, and `rollback_compatible`. `resolve()` honors `ReleaseTarget::Latest` (rust-only latest stable) vs `ReleaseTarget::Exact` (explicit version; historical Python only through an explicit package-manager request), rejects yanked/unavailable entries, and gates Rust releases by platform target class. `operations/update.rs` (separate Hyper/Rustls owner, `NATIVE_RELEASE_VERSION = "0.8.1"`: GitHub release discovery via HTTPS-only `ReleaseClient` with loopback-HTTP test overrides, `MAX_REDIRECTS = 3` trusted-redirect fetches, SHA-256 verification, catalog-gated atomic transition through `PackageTransitionService` with rollback, explicit exact-version resolution; conservative check-only background freshness probes via `UpdateCheckerState::check_once` over `check_info`) consumes this catalog.

## Safety invariants

- No database reset or schema downgrade is implicit.
- Package-manager ownership is never guessed.
- Managed service and path changes are atomic where possible.
- Partial archives are never presented as complete.
- Restore targets must equal the live configuration paths.
- Failures report an actionable recovery path without exposing secrets.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test database_compatibility -- --test-threads=1
```
