"""Read-only diagnostic helpers for quota, reservations, and request exactness.

Wraps SQL queries against the durable ``requests`` and ``reservations``
tables so operators and the ``eggpool`` CLI can introspect aggregate
state without depending on the in-memory :class:`QuotaEstimator`.

These helpers do not mutate state; they exist to validate the
reservation/cost accounting path introduced in Phase 7 of the
``upstream-authoritative-suppression`` plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.db.connection import Database


async def account_usage_breakdown(
    db: Database,
    account_id: int,
) -> dict[str, Any]:
    """Return aggregated usage stats for a single account.

    Includes total request count, total cost, the 5-hour rolling cost,
    the pending reserved amount still held against the account, and a
    breakdown of how many requests completed, errored, or remain
    pending.
    """
    row = await db.fetch_one(
        """
        SELECT
            a.id AS account_id,
            a.name AS account_name,
            COUNT(r.id) AS request_count,
            COALESCE(SUM(r.cost_microdollars), 0) AS total_cost_microdollars,
            COALESCE(SUM(
                CASE WHEN r.started_at >= datetime('now', '-5 hours')
                THEN r.cost_microdollars ELSE 0 END
            ), 0) AS cost_5h_microdollars,
            COALESCE(SUM(
                CASE WHEN r.status = 'pending'
                THEN r.reserved_microdollars ELSE 0 END
            ), 0) AS pending_reserved_microdollars,
            COALESCE(SUM(
                CASE WHEN r.status = 'completed' THEN 1 ELSE 0 END
            ), 0) AS completed_count,
            COALESCE(SUM(
                CASE WHEN r.status = 'error' THEN 1 ELSE 0 END
            ), 0) AS error_count,
            COALESCE(SUM(
                CASE WHEN r.status = 'pending' THEN 1 ELSE 0 END
            ), 0) AS pending_count
        FROM accounts a
        LEFT JOIN requests r ON r.account_id = a.id
        WHERE a.id = ?
        GROUP BY a.id, a.name
        """,
        (account_id,),
    )
    if row is None:
        return {
            "account_id": account_id,
            "account_name": None,
            "request_count": 0,
            "total_cost_microdollars": 0,
            "cost_5h_microdollars": 0,
            "pending_reserved_microdollars": 0,
            "completed_count": 0,
            "error_count": 0,
            "pending_count": 0,
        }
    return {
        "account_id": int(row["account_id"]),
        "account_name": str(row["account_name"]),
        "request_count": int(row["request_count"] or 0),
        "total_cost_microdollars": int(row["total_cost_microdollars"] or 0),
        "cost_5h_microdollars": int(row["cost_5h_microdollars"] or 0),
        "pending_reserved_microdollars": int(row["pending_reserved_microdollars"] or 0),
        "completed_count": int(row["completed_count"] or 0),
        "error_count": int(row["error_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
    }


async def active_reservations_summary(db: Database) -> list[dict[str, Any]]:
    """Return per-account active reservations with reserved cost totals.

    A row is emitted for every account that has at least one active
    reservation. The ``active_reserved_microdollars`` value is the sum
    of ``reservations.reserved_microdollars`` for that account; the
    router subtracts it from remaining capacity in the scorer.
    """
    rows = await db.fetch_all(
        """
        SELECT
            a.id AS account_id,
            a.name AS account_name,
            COUNT(r.id) AS active_reservations,
            COALESCE(SUM(r.reserved_microdollars), 0)
                AS active_reserved_microdollars,
            MIN(r.created_at) AS oldest_reservation_at
        FROM reservations r
        JOIN accounts a ON a.id = r.account_id
        WHERE r.status = 'active'
        GROUP BY a.id, a.name
        ORDER BY a.name
        """
    )
    return [
        {
            "account_id": int(row["account_id"]),
            "account_name": str(row["account_name"]),
            "active_reservations": int(row["active_reservations"] or 0),
            "active_reserved_microdollars": int(
                row["active_reserved_microdollars"] or 0
            ),
            "oldest_reservation_at": row["oldest_reservation_at"],
        }
        for row in rows
    ]


async def exactness_distribution(db: Database) -> list[dict[str, Any]]:
    """Group requests by exactness level with cost totals.

    Useful for verifying that ``estimated`` requests (which inherit
    ``estimated_microdollars`` when the upstream omits usage data) do
    not dominate total cost.
    """
    rows = await db.fetch_all(
        """
        SELECT
            exactness,
            COUNT(*) AS request_count,
            COALESCE(SUM(cost_microdollars), 0) AS total_cost_microdollars,
            COALESCE(AVG(cost_microdollars), 0) AS avg_cost_microdollars
        FROM requests
        WHERE status != 'pending'
        GROUP BY exactness
        ORDER BY request_count DESC
        """
    )
    return [
        {
            "exactness": str(row["exactness"]),
            "request_count": int(row["request_count"] or 0),
            "total_cost_microdollars": int(row["total_cost_microdollars"] or 0),
            "avg_cost_microdollars": float(row["avg_cost_microdollars"] or 0.0),
        }
        for row in rows
    ]


async def stale_pending_requests(db: Database, threshold_seconds: int = 900) -> int:
    """Return the number of pending requests older than the threshold.

    Used by the audit CLI to flag requests that survived their
    reservation TTL without finalizing — a symptom of crashed workers
    or post-commit interruptions that need operator cleanup.
    """
    row = await db.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM requests
        WHERE status = 'pending'
          AND started_at < datetime('now', ?)
        """,
        (f"-{threshold_seconds} seconds",),
    )
    if row is None:
        return 0
    return int(row["n"] or 0)


__all__ = [
    "account_usage_breakdown",
    "active_reservations_summary",
    "exactness_distribution",
    "stale_pending_requests",
]
