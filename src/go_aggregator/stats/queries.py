"""Statistics query layer for SQLite aggregations.

Provides parameterized SQL queries for the statistics API and dashboard.
SQL logic lives here, not in HTTP route handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database


def _format_dt(dt: str) -> str:
    """Validate ISO 8601 datetime string for SQL parameter binding."""
    if not dt:
        return dt
    return dt


async def fetch_summary(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Get aggregate summary statistics for a time window."""
    sql = """
    SELECT
        COUNT(*) as total_requests,
        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
        COALESCE(SUM(cost_microdollars), 0) as total_cost_microdollars,
        COALESCE(AVG(upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0)
            as successful_requests,
        COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0)
            as error_requests,
        COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0)
            as total_cache_read_tokens,
        COALESCE(SUM(COALESCE(cache_write_tokens, 0)), 0)
            as total_cache_write_tokens,
        COALESCE(SUM(COALESCE(reasoning_tokens, 0)), 0)
            as total_reasoning_tokens,
        COALESCE(SUM(CASE WHEN streamed = 1 THEN 1 ELSE 0 END), 0)
            as streamed_requests,
        COALESCE(SUM(CASE WHEN streamed = 0 THEN 1 ELSE 0 END), 0)
            as non_streamed_requests,
        COALESCE(SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END), 0)
            as exact_count,
        COALESCE(SUM(CASE WHEN exactness = 'derived' THEN 1 ELSE 0 END), 0)
            as derived_count,
        COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0)
            as estimated_count,
        COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0)
            as unknown_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
    """
    row = await db.fetch_one(sql, (_format_dt(start), _format_dt(end)))
    if row is None:
        return _empty_summary()
    return _build_summary(dict(row))


