"""Background cleanup tasks for retention and reservation management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database
    from go_aggregator.quota.estimation import QuotaEstimator

logger = logging.getLogger(__name__)


async def cleanup_stale_reservations(
    db: Database,
    max_age_seconds: float = 600.0,
) -> int:
    """Mark stale reservations as interrupted and release them.

    Returns the number of reservations cleaned up.
    """
    async with db.transaction():
        count = await db.execute_write(
            """
            UPDATE reservations
            SET status = 'released', released_at = datetime('now')
            WHERE status = 'active'
              AND created_at < datetime('now', ? || ' seconds')
            """,
            (f"-{int(max_age_seconds)}",),
        )
    if count > 0:
        logger.info("Cleaned up %d stale reservations", count)
    return count


async def cleanup_old_requests(
    db: Database,
    retain_days: int = 30,
) -> int:
    """Delete request records older than the retention period.

    Also deletes associated reservations.
    Returns the number of requests deleted.
    """
    async with db.transaction():
        # First delete reservations for old requests
        await db.execute_write(
            """
            DELETE FROM reservations
            WHERE request_id IN (
                SELECT id FROM requests
                WHERE started_at < datetime('now', ? || ' days')
            )
            """,
            (f"-{retain_days}",),
        )

        count = await db.execute_write(
            """
            DELETE FROM requests
            WHERE started_at < datetime('now', ? || ' days')
            """,
            (f"-{retain_days}",),
        )
    if count > 0:
        logger.info(
            "Deleted %d old request records (retention=%d days)",
            count,
            retain_days,
        )
    return count


async def cleanup_old_events(
    db: Database,
    retain_days: int = 90,
) -> int:
    """Delete account events older than the retention period."""
    async with db.transaction():
        count = await db.execute_write(
            """
            DELETE FROM account_events
            WHERE created_at < datetime('now', ? || ' days')
            """,
            (f"-{retain_days}",),
        )
    if count > 0:
        logger.info("Deleted %d old account events", count)
    return count


async def reconcile_expired_reservations(
    db: Database,
    quota_estimator: QuotaEstimator | None = None,
    router: Any | None = None,
) -> int:
    """Release reservations past their expiry atomically.

    Uses UPDATE ... RETURNING inside a single transaction so that only
    rows actually transitioned by this call are reconciled.  No other
    task can race the same rows.

    Returns the number of reservations reconciled.
    """
    try:
        async with db.transaction():
            rows = await db.execute_returning(
                """
                UPDATE reservations
                SET status = 'expired',
                    released_at = CURRENT_TIMESTAMP,
                    release_reason = 'expired'
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at < CURRENT_TIMESTAMP
                  AND NOT EXISTS (
                      SELECT 1
                      FROM requests
                      WHERE requests.id = reservations.request_id
                        AND requests.status = 'pending'
                  )
                RETURNING id, account_id, reserved_microdollars,
                    (SELECT name FROM accounts WHERE id = reservations.account_id)
                    AS account_name
                """,
            )
            transitioned_rows = [dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to reconcile expired reservations")
        raise

    count = len(transitioned_rows)

    # Sync in-memory reservation tracking for the rows we actually transitioned
    if quota_estimator is not None and transitioned_rows:
        for row in transitioned_rows:
            account_id = row.get("account_id")
            if account_id is None:
                continue
            estimated_microdollars = row.get("reserved_microdollars") or 0
            if estimated_microdollars <= 0:
                continue
            account_name = row.get("account_name")
            if account_name is None:
                continue
            await quota_estimator.remove_reservation(
                account_name, estimated_microdollars
            )
            # Also decrement active request count if router is available
            if router is not None:
                await router.decrement_active_request_count(account_name)

    if count > 0:
        logger.info("Reconciled %d expired reservations", count)
    return count


async def checkpoint_database(db: Database) -> None:
    """Force a WAL checkpoint to reclaim disk space."""
    if db.read_only:
        logger.debug("Skipping WAL checkpoint on read-only database")
        return
    await db.execute_pragma("PRAGMA wal_checkpoint(PASSIVE)")
    logger.debug("Database WAL checkpoint completed")
