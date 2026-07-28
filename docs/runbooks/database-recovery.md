# Database Connection Recovery — Operator Runbook

Plan 027 — bounded recovery from indeterminate SQLite transaction outcomes.

## What is Database Connection Recovery?

When SQLite reports an indeterminate commit outcome (the commit raised but the transaction state is unknown), Eggpool automatically:

1. Detaches and closes the suspect connection
2. Opens a replacement connection
3. Reconciles any ambiguous operations
4. Restores write admission and readiness

This happens without process restart or database deletion. The recovery controller is process-owned — it survives generation swaps (rehash).

## Lifecycle States

| State | Meaning |
|-------|---------|
| `ready` | Normal operation, writes and reads admitted |
| `invalidated` | Connection marked suspect, admission stopped |
| `recovering` | Replacement connection being opened |
| `reconciling` | Ambiguous operations being resolved |
| `failed_closed` | All recovery attempts exhausted, writes rejected |
| `shutting_down` | Process shutting down, no new recovery attempts |

## Configuration

All fields in `[database.recovery]` are live-reloadable via `eggpool rehash`.

```toml
[database.recovery]
enabled = true                     # Master switch (default: true)
max_attempts = 5                   # Retry attempts before failed_closed (1–20)
initial_backoff_ms = 100           # First retry delay in ms (0–10000)
max_backoff_ms = 5000              # Backoff cap in ms (0–60000)
reconciliation_timeout_s = 30      # Max seconds per ambiguous op (0–600)
fail_process_on_exhaustion = false # Exit process after retries fail
```

Defaults are production-ready. `fail_process_on_exhaustion` is an operator escape hatch — do not enable casually; the default systemd unit does not use it.

## Observing Recovery Events

### Diagnostics Endpoint

The database exposes recovery state in its diagnostics dict:

```
GET /internal/diagnostics
```

Key fields:

| Field | Description |
|-------|-------------|
| `lifecycle_state` | Current state string (`ready`, `invalidated`, etc.) |
| `connection_epoch` | Incremented on every successful reconnect |
| `recovery_count` | Total successful recoveries since process start |
| `pending_ambiguous_operations` | Number of operations awaiting reconciliation |
| `writes_admitted` | Whether new writes are allowed |
| `reads_admitted` | Whether read-only stats queries are allowed |

### Recovery Controller Snapshot

The recovery controller exposes a richer snapshot via `app.state.recovery_controller.snapshot()`:

| Field | Description |
|-------|-------------|
| `recovery_attempts` | Total attempts (including failed) |
| `successful_recoveries` | Successful recoveries |
| `failed_recoveries` | Failed attempts |
| `last_attempt.error_class` | Exception class from last failure |
| `last_attempt.error_message` | Exception message from last failure |
| `active_recovery` | Whether a recovery attempt is in progress |
| `active_waiters` | Number of tasks waiting for recovery |
| `failed_closed_reason` | Why the controller gave up |
| `time_to_recover_s` | Duration of last recovery cycle |

### Readiness Probe

`/readyz` returns **503** during recovery:

```json
{"status":"degraded","reason":"database recovery invalidated"}
{"status":"degraded","reason":"database recovery failed_closed"}
```

The orchestrator should stop routing traffic to a process in recovery.

## Common Scenarios

### 1. Normal Automatic Recovery

**Symptom:** Brief 503 on `/readyz`, then recovery. Requests resume automatically.

**Resolution:** None. Verify recovery occurred:

```bash
# Check diagnostics for recovery_count > 0
curl -s http://localhost:PORT/internal/diagnostics | jq .database.recovery_count
```

Recovery is typically sub-second. If it persists, check underlying I/O health.

### 2. Disk Full

**Symptom:** Recovery fails repeatedly. Controller enters `failed_closed`. `/readyz` returns 503 indefinitely.

**Resolution:**

1. Free disk space on the volume containing the database file
2. Apply recovery config change to trigger re-evaluation:
   ```bash
   eggpool rehash
   ```
3. If `failed_closed` persists, restart the process:
   ```bash
   systemctl restart eggpool
   ```