async def fetch_account_stats(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Get per-account statistics for a time window."""
    sql = """
    SELECT
        a.id as account_id,
        a.name as account_name,
        a.enabled as account_enabled,
        a.weight as account_weight,
        COUNT(r.id) as request_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(AVG(r.upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count,
        COALESCE((
            SELECT SUM(r2.cost_microdollars) FROM requests r2
            WHERE r2.account_id = a.id
            AND r2.started_at >= datetime('now', '-5 hours')
            AND r2.status != 'cancelled'
        ), 0) as cost_5h,
        COALESCE((
            SELECT SUM(r2.cost_microdollars) FROM requests r2
            WHERE r2.account_id = a.id
            AND r2.started_at >= datetime('now', '-7 days')
            AND r2.status != 'cancelled'
        ), 0) as cost_7d,
        COALESCE((
            SELECT SUM(r2.cost_microdollars) FROM requests r2
            WHERE r2.account_id = a.id
            AND r2.started_at >= datetime('now', '-30 days')
            AND r2.status != 'cancelled'
        ), 0) as cost_30d
    FROM accounts a
    LEFT JOIN requests r
        ON r.account_id = a.id
        AND r.started_at >= ? AND r.started_at < ?
    GROUP BY a.id, a.name, a.enabled, a.weight
    ORDER BY a.name
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_model_stats(
    db: Database,
    start: str,
    end: str,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    """Get per-model statistics, optionally filtered by account."""
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND r.account_id = ?"
        params.append(account_id)

    sql = f"""
    SELECT
        r.model_id,
        COUNT(*) as request_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(AVG(r.upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count
    FROM requests r
    WHERE r.started_at >= ? AND r.started_at < ?{account_filter}
    GROUP BY r.model_id
    ORDER BY request_count DESC
    """
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_timeseries(
    db: Database,
    start: str,
    end: str,
    bucket: str = "hour",
    account_id: int | None = None,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get time-bucketed time series for a time window.

    Bucket must be one of: "hour", "day".
    """
    if bucket not in ("hour", "day"):
        bucket = "hour"

    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    params: list[Any] = [fmt, _format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND r.account_id = ?"
        params.append(account_id)
    model_filter = ""
    if model_id is not None:
        model_filter = " AND r.model_id = ?"
        params.append(model_id)

    sql = f"""
    SELECT
        strftime(?, r.started_at) as bucket,
        COUNT(*) as request_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count
    FROM requests r
    WHERE r.started_at >= ? AND r.started_at < ?{account_filter}{model_filter}
    GROUP BY bucket
    ORDER BY bucket
    """
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_error_breakdown(
    db: Database,
    start: str,
    end: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get error message breakdown for a time window."""
    sql = """
    SELECT
        r.error_message,
        r.model_id,
        a.name as account_name,
        COUNT(*) as error_count,
        MAX(r.started_at) as last_occurred_at
    FROM requests r
    JOIN accounts a ON a.id = r.account_id
    WHERE r.started_at >= ? AND r.started_at < ?
        AND r.status = 'error'
        AND r.error_message IS NOT NULL
    GROUP BY r.error_message, r.model_id, a.name
    ORDER BY error_count DESC
    LIMIT ?
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end), limit))
    return [dict(row) for row in rows]


async def fetch_recent_events(
    db: Database,
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Get recent account events, optionally filtered by type."""
    params: list[Any] = []
    type_filter = ""
    if event_type is not None:
        type_filter = " WHERE ae.event_type = ?"
        params.append(event_type)

    sql = f"""
    SELECT
        ae.id,
        ae.account_id,
        a.name as account_name,
        ae.event_type,
        ae.details,
        ae.created_at
    FROM account_events ae
    JOIN accounts a ON a.id = ae.account_id
    {type_filter}
    ORDER BY ae.created_at DESC
    LIMIT ?
    """
    params.append(limit)
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_active_reservations(
    db: Database,
) -> list[dict[str, Any]]:
    """Get currently active reservations."""
    sql = """
    SELECT
        r.id,
        r.request_id,
        r.account_id,
        a.name as account_name,
        r.model_id,
        r.reserved_microdollars,
        r.created_at
    FROM reservations r
    JOIN accounts a ON a.id = r.account_id
    WHERE r.status = 'active'
    ORDER BY r.created_at DESC
    """
    rows = await db.fetch_all(sql, ())
    return [dict(row) for row in rows]


async def fetch_account_id(db: Database, name: str) -> int | None:
    """Look up an account ID by name."""
    row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
    if row is None:
        return None
    return int(row["id"])


def _build_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Build a summary dict from a SQL row."""
    total = int(row.get("total_requests", 0))
    errors = int(row.get("error_requests", 0))
    error_rate = (errors / total) if total > 0 else 0.0
    return {
        "total_requests": total,
        "successful_requests": int(row.get("successful_requests", 0)),
        "error_requests": errors,
        "error_rate": error_rate,
        "total_input_tokens": int(row.get("total_input_tokens", 0)),
        "total_output_tokens": int(row.get("total_output_tokens", 0)),
        "total_cost_microdollars": int(row.get("total_cost_microdollars", 0)),
        "avg_latency_ms": float(row.get("avg_latency_ms", 0.0)),
        "total_cache_read_tokens": int(row.get("total_cache_read_tokens", 0)),
        "total_cache_write_tokens": int(row.get("total_cache_write_tokens", 0)),
        "total_reasoning_tokens": int(row.get("total_reasoning_tokens", 0)),
        "streamed_requests": int(row.get("streamed_requests", 0)),
        "non_streamed_requests": int(row.get("non_streamed_requests", 0)),
        "exact_count": int(row.get("exact_count", 0)),
        "derived_count": int(row.get("derived_count", 0)),
        "estimated_count": int(row.get("estimated_count", 0)),
        "unknown_count": int(row.get("unknown_count", 0)),
    }


def _empty_summary() -> dict[str, Any]:
    """Return a zero-valued summary."""
    return {
        "total_requests": 0,
        "successful_requests": 0,
        "error_requests": 0,
        "error_rate": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cost_microdollars": 0,
        "avg_latency_ms": 0.0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_reasoning_tokens": 0,
        "streamed_requests": 0,
        "non_streamed_requests": 0,
        "exact_count": 0,
        "derived_count": 0,
        "estimated_count": 0,
        "unknown_count": 0,
    }
