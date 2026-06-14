"""Background cleanup tasks for retention and reservation management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

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
) -> int:
    """Release reservations past their expiry inside a transaction.

    Returns the number of reservations reconciled.
    """
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
    if count > 0:
        logger.info("Reconciled %d expired reservations", count)
    return count


async def checkpoint_database(db: Database) -> None:
    """Force a WAL checkpoint to reclaim disk space."""
    await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    logger.debug("Database WAL checkpoint completed")
