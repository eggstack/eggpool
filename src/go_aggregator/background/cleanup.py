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
        result = await db.execute(
            """
            UPDATE reservations
            SET status = 'released', released_at = datetime('now')
            WHERE status = 'active'
              AND created_at < datetime('now', ? || ' seconds')
            """,
            (f"-{int(max_age_seconds)}",),
        )
    count = result.rowcount or 0
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
        await db.execute(
            """
            DELETE FROM reservations
            WHERE request_id IN (
                SELECT id FROM requests
                WHERE started_at < datetime('now', ? || ' days')
            )
            """,
            (f"-{retain_days}",),
        )

        result = await db.execute(
            """
            DELETE FROM requests
            WHERE started_at < datetime('now', ? || ' days')
            """,
            (f"-{retain_days}",),
        )
    count = result.rowcount or 0
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
        result = await db.execute(
            """
            DELETE FROM account_events
            WHERE created_at < datetime('now', ? || ' days')
            """,
            (f"-{retain_days}",),
        )
    count = result.rowcount or 0
    if count > 0:
        logger.info("Deleted %d old account events", count)
    return count


async def reconcile_expired_reservations(
    db: Database,
    quota_estimator: QuotaEstimator | None = None,
) -> int:
    """Release reservations past their expiry inside a transaction.

    Optionally reconciles in-memory reservation totals so that expired
    reservations are removed from the quota estimator's tracking.

    Returns the number of reservations reconciled.
    """
    # Collect expired reservations for in-memory sync before releasing
    expired_rows: list[dict[str, Any]] = []
    if quota_estimator is not None:
        expired_rows = [
            dict(r)
            for r in await db.fetch_all(
                "SELECT id, account_id, estimated_microdollars FROM reservations "
                "WHERE status = 'active' "
                "AND expires_at IS NOT NULL "
                "AND expires_at < CURRENT_TIMESTAMP"
            )
        ]

    async with db.transaction():
        cursor = await db.execute(
            """
            UPDATE reservations
            SET status = 'expired',
                released_at = CURRENT_TIMESTAMP,
                release_reason = 'expired'
            WHERE status = 'active'
              AND expires_at IS NOT NULL
              AND expires_at < CURRENT_TIMESTAMP
            """,
        )
    count = cursor.rowcount or 0

    # Sync in-memory reservation tracking for expired reservations
    if quota_estimator is not None and expired_rows:
        for row in expired_rows:
            account_id = row["account_id"]
            estimated_microdollars = row["estimated_microdollars"] or 0
            if estimated_microdollars <= 0:
                continue
            # Resolve account name from account_id
            acct_row = await db.fetch_one(
                "SELECT name FROM accounts WHERE id = ?", (account_id,)
            )
            if acct_row is not None:
                account_name = acct_row["name"]
                quota_estimator.remove_reservation(account_name, estimated_microdollars)

    if count > 0:
        logger.info("Reconciled %d expired reservations", count)
    return count


async def checkpoint_database(db: Database) -> None:
    """Force a WAL checkpoint to reclaim disk space."""
    await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    logger.debug("Database WAL checkpoint completed")
