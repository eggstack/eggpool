# EggPool 503 Bug Fix — Implementation Plan

## Problem Summary

Streaming request finalization fails under client-disconnect + DB-lock-contention, causing requests to leak as `pending` with `active` reservations. These accumulate, slow down cleanup queries, and saturate the single SQLite connection lock — producing 503s after 5–10 minutes. A restart does not help because leaked state persists in the database file.

---

## 1. Files to Modify

| File | Change |
|------|--------|
| `src/eggpool/app.py` | Add `_finalize_stale_requests` background task; harden `_crash_recovery` |
| `src/eggpool/request/coordinator.py` | Shield + timeout streaming finalization; always close upstream response |
| `src/eggpool/db/schema/002_add_pending_index.sql` | New migration: index on `requests(status, started_at)` |
| `src/eggpool/db/migrations.py` | Ensure new migration is picked up |
| `docs/deployment.md` | Document the new background task and tuning knobs |

---

## 2. Detailed Changes

### 2.1 `src/eggpool/app.py`

#### 2.1.1 Harden `_crash_recovery` (startup)

**Current behavior:** Only recovers requests older than 5 minutes.

**New behavior:** Recover **all** pending requests and **all** active reservations at startup. A process restart is a definitive signal that in-flight work is dead.

```python
async def _crash_recovery(db: Database) -> None:
    # Mark ALL stale pending requests as interrupted, release ALL active reservations.
    #
    # A process restart is a hard boundary: any request that was pending
    # in the previous process is definitively dead.  We do NOT time-gate
    # this recovery so that leaked requests from the previous run are
    # cleaned up regardless of how recently they were created.

    # Collect affected account_ids before recovery
    affected = await db.fetch_all(
        "SELECT DISTINCT account_id FROM requests WHERE status = 'pending' "
        "UNION "
        "SELECT DISTINCT account_id FROM reservations WHERE status = 'active'"
    )
    affected_account_ids = [int(row["account_id"]) for row in affected]

    async with db.transaction():
        # Recover ALL pending requests (no time threshold)
        stale_requests = await db.execute_write(
            "UPDATE requests SET status = 'interrupted', "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE status = 'pending'",
            (),
        )
        # Release ALL active reservations (no time threshold)
        stale_reservations = await db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'crash_recovery' "
            "WHERE status = 'active'",
            (),
        )
        # Finalize ALL incomplete attempts (no time threshold)
        await db.execute_write(
            "UPDATE request_attempts SET "
            "completed_at = CURRENT_TIMESTAMP, error_class = 'process_interrupted' "
            "WHERE completed_at IS NULL",
            (),
        )

    # Record recovery events
    if affected_account_ids:
        event_repo = AccountEventRepository(db)
        for account_id in affected_account_ids:
            await event_repo.record(
                account_id=account_id,
                event_type="crash_recovery",
                details='{"action": "marked_interrupted", "reason": "startup_recovery"}',
            )
        logger.info(
            "Crash recovery: marked %d stale requests, released %d reservations, "
            "recorded events for %d accounts",
            stale_requests,
            stale_reservations,
            len(affected_account_ids),
        )
    else:
        logger.info("Crash recovery: no stale requests found")
```

#### 2.1.2 Add `_finalize_stale_requests` background task

Register a new periodic task that runs every **60 seconds** and force-finalizes any request that has been `pending` longer than the upstream read timeout (default 300s / 5 minutes).