4. Verify: check `recovery_count` increased and `lifecycle_state` is `ready`

### 3. Permissions Changed

**Symptom:** `failed_closed` after a file permission or ownership change on the database file or its directory.

**Resolution:**

1. Stop the process
2. Fix permissions:
   ```bash
   # Database file should be owned by the eggpool service user
   chown eggpool:eggpool /path/to/eggpool.db
   chmod 644 /path/to/eggpool.db
   ```
3. Restart the process — recovery validates schema on startup
4. If the database was replaced out-of-band, check schema version:
   ```sql
   SELECT MAX(version) FROM _migrations;
   ```

### 4. Database Locked by External Process

**Symptom:** `busy_timeout` errors, possible connection invalidation. Recovery may succeed once the external process releases the lock.

**Resolution:**

1. Identify the external process holding the lock:
   ```bash
   fuser /path/to/eggpool.db
   ```
2. Stop or reconfigure the external process
3. Recovery is automatic once the lock is released
4. If recovery failed, restart the process

### 5. Corruption / Integrity Failure

**Symptom:** `PRAGMA quick_check` returns anything other than `ok` at startup. Recovery may fail with schema verification errors.

**Resolution:**

1. Stop the process
2. Restore from backup (see Backup/Restore below)
3. Do **not** attempt to repair the database in-place — use a known-good backup

### 6. Failed Migration

**Symptom:** Startup fails with a migration error. Schema version in the database is lower than expected.

**Resolution:**

1. Check current schema version:
   ```sql
   SELECT MAX(version) FROM _migrations;
   ```
2. Check expected schema version in the codebase
3. If the database was partially migrated, restore from backup
4. If the database was replaced with an older copy, restore from backup

### 7. Backup and Restore

**Creating backups:**

```bash
# Consistent backup (recommended)
eggpool backup --output-dir /var/backups/eggpool/

# Automatic backups are run by the background task if configured
```

**Restoring from backup:**

1. Stop the process:
   ```bash
   systemctl stop eggpool
   ```
2. Replace the database file:
   ```bash
   cp /var/backups/eggpool/eggpool-YYYY-MM-DD.db /var/lib/eggpool/eggpool.db
   ```
3. Start the process — recovery will validate the schema on startup
4. Verify readiness:
   ```bash
   curl -s http://localhost:PORT/readyz
   ```

### 8. Why Not Delete the Database?

Deleting `eggpool.db` removes all request history, usage data, model info, and quarantine state. This is **not** a normal recovery step.

Recovery is designed to handle transient failures safely. Deletion is a last resort when:

- All other recovery methods have been attempted
- You have confirmed persistent corruption that backup restore also cannot fix
- You accept the loss of accumulated state

Always prefer backup restore over deletion.

## Escalation Path

When automatic recovery fails (`failed_closed`):

1. **Check diagnostics** — read `lifecycle_state`, `failed_closed_reason`, `last_attempt.error_class`, `recovery_count`
2. **Check disk and permissions** — the most common causes
3. **Check for external processes** — `fuser`, `lsof`, or `sqlite3` locks
4. **Restart the process** — `systemctl restart eggpool`
5. **Restore from backup** — if restart fails and schema is corrupt
6. **File a support ticket** — include the diagnostics snapshot, log output, and any `last_attempt.error_message`

## Configuration Reference

| Field | Type | Default | Range | Description |
|-------|------|---------|-------|-------------|
| `enabled` | bool | `true` | — | Master switch for recovery |
| `max_attempts` | int | `5` | 1–20 | Maximum recovery attempts |
| `initial_backoff_ms` | int | `100` | 0–10000 | Initial backoff between attempts |
| `max_backoff_ms` | int | `5000` | 0–60000 | Maximum backoff (doubles each attempt) |
| `reconciliation_timeout_s` | float | `30.0` | 0–600 | Timeout per ambiguous operation |
| `fail_process_on_exhaustion` | bool | `false` | — | Exit process when retries fail |

All fields are live-reloadable via `eggpool rehash`. The config section `[database.recovery]` can be applied without restart.
