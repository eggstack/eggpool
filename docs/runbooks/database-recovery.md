# Database Failure and Restart — Operator Runbook

EggPool treats SQLite integrity and indeterminate runtime connection failures
as local process failures. The worker closes admission and exits nonzero;
systemd restarts it. Startup then runs migrations, `PRAGMA quick_check`, crash
reconciliation, and the initial writable probe before readiness is reopened.

There is no same-process replacement-connection recovery and no automatic
database deletion, salvage, vacuum, or in-place repair.

## Normal restart contract

```bash
systemctl restart eggpool
curl -sS http://localhost:PORT/readyz
```

During startup, `/readyz` remains unavailable. A healthy process returns a
normal readiness response only after integrity and startup repair succeed.
Requests left pending by the previous process are repaired from durable
request/attempt/reservation identities. A client request is not retried across
the restart.

## Failure categories

- **Busy/locked:** bounded SQLite lock contention. The configured SQLite busy
  timeout applies; this is not provider failure and does not create provider
  backoff.
- **Disk/full/read-only:** local service failure. Free space or correct the
  filesystem permissions, then restart.
- **Corruption/not-a-database:** startup-fatal. Keep the service stopped and
  restore a known-good backup after preserving the failed database for
  investigation.
- **Indeterminate commit/rollback/connection invalidation:** readiness closes
  and the worker exits so systemd can restart it.

## Corruption or failed integrity check

If logs report a non-`ok` `PRAGMA quick_check` result or an integrity-check
exception:

1. Stop the service: `systemctl stop eggpool`.
2. Preserve the database and its `-wal`/`-shm` files for diagnosis.
3. Restore a known-good backup or follow the project recovery procedure.
4. Start the service and verify readiness.

Do not delete, vacuum, or modify the suspect database as an automatic fix.

## Disk, permissions, or external lock

Check the database volume, ownership, and external processes holding the file:

```bash
df -h /var/lib/eggpool
ls -l /var/lib/eggpool/eggpool.db
fuser /var/lib/eggpool/eggpool.db
```

Resolve the local condition and restart. If a lock is held by a maintenance
process, stop that process or wait for it to finish; do not increase layered
application retries beyond the SQLite busy timeout.

## Backups and restore

Create backups with the supported command:

```bash
eggpool backup --output-dir /var/backups/eggpool/
```

To restore, stop EggPool, copy a known-good backup into the configured data
directory, start the service, and verify `/readyz`. Startup integrity checks
are the final gate before traffic is accepted.

## Evidence to collect

Capture the bounded startup/worker log reason, service status, database file
metadata, free space, and the last readiness response. Do not include API
keys, request bodies, or credentials in an incident report.