```python
async def _finalize_stale_requests(
    db: Database,
    router: Router,
    quota_estimator: QuotaEstimator,
    max_pending_seconds: float = 300.0,
) -> None:
    # Periodic task: finalize requests that have been pending too long.
    #
    # This is a safety net for streaming requests whose finalizer never
    # ran (e.g. client disconnect + cancellation timeout killed the
    # generator task before finalize() could acquire the DB lock).
    #
    # Args:
    #     db: The primary (write) database connection.
    #     router: For decrementing active request counts.
    #     quota_estimator: For removing in-memory reservation tracking.
    #     max_pending_seconds: How long a request may stay pending before
    #         it is considered leaked.  Default matches the upstream
    #         read_timeout so legitimate long-running requests are
    #         never touched.

    while True:
        await asyncio.sleep(60)
        try:
            threshold = f"-{int(max_pending_seconds)} seconds"
            async with db.transaction():
                # Find leaked pending requests
                rows = await db.execute_returning(
                    "SELECT r.id, r.account_id, a.name AS account_name, "
                    "       res.id AS reservation_id, res.reserved_microdollars "
                    "FROM requests r "
                    "JOIN accounts a ON a.id = r.account_id "
                    "LEFT JOIN reservations res "
                    "    ON res.request_id = r.id AND res.status = 'active' "
                    "WHERE r.status = 'pending' "
                    "  AND r.started_at < datetime('now', ?)",
                    (threshold,),
                )
                transitioned = [dict(row) for row in rows]
                if not transitioned:
                    continue

                request_ids = [r["id"] for r in transitioned]
                reservation_ids = [
                    r["reservation_id"] for r in transitioned
                    if r["reservation_id"] is not None
                ]

                # Mark requests interrupted
                placeholders = ",".join("?" * len(request_ids))
                await db.execute_write(
                    f"UPDATE requests "
                    f"SET status = 'interrupted', "
                    f"    completed_at = CURRENT_TIMESTAMP, "
                    f"    error_class = 'StaleRequestFinalizer' "
                    f"WHERE id IN ({placeholders}) "
                    f"  AND status = 'pending'",
                    tuple(request_ids),
                )

                # Release associated reservations
                if reservation_ids:
                    res_placeholders = ",".join("?" * len(reservation_ids))
                    await db.execute_write(
                        f"UPDATE reservations "
                        f"SET status = 'released', "
                        f"    released_at = CURRENT_TIMESTAMP, "
                        f"    release_reason = 'stale_request' "
                        f"WHERE id IN ({res_placeholders}) "
                        f"  AND status = 'active'",
                        tuple(reservation_ids),
                    )

            # Post-commit: reconcile runtime state
            seen_accounts: set[str] = set()
            for row in transitioned:
                account_name = row.get("account_name")
                if not account_name or account_name in seen_accounts:
                    continue
                seen_accounts.add(account_name)

                # Decrement active request count (idempotent if already 0)
                await router.decrement_active_request_count(account_name)

                # Remove in-memory reservation tracking
                reserved = row.get("reserved_microdollars", 0)
                if reserved and quota_estimator:
                    await quota_estimator.remove_reservation(
                        account_name, int(reserved)
                    )

            logger.info(
                "Stale request finalizer: cleaned up %d leaked requests",
                len(transitioned),
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale request finalizer failed")
```

**Registration** (in `_lifespan_runtime`, alongside other background tasks):

```python
# Register stale request finalizer (runs every 60s)
async def _stale_request_loop() -> None:
    await _finalize_stale_requests(
        db=db,
        router=router,
        quota_estimator=router.quota_estimator,
        max_pending_seconds=config.upstream.read_timeout or 300.0,
    )

supervisor.register("stale_request_finalizer", _stale_request_loop)
```

---

### 2.2 `src/eggpool/request/coordinator.py`

#### 2.2.1 Shield + timeout streaming finalization

In `_build_stream_generator`, the `CancelledError` handler must not be killed by ASGI cancellation while it holds the DB lock. Wrap the finalize call in `asyncio.shield()` with a short timeout.

**Current code (inside `_build_stream_generator`):**

```python
except asyncio.CancelledError:
    observer.flush()
    usage_result = observer.usage
    if not context.client_metadata.get("_cancelled_finalized"):
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.CLIENT_CANCELLED,
                ...
            ),
        )
    raise
```

**New code:**

