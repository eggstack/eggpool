"""Background cleanup tasks for retention and reservation management."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from eggpool.background.maintenance import MaintenanceBudget, MaintenancePassResult
from eggpool.errors import DatabaseTransactionOwnershipError

_SQLITE_MAX_VARIABLE_NUMBER = 900

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from eggpool.db.connection import Database
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router

_DEFAULT_BUDGET = MaintenanceBudget()

logger = logging.getLogger(__name__)


async def retry_runtime_reconciliation(
    operation: Callable[[], Awaitable[None]],
    description: str,
) -> None:
    """Retry one runtime mirror update once, without aborting the batch."""
    try:
        await operation()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Runtime reconciliation failed for %s; retrying: %s", description, exc
        )
        try:
            await operation()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Runtime reconciliation failed after retry for %s", description
            )


async def _has_remaining_rows(
    db: Database, query: str, params: tuple[object, ...]
) -> bool:
    """Check if at least one row matches the given query (fast existence check).

    Uses :meth:`~eggpool.db.connection.Database.fetch_all` so the check
    can run outside an explicit transaction boundary.
    """
    rows = await db.fetch_all(query, params)
    return len(rows) > 0


async def _chunked_in_delete(
    db: Database, table: str, column: str, ids: list[object]
) -> int:
    """DELETE rows whose *column* is in *ids*, chunked for SQLite limits.

    Returns total rows deleted.
    """
    total_deleted = 0
    for i in range(0, len(ids), _SQLITE_MAX_VARIABLE_NUMBER):
        chunk = ids[i : i + _SQLITE_MAX_VARIABLE_NUMBER]
        placeholders = ",".join("?" for _ in chunk)
        total_deleted += await db.execute_write(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
            chunk,
        )
    return total_deleted


def _retention_cutoff(retain_days: int) -> str:
    """Return a SQLite relative-date modifier for a valid retention period."""
    if retain_days <= 0:
        raise ValueError("retain_days must be greater than zero")
    return f"-{retain_days}"


async def cleanup_stale_reservations(
    db: Database,
    max_age_seconds: float = 600.0,
    quota_estimator: QuotaEstimator | None = None,
    router: Router | None = None,
) -> int:
    """Release stale reservations whose request is no longer pending.

    Returns the number of reservations cleaned up.
    """
    if max_age_seconds <= 0:
        max_age_seconds = 1.0
    async with db.transaction():
        rows = await db.execute_returning(
            """
            UPDATE reservations
            SET status = 'released',
                released_at = datetime('now'),
                release_reason = 'stale_cleanup'
            WHERE status = 'active'
              AND created_at < datetime('now', ? || ' seconds')
              AND NOT EXISTS (
                  SELECT 1
                  FROM requests
                  WHERE requests.id = reservations.request_id
                    AND requests.status = 'pending'
              )
            RETURNING id, account_id, reserved_microdollars, estimated_tokens,
                (SELECT name FROM accounts WHERE id = reservations.account_id)
                AS account_name
            """,
            (-int(max_age_seconds),),
        )
        transitioned_rows = [dict(row) for row in rows]

    count = len(transitioned_rows)

    await _reconcile_runtime_reservations(
        transitioned_rows,
        quota_estimator=quota_estimator,
        router=router,
    )

    if count > 0:
        logger.info("Cleaned up %d stale reservations", count)
    return count


async def cleanup_old_requests(
    db: Database,
    retain_days: int = 30,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete request records older than the retention period.

    Also deletes associated reservations.  Uses keyset pagination bounded
    by *budget* to avoid long-running transactions.  The result has a
    ``.rows_changed`` attribute for backward compatibility.
    """
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            # Select a batch of candidate request IDs ordered for
            # stable keyset pagination.
            rows = await db.execute_returning(
                """
                SELECT id FROM requests
                WHERE started_at < datetime('now', ? || ' days')
                ORDER BY started_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            await _chunked_in_delete(db, "reservations", "request_id", ids)
            await _chunked_in_delete(db, "requests", "id", ids)

        total_deleted += len(ids)
        batches += 1
        await asyncio.sleep(0)

    # Check if more rows remain beyond the budget.
    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM requests"
                " WHERE started_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old request records (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def cleanup_old_events(
    db: Database,
    retain_days: int = 90,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete account events older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT id FROM account_events
                WHERE created_at < datetime('now', ? || ' days')
                ORDER BY created_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            count = await _chunked_in_delete(db, "account_events", "id", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM account_events"
                " WHERE created_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info("Deleted %d old account events", total_deleted)
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def reconcile_expired_reservations(
    db: Database,
    quota_estimator: QuotaEstimator | None = None,
    router: Router | None = None,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Release reservations past their expiry atomically.

    Uses UPDATE ... RETURNING inside bounded transactions so that only
    rows actually transitioned by this call are reconciled.  No other
    task can race the same rows.  The result has a ``.rows_changed``
    attribute for backward compatibility.
    """
    b = budget or _DEFAULT_BUDGET
    total_reconciled = 0
    batches = 0
    start = time.monotonic()

    try:
        while not b.expired(start_time=start, batches_done=batches):
            async with db.transaction():
                rows = await db.execute_returning(
                    """
                    UPDATE reservations
                    SET status = 'expired',
                        released_at = CURRENT_TIMESTAMP,
                        release_reason = 'expired'
                    WHERE id IN (
                        SELECT id FROM reservations
                        WHERE status = 'active'
                          AND expires_at IS NOT NULL
                          AND expires_at <= CURRENT_TIMESTAMP
                          AND NOT EXISTS (
                              SELECT 1
                              FROM requests
                              WHERE requests.id = reservations.request_id
                                AND requests.status = 'pending'
                          )
                        ORDER BY expires_at, id
                        LIMIT ?
                    )
                    RETURNING id, account_id, reserved_microdollars, estimated_tokens,
                      (SELECT name FROM accounts WHERE id = reservations.account_id)
                      AS account_name
                    """,
                    (b.max_rows_per_batch,),
                )
                transitioned_rows = [dict(row) for row in rows]
                if transitioned_rows:
                    from eggpool.db.repositories import OperationalEventRepository

                    await OperationalEventRepository(db).record(
                        event_type="reservation_reconcile",
                        details={
                            "expired_reservations": len(transitioned_rows),
                        },
                    )

            total_reconciled += len(transitioned_rows)
            batches += 1

            await _reconcile_runtime_reservations(
                transitioned_rows,
                quota_estimator=quota_estimator,
                router=router,
            )

            if not transitioned_rows:
                break
            await asyncio.sleep(0)
    except Exception:
        logger.exception("Failed to reconcile expired reservations")
        raise

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            """SELECT 1 FROM reservations
               WHERE status = 'active'
                 AND expires_at IS NOT NULL
                 AND expires_at <= CURRENT_TIMESTAMP
                 AND NOT EXISTS (
                     SELECT 1 FROM requests
                     WHERE requests.id = reservations.request_id
                       AND requests.status = 'pending'
                 )
               LIMIT 1""",
            (),
        )
        remaining = 1 if has_more else 0

    if total_reconciled > 0:
        logger.info("Reconciled %d expired reservations", total_reconciled)
    return MaintenancePassResult(
        rows_changed=total_reconciled,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def _reconcile_runtime_reservations(
    transitioned_rows: list[dict[str, object]],
    *,
    quota_estimator: QuotaEstimator | None,
    router: Router | None,
) -> None:
    """Mirror durable reservation transitions into runtime accounting.

    Active request counts track reservations, not their monetary value, so a
    zero-cost reservation must still decrement the count when it transitions.
    """
    for row in transitioned_rows:
        account_name_value = row.get("account_name")
        if not isinstance(account_name_value, str):
            continue

        reserved_value = row.get("reserved_microdollars")
        reserved_microdollars = reserved_value if isinstance(reserved_value, int) else 0
        estimated_tokens_value = row.get("estimated_tokens")
        estimated_tokens = (
            estimated_tokens_value if isinstance(estimated_tokens_value, int) else 0
        )
        if quota_estimator is not None:

            async def remove_reservation(
                account_name: str = account_name_value,
                reserved: int = reserved_microdollars,
                tokens: int = estimated_tokens,
            ) -> None:
                await quota_estimator.remove_reservation(
                    account_name,
                    reserved,
                    requests=1,
                    tokens=tokens,
                )

            await retry_runtime_reconciliation(
                remove_reservation,
                f"account {account_name_value!r} quota reservation",
            )
        if router is not None:
            await retry_runtime_reconciliation(
                lambda account_name=account_name_value: (
                    router.decrement_active_request_count(account_name)
                ),
                f"account {account_name_value!r} active request count",
            )


async def cleanup_old_operational_events(
    db: Database,
    retain_days: int = 90,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete operational events older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT id FROM operational_events
                WHERE occurred_at < datetime('now', ? || ' days')
                ORDER BY occurred_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            count = await _chunked_in_delete(db, "operational_events", "id", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM operational_events"
                " WHERE occurred_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old operational events (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def cleanup_old_usage_rollups(
    db: Database,
    retain_days: int = 90,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete usage rollups older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT rowid FROM usage_rollups
                WHERE bucket_start < datetime('now', ? || ' days')
                ORDER BY bucket_start, rowid
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["rowid"] for row in rows]
            count = await _chunked_in_delete(db, "usage_rollups", "rowid", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM usage_rollups"
                " WHERE bucket_start < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old usage_rollups rows (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def cleanup_old_price_snapshots(
    db: Database,
    retain_days: int = 180,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete model price snapshots older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT id FROM model_price_snapshots
                WHERE captured_at < datetime('now', ? || ' days')
                ORDER BY captured_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            count = await _chunked_in_delete(db, "model_price_snapshots", "id", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM model_price_snapshots"
                " WHERE captured_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old model price snapshots (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def cleanup_old_model_info_observations(
    db: Database,
    retain_days: int = 90,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete model info observations older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT id FROM model_info_observations
                WHERE observed_at < datetime('now', ? || ' days')
                ORDER BY observed_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            count = await _chunked_in_delete(db, "model_info_observations", "id", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM model_info_observations"
                " WHERE observed_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old model info observations (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def cleanup_old_routing_decisions(
    db: Database,
    retain_days: int = 90,
    budget: MaintenanceBudget | None = None,
) -> MaintenancePassResult:
    """Delete routing decisions older than the retention period."""
    b = budget or _DEFAULT_BUDGET
    cutoff = _retention_cutoff(retain_days)
    total_deleted = 0
    batches = 0
    start = time.monotonic()

    while not b.expired(start_time=start, batches_done=batches):
        async with db.transaction():
            rows = await db.execute_returning(
                """
                SELECT id FROM routing_decisions
                WHERE decision_made_at < datetime('now', ? || ' days')
                ORDER BY decision_made_at, id
                LIMIT ?
                """,
                (cutoff, b.max_rows_per_batch),
            )
            if not rows:
                break

            ids = [row["id"] for row in rows]
            count = await _chunked_in_delete(db, "routing_decisions", "id", ids)

        total_deleted += count
        batches += 1
        await asyncio.sleep(0)

    remaining: int | None = None
    if b.expired(start_time=start, batches_done=batches):
        has_more = await _has_remaining_rows(
            db,
            (
                "SELECT 1 FROM routing_decisions"
                " WHERE decision_made_at < datetime('now', ? || ' days')"
                " LIMIT 1"
            ),
            (cutoff,),
        )
        remaining = 1 if has_more else 0

    if total_deleted > 0:
        logger.info(
            "Deleted %d old routing decisions (retention=%d days)",
            total_deleted,
            retain_days,
        )
    return MaintenancePassResult(
        rows_changed=total_deleted,
        batches_completed=batches,
        remaining_estimate=remaining,
        budget_exhausted=b.expired(start_time=start, batches_done=batches),
    )


async def checkpoint_database(db: Database) -> dict[str, object]:
    """Force a WAL checkpoint to reclaim disk space.

    Returns a dict with checkpoint telemetry: ``busy``, ``log``,
    ``checkpointed`` frame counts, ``duration_ms``, and ``mode``.
    """
    if db.read_only:
        logger.debug("Skipping WAL checkpoint on read-only database")
        return {
            "busy": 0,
            "log": 0,
            "checkpointed": 0,
            "duration_ms": 0.0,
            "mode": "PASSIVE",
        }
    start = time.monotonic()
    try:
        rows = await db.execute_pragma("PRAGMA wal_checkpoint(PASSIVE)")
        busy = int(rows[0][0]) if rows else 0
        log = int(rows[0][1]) if rows else 0
        checkpointed = int(rows[0][2]) if rows else 0
    except DatabaseTransactionOwnershipError as exc:
        logger.debug("WAL checkpoint deferred while a transaction is active: %s", exc)
        duration_ms = (time.monotonic() - start) * 1000
        return {
            "busy": 1,
            "log": 0,
            "checkpointed": 0,
            "duration_ms": duration_ms,
            "mode": "PASSIVE",
            "contention": True,
        }
    except Exception:
        logger.exception("WAL checkpoint failed")
        duration_ms = (time.monotonic() - start) * 1000
        return {
            "busy": 0,
            "log": 0,
            "checkpointed": 0,
            "duration_ms": duration_ms,
            "mode": "PASSIVE",
        }
    duration_ms = (time.monotonic() - start) * 1000
    logger.debug(
        "Database WAL checkpoint completed: busy=%d log=%d"
        " checkpointed=%d duration_ms=%.1f",
        busy,
        log,
        checkpointed,
        duration_ms,
    )
    return {
        "busy": busy,
        "log": log,
        "checkpointed": checkpointed,
        "duration_ms": duration_ms,
        "mode": "PASSIVE",
    }
