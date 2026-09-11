# Deep Dive: Backup, Restore, and Uninstall

Back to [Architecture](README.md)

`rust/src/operations/backup.rs` and `rust/src/operations/deploy.rs` own
backup, restore, deployment, recovery, and uninstall behavior. They operate on
the resolved configuration, environment file, SQLite state, runtime paths, and
managed service files without depending on a repository-local application.

## Backup and restore

Backups are timestamped archives with bounded metadata and a consistent SQLite
snapshot. Publication is staged and atomic; partial archives are not presented
as complete backups. Restore validates metadata, requires the service to be
stopped, restores the selected state, and leaves restart responsibility
explicit to the operator or service manager.

## Uninstall and recovery

Uninstall detects the owning package-manager or standalone installation before
removing managed artifacts. It requires confirmation for destructive actions,
checks for a running service, and preserves operator configuration/database
paths unless the operator explicitly selects their removal. Recovery repairs
only states covered by the startup reconciliation contract.

## Safety invariants

- no database reset or schema downgrade is implicit;
- package-manager ownership is never guessed;
- managed service and path changes are atomic where possible;
- failures report an actionable recovery path without exposing secrets.