```python
except asyncio.CancelledError:
    observer.flush()
    usage_result = observer.usage
    if not context.client_metadata.get("_cancelled_finalized"):
        try:
            # Shield from ASGI task cancellation and cap wait time.
            # If the DB lock is heavily contended we still want to
            # finalize, but we cannot block the event loop forever.
            await asyncio.wait_for(
                asyncio.shield(
                    finalizer.finalize(
                        selected,
                        FinalizationData(
                            outcome=FinalizationOutcome.CLIENT_CANCELLED,
                            first_byte_ms=(
                                int(first_byte_ms) if first_byte_ms > 0 else None
                            ),
                            upstream_latency_ms=int(
                                (time.monotonic() - reference) * 1000
                            ),
                            bytes_emitted=bytes_emitted,
                            input_tokens=usage_result.input_tokens,
                            output_tokens=usage_result.output_tokens,
                            cache_read_tokens=usage_result.cache_read_tokens,
                            cache_write_tokens=usage_result.cache_creation_tokens,
                            reasoning_tokens=usage_result.reasoning_tokens,
                            thinking_characters=usage_result.thinking_characters,
                            bytes_received=len(context.original_body),
                        ),
                    )
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Finalizer timed out for cancelled stream %s; "
                "request %s may leak as pending",
                context.request_id,
                selected.db_request_id,
            )
        except Exception:
            logger.exception(
                "Finalizer failed for cancelled stream %s",
                context.request_id,
            )
    raise
```

#### 2.2.2 Always close upstream response in `_execute_streaming`

**Current code:**

```python
finally:
    if response is not None and (
        response.status_code >= 400 or not generator_created
    ):
        await response.aclose()
```

**New code:**

```python
finally:
    # Always close the upstream response.  When generator_created is
    # True the generator's own finally block also closes it, but
    # double-close is harmless (httpx ignores it) and this guards
    # the case where the generator is never started (client
    # disconnects before first chunk).
    if response is not None:
        try:
            await response.aclose()
        except Exception:
            logger.debug("Error closing upstream response", exc_info=True)
```

---

### 2.3 `src/eggpool/db/schema/002_add_pending_index.sql`

New migration file. The migration runner (`MigrationRunner`) picks up files in `db/schema/` ordered by filename, so `002_` follows `001_initial.sql`.

```sql
-- Migration 002: Index for stale-request queries
--
-- The crash_recovery, reconcile_expired_reservations, and the new
-- stale-request finalizer all scan requests by status + started_at.
-- Without an index this is a full table scan; with many leaked rows
-- the scan holds the connection lock long enough to starve new
-- requests.

CREATE INDEX IF NOT EXISTS idx_requests_status_started
ON requests(status, started_at);
```

**Verify `MigrationRunner` picks it up:** The existing runner should already glob `db/schema/*.sql` and run them in filename order. If it does not, update `migrations.py` to include this file explicitly.

---

### 2.4 `src/eggpool/db/migrations.py` (if needed)

If the migration runner does not auto-discover new files, add an explicit entry:

```python
MIGRATIONS: list[tuple[str, str]] = [
    ("001_initial", "001_initial.sql"),
    ("002_add_pending_index", "002_add_pending_index.sql"),
]
```

*(Only needed if the current runner uses a hardcoded list instead of filesystem globbing.)*

---

### 2.5 `docs/deployment.md`

Add a new subsection under "Operational Monitoring":

```markdown
### Leaked Request Detection

If the proxy starts returning 503s after running successfully for
several minutes, check for leaked pending requests:

```bash
sqlite3 ~/.eggpool/eggpool.db "SELECT COUNT(*) FROM requests WHERE status = 'pending';"
sqlite3 ~/.eggpool/eggpool.db "SELECT COUNT(*) FROM reservations WHERE status = 'active';"
```

Non-zero counts that grow over time indicate finalization failures.
The stale-request finalizer background task (runs every 60s) should
automatically clean these up.  If it is not keeping up, check the
logs for `Stale request finalizer` messages.

### Tuning the Stale-Request Finalizer

The finalizer uses the upstream `read_timeout` (default 300s) as the
pending-request threshold.  You can override this in `config.toml`:

```toml
[upstream]
read_timeout = 300  # seconds; also used as stale-request threshold
```

Lowering this value makes the finalizer more aggressive but increases
the risk of interrupting legitimate slow requests.  The default matches
the upstream timeout so no request that is still making progress
should be touched.
```

