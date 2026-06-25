"""Background cleanup tasks for retention and reservation management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router

logger = logging.getLogger(__name__)


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
            RETURNING id, account_id, reserved_microdollars,
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
            (-retain_days,),
        )

        count = await db.execute_write(
            """
            DELETE FROM requests
            WHERE started_at < datetime('now', ? || ' days')
            """,
            (-retain_days,),
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
            (-retain_days,),
        )
    if count > 0:
        logger.info("Deleted %d old account events", count)
    return count


async def reconcile_expired_reservations(
    db: Database,
    quota_estimator: QuotaEstimator | None = None,
    router: Router | None = None,
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
                  AND expires_at <= CURRENT_TIMESTAMP
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
            if transitioned_rows:
                from eggpool.db.repositories import OperationalEventRepository

                await OperationalEventRepository(db).record(
                    event_type="reservation_reconcile",
                    details={
                        "expired_reservations": len(transitioned_rows),
                    },
                )
    except Exception:
        logger.exception("Failed to reconcile expired reservations")
        raise

    count = len(transitioned_rows)

    await _reconcile_runtime_reservations(
        transitioned_rows,
        quota_estimator=quota_estimator,
        router=router,
    )

    if count > 0:
        logger.info("Reconciled %d expired reservations", count)
    return count


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
        if quota_estimator is not None and reserved_microdollars > 0:
            await quota_estimator.remove_reservation(
                account_name_value,
                reserved_microdollars,
            )
        if router is not None:
            await router.decrement_active_request_count(account_name_value)


async def checkpoint_database(db: Database) -> None:
    """Force a WAL checkpoint to reclaim disk space."""
    if db.read_only:
        logger.debug("Skipping WAL checkpoint on read-only database")
        return
    await db.execute_pragma("PRAGMA wal_checkpoint(PASSIVE)")
    logger.debug("Database WAL checkpoint completed")