---

## 3. Testing Plan

### 3.1 Unit Tests

| Test | Description |
|------|-------------|
| `test_crash_recovery_clears_all_pending` | Verify `_crash_recovery` marks ALL pending requests as interrupted, not just old ones |
| `test_stale_finalizer_transitions_pending` | Verify `_finalize_stale_requests` finds and finalizes requests older than threshold |
| `test_stale_finalizer_releases_reservations` | Verify associated reservations are released and runtime state is decremented |
| `test_stale_finalizer_idempotent` | Running twice on same data is a no-op |
| `test_streaming_cancel_with_shield` | Mock a slow DB lock; verify `asyncio.shield` prevents ASGI cancellation from killing finalize |
| `test_streaming_response_always_closed` | Verify upstream response is closed even when generator is never started |

### 3.2 Integration Tests

| Test | Description |
|------|-------------|
| `test_streaming_client_disconnect` | Start a streaming request, drop the client connection, verify request is eventually finalized (may need stale-finalizer to catch it) |
| `test_leak_recovery_under_load` | Run 10 concurrent streaming requests, kill 5 clients mid-stream, verify stale finalizer cleans up within 2 cycles |
| `test_503_after_leak_accumulation` | Artificially insert 1000 leaked pending requests, verify proxy still serves new requests after stale finalizer runs |

### 3.3 Manual Verification Steps

1. Start eggpool with existing database that has leaked requests.
2. Check logs for `Crash recovery: marked N stale requests` on startup.
3. Verify `SELECT COUNT(*) FROM requests WHERE status = 'pending'` returns 0 after startup.
4. Run normal load for 10 minutes.
5. Verify pending count stays near 0 (should be equal to currently active requests only).
6. Check `SELECT COUNT(*) FROM reservations WHERE status = 'active'` — should be near 0.

---

## 4. Rollout & Rollback

### 4.1 Rollout

1. Apply migration: `eggpool migrate` (or auto-applied on startup).
2. Deploy new code.
3. Monitor logs for:
   - `Crash recovery: marked N stale requests` (should see this on first startup)
   - `Stale request finalizer: cleaned up N leaked requests` (should see 0 after steady state)
4. Verify 503s stop occurring.

### 4.2 Rollback

If issues arise:
1. Revert to previous code version.
2. The new index (`idx_requests_status_started`) is harmless — leave it.
3. The stale-finalizer background task will not exist in the old code, so leaked requests may re-accumulate. If this is a concern, run a one-off SQL cleanup:

```sql
UPDATE requests SET status = 'interrupted', completed_at = CURRENT_TIMESTAMP WHERE status = 'pending';
UPDATE reservations SET status = 'released', released_at = CURRENT_TIMESTAMP, release_reason = 'rollback_cleanup' WHERE status = 'active';
```

---

## 5. Estimated Effort

| Task | Effort |
|------|--------|
| Code changes (app.py, coordinator.py) | 2–3 hours |
| Migration file + runner verification | 30 min |
| Unit tests | 2–3 hours |
| Integration tests | 2–3 hours |
| Manual verification | 1 hour |
| Documentation update | 30 min |
| **Total** | **1–1.5 days** |

---

## 6. Open Questions

1. **Does the current `MigrationRunner` auto-discover `002_*.sql` files, or is there a hardcoded list?** — Verify before creating the migration.
2. **Should the stale-finalizer threshold be configurable independently of `read_timeout`?** — A dedicated `[limits].stale_request_threshold_seconds` would decouple them.
3. **Should we add metrics (Prometheus-style counters) for leaked-request cleanups?** — Useful for operational monitoring but out of scope for the immediate fix.
4. **Is there a separate read-only stats DB that needs its own crash recovery?** — The stats DB is read-only and opened after the main DB recovery, so it should not have leaked state. Confirm.

