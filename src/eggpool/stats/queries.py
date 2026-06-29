"""Statistics query layer for SQLite aggregations.

Provides parameterized SQL queries for the statistics API and dashboard.
SQL logic lives here, not in HTTP route handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eggpool.stats.grouped_timeseries import postprocess_grouped_timeseries

if TYPE_CHECKING:
    from eggpool.db.connection import Database


def _format_dt(dt: str) -> str:
    """Validate ISO 8601 datetime string for SQL parameter binding.

    Empty input is preserved so callers can pass ``""`` when no
    filter is desired; otherwise the value must start with a 4-digit
    year and contain at least a date portion. Raises :class:`ValueError`
    on obviously invalid input so a malformed date does not silently
    match every row.
    """
    if not dt:
        return dt
    # Basic format check: must start with a 4-digit year and contain
    # at least a date portion.  Reject obviously invalid values.
    if len(dt) < 10 or not dt[:4].isdigit() or dt[4] != "-":
        raise ValueError(
            f"Invalid datetime {dt!r}: expected ISO 8601 string (YYYY-MM-DD[ HH:MM:SS])"
        )
    return dt


async def fetch_summary(
    db: Database,
    start: str,
    end: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Get aggregate summary statistics for a time window."""
    account_filter = " AND account_id = ?" if account_id is not None else ""
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    if account_id is not None:
        params.append(account_id)
    sql = f"""
    SELECT
        COUNT(*) as total_requests,
        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
        COALESCE(SUM(CASE WHEN status != 'pending'
            THEN input_tokens + output_tokens ELSE 0 END), 0)
            as total_tokens,
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
        COALESCE(SUM(CASE WHEN exactness = 'partial' THEN 1 ELSE 0 END), 0)
            as partial_count,
        COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0)
            as estimated_count,
        COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0)
            as unknown_count,
        COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN 1 ELSE 0 END), 0)
            as provider_reported_count,
        COALESCE(SUM(CASE WHEN exactness = 'provider_reported'
            THEN cost_microdollars ELSE 0 END), 0)
            as provider_reported_cost_microdollars,
        COALESCE(SUM(CASE WHEN exactness = 'estimated'
            THEN cost_microdollars ELSE 0 END), 0)
            as estimated_cost_sum_microdollars,
        COALESCE(SUM(bytes_received), 0) as total_bytes_received,
        COALESCE(SUM(bytes_emitted), 0) as total_bytes_emitted,
        (SELECT COUNT(DISTINCT provider_id) FROM accounts) as total_providers,
        COALESCE(AVG(CASE WHEN streamed = 1 THEN first_byte_ms END), 0)
            as avg_ttft_ms,
        CASE
            WHEN COALESCE(SUM(CASE WHEN status != 'pending'
                THEN upstream_latency_ms ELSE 0 END), 0) > 0
            THEN CAST(SUM(CASE WHEN status != 'pending'
                THEN output_tokens ELSE 0 END) AS REAL) * 1000.0
                / SUM(CASE WHEN status != 'pending'
                    THEN upstream_latency_ms ELSE 0 END)
            ELSE 0
        END as tokens_per_second
    FROM requests
    WHERE started_at >= ? AND started_at < ?{account_filter}
    """
    row = await db.fetch_one(sql, tuple(params))
    if row is None:
        return _empty_summary()

    result = _build_summary(dict(row))

    # Compute TTFT percentiles (streamed only) — requires window functions
    ttft = await _fetch_ttft_percentiles(db, start, end, account_id=account_id)
    result.update(ttft)

    return result


async def fetch_account_stats(
    db: Database,
    start: str,
    end: str,
    *,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    """Get per-account statistics for a time window.

    Extended with Phase 5 cost/cache/reasoning exactness metrics:
        exact_count / derived_count / estimated_count / unknown_count
        estimated_cost_fraction / unknown_cost_fraction
        cache_read_tokens / cache_write_tokens / cache_read_ratio /
            cache_write_ratio
        reasoning_tokens / reasoning_output_ratio
        avg_cost_per_request / avg_cost_per_1k_tokens
    Ratios are NULL (not 0) when the denominator is zero so the dashboard
    can distinguish "no usage" from "0.0 ratio on real usage".

    ``include_disabled`` defaults to True so the JSON API keeps returning
    every account (including soft-deleted ones — their historical rows
    still need to attribute costs/tokens). The dashboard passes False to
    hide accounts that ``sync_from_config`` marked ``enabled = 0`` after
    a ``eggpool logout`` round-trip, so the page matches the operator's
    mental model while preserving tombstones for history.
    """
    where_clause = "" if include_disabled else "WHERE a.enabled = 1"
    sql = f"""
    WITH period_stats AS (
        SELECT
            r.account_id,
            COUNT(*) as request_count,
            COALESCE(SUM(r.input_tokens), 0) as input_tokens,
            COALESCE(SUM(r.output_tokens), 0) as output_tokens,
            COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
            COALESCE(AVG(r.upstream_latency_ms), 0) as avg_latency_ms,
            COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
                as error_count,
            COALESCE(SUM(r.bytes_received), 0) as bytes_received,
            COALESCE(SUM(r.bytes_emitted), 0) as bytes_emitted,
            COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)
                as avg_ttft_ms,
            COALESCE(SUM(CASE WHEN r.status != 'pending'
                THEN r.upstream_latency_ms ELSE 0 END), 0) as sum_latency_ms,
            COALESCE(SUM(r.cache_read_tokens), 0) as cache_read_tokens,
            COALESCE(SUM(r.cache_write_tokens), 0) as cache_write_tokens,
            COALESCE(SUM(r.reasoning_tokens), 0) as reasoning_tokens,
            COALESCE(SUM(CASE WHEN r.exactness = 'exact' THEN 1 ELSE 0 END), 0)
                as exact_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'derived' THEN 1 ELSE 0 END), 0)
                as derived_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'partial' THEN 1 ELSE 0 END), 0)
                as partial_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'estimated' THEN 1 ELSE 0 END), 0)
                as estimated_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'unknown' OR r.exactness IS NULL
                THEN 1 ELSE 0 END), 0) as unknown_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'provider_reported'
                THEN 1 ELSE 0 END), 0) as provider_reported_count,
            COALESCE(SUM(CASE WHEN r.exactness = 'provider_reported'
                THEN r.cost_microdollars ELSE 0 END), 0)
                as provider_reported_cost_microdollars,
            COALESCE(SUM(CASE WHEN r.exactness = 'estimated'
                THEN r.cost_microdollars ELSE 0 END), 0)
                as estimated_cost_sum_microdollars
        FROM requests r
        WHERE r.started_at >= ? AND r.started_at < ?
        GROUP BY r.account_id
    ),
    rolling_stats AS (
        SELECT
            r.account_id,
            COALESCE(SUM(CASE
                WHEN r.started_at >= datetime('now', '-5 hours')
                THEN r.cost_microdollars ELSE 0 END), 0) as cost_5h,
            COALESCE(SUM(CASE
                WHEN r.started_at >= datetime('now', '-7 days')
                THEN r.cost_microdollars ELSE 0 END), 0) as cost_7d,
            COALESCE(SUM(r.cost_microdollars), 0) as cost_30d
        FROM requests r
        WHERE r.started_at >= datetime('now', '-30 days')
          AND r.status != 'pending'
        GROUP BY r.account_id
    )
    SELECT
        a.id as account_id,
        a.name as account_name,
        a.enabled as account_enabled,
        a.weight as account_weight,
        a.provider_id as provider_id,
        COALESCE(ps.request_count, 0) as request_count,
        COALESCE(ps.input_tokens, 0) as input_tokens,
        COALESCE(ps.output_tokens, 0) as output_tokens,
        COALESCE(ps.input_tokens, 0) + COALESCE(ps.output_tokens, 0)
            as total_tokens,
        COALESCE(ps.cost_microdollars, 0) as cost_microdollars,
        COALESCE(ps.avg_latency_ms, 0) as avg_latency_ms,
        COALESCE(ps.error_count, 0) as error_count,
        COALESCE(rs.cost_5h, 0) as cost_5h,
        COALESCE(rs.cost_7d, 0) as cost_7d,
        COALESCE(rs.cost_30d, 0) as cost_30d,
        COALESCE(ps.bytes_received, 0) as bytes_received,
        COALESCE(ps.bytes_emitted, 0) as bytes_emitted,
        COALESCE(ps.avg_ttft_ms, 0) as avg_ttft_ms,
        CASE
            WHEN COALESCE(ps.sum_latency_ms, 0) > 0
            THEN CAST(COALESCE(ps.output_tokens, 0) AS REAL) * 1000.0
                / ps.sum_latency_ms
            ELSE 0
        END as tokens_per_second,
        COALESCE(ps.cache_read_tokens, 0) as cache_read_tokens,
        COALESCE(ps.cache_write_tokens, 0) as cache_write_tokens,
        COALESCE(ps.reasoning_tokens, 0) as reasoning_tokens,
        COALESCE(ps.exact_count, 0) as exact_count,
        COALESCE(ps.derived_count, 0) as derived_count,
        COALESCE(ps.partial_count, 0) as partial_count,
        COALESCE(ps.estimated_count, 0) as estimated_count,
        COALESCE(ps.unknown_count, 0) as unknown_count,
        COALESCE(ps.provider_reported_count, 0) as provider_reported_count,
        COALESCE(ps.provider_reported_cost_microdollars, 0)
            as provider_reported_cost_microdollars,
        COALESCE(ps.estimated_cost_sum_microdollars, 0)
            as estimated_cost_sum_microdollars,
        CASE
            WHEN COALESCE(ps.request_count, 0) > 0
            THEN CAST(COALESCE(ps.estimated_count, 0) AS REAL)
                / ps.request_count
            ELSE 0
        END as estimated_cost_fraction,
        CASE
            WHEN COALESCE(ps.request_count, 0) > 0
            THEN CAST(COALESCE(ps.unknown_count, 0) AS REAL)
                / ps.request_count
            ELSE 0
        END as unknown_cost_fraction,
        CASE
            WHEN COALESCE(ps.input_tokens, 0) > 0
            THEN CAST(COALESCE(ps.cache_read_tokens, 0) AS REAL)
                / ps.input_tokens
            ELSE NULL
        END as cache_read_ratio,
        CASE
            WHEN COALESCE(ps.input_tokens, 0) > 0
            THEN CAST(COALESCE(ps.cache_write_tokens, 0) AS REAL)
                / ps.input_tokens
            ELSE NULL
        END as cache_write_ratio,
        CASE
            WHEN COALESCE(ps.output_tokens, 0) > 0
            THEN CAST(COALESCE(ps.reasoning_tokens, 0) AS REAL)
                / ps.output_tokens
            ELSE NULL
        END as reasoning_output_ratio,
        CASE
            WHEN COALESCE(ps.request_count, 0) > 0
            THEN CAST(COALESCE(ps.cost_microdollars, 0) AS REAL)
                / ps.request_count
            ELSE 0
        END as avg_cost_per_request,
        CASE
            WHEN (COALESCE(ps.input_tokens, 0)
                  + COALESCE(ps.output_tokens, 0)) > 0
            THEN CAST(COALESCE(ps.cost_microdollars, 0) AS REAL) * 1000.0
                / (ps.input_tokens + ps.output_tokens)
            ELSE NULL
        END as avg_cost_per_1k_tokens
    FROM accounts a
    LEFT JOIN period_stats ps ON ps.account_id = a.id
    LEFT JOIN rolling_stats rs ON rs.account_id = a.id
    {where_clause}
    ORDER BY a.name
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_disabled_account_count(db: Database) -> int:
    """Return the count of accounts marked ``enabled = 0`` by sync_from_config.

    Used by the dashboard to render the "N disabled — show them?" empty
    state when the operator has filtered disabled rows out. Cheap one-row
    aggregate; safe to call on every render.
    """
    row = await db.fetch_one(
        "SELECT COUNT(*) AS disabled_count FROM accounts WHERE enabled = 0",
        (),
    )
    if row is None:
        return 0
    return int(row["disabled_count"])


async def fetch_model_stats(
    db: Database,
    start: str,
    end: str,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    """Get per-model statistics, optionally filtered by account.

    Rows whose ``model_id`` has been relinked to the deprecated
    placeholder are reported under their ``original_model_id`` so
    historical usage remains attributable to the real model name.
    """
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND r.account_id = ?"
        params.append(account_id)

    sql = f"""
    SELECT
        COALESCE(r.original_model_id, r.model_id) AS model_id,
        r.provider_id,
        COUNT(*) as request_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0)
            as total_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(AVG(r.upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count,
        COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)
            as avg_ttft_ms,
        CASE
            WHEN COALESCE(SUM(CASE WHEN r.status != 'pending'
                THEN r.upstream_latency_ms ELSE 0 END), 0) > 0
            THEN CAST(COALESCE(SUM(r.output_tokens), 0) AS REAL) * 1000.0
                / SUM(CASE WHEN r.status != 'pending'
                    THEN r.upstream_latency_ms ELSE 0 END)
            ELSE 0
        END as tokens_per_second,
        COALESCE(SUM(r.cache_read_tokens), 0) as cache_read_tokens,
        COALESCE(SUM(r.cache_write_tokens), 0) as cache_write_tokens,
        COALESCE(SUM(r.reasoning_tokens), 0) as reasoning_tokens,
        COALESCE(SUM(CASE WHEN r.exactness = 'exact' THEN 1 ELSE 0 END), 0)
            as exact_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'derived' THEN 1 ELSE 0 END), 0)
            as derived_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'partial' THEN 1 ELSE 0 END), 0)
            as partial_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'estimated' THEN 1 ELSE 0 END), 0)
            as estimated_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'unknown' OR r.exactness IS NULL
            THEN 1 ELSE 0 END), 0) as unknown_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'provider_reported' THEN 1 ELSE 0 END), 0)
            as provider_reported_count,
        COALESCE(SUM(CASE WHEN r.exactness = 'provider_reported'
            THEN r.cost_microdollars ELSE 0 END), 0)
            as provider_reported_cost_microdollars,
        COALESCE(SUM(CASE WHEN r.exactness = 'estimated'
            THEN r.cost_microdollars ELSE 0 END), 0)
            as estimated_cost_sum_microdollars,
        CASE
            WHEN COUNT(*) > 0
            THEN CAST(COALESCE(SUM(CASE WHEN r.exactness = 'estimated'
                THEN 1 ELSE 0 END), 0) AS REAL) / COUNT(*)
            ELSE 0
        END as estimated_cost_fraction,
        CASE
            WHEN COUNT(*) > 0
            THEN CAST(COALESCE(SUM(CASE WHEN r.exactness = 'unknown'
                OR r.exactness IS NULL THEN 1 ELSE 0 END), 0) AS REAL)
                / COUNT(*)
            ELSE 0
        END as unknown_cost_fraction,
        CASE
            WHEN COALESCE(SUM(r.input_tokens), 0) > 0
            THEN CAST(COALESCE(SUM(r.cache_read_tokens), 0) AS REAL)
                / SUM(r.input_tokens)
            ELSE NULL
        END as cache_read_ratio,
        CASE
            WHEN COALESCE(SUM(r.input_tokens), 0) > 0
            THEN CAST(COALESCE(SUM(r.cache_write_tokens), 0) AS REAL)
                / SUM(r.input_tokens)
            ELSE NULL
        END as cache_write_ratio,
        CASE
            WHEN COALESCE(SUM(r.output_tokens), 0) > 0
            THEN CAST(COALESCE(SUM(r.reasoning_tokens), 0) AS REAL)
                / SUM(r.output_tokens)
            ELSE NULL
        END as reasoning_output_ratio,
        CASE
            WHEN COUNT(*) > 0
            THEN CAST(COALESCE(SUM(r.cost_microdollars), 0) AS REAL)
                / COUNT(*)
            ELSE 0
        END as avg_cost_per_request,
        CASE
            WHEN (COALESCE(SUM(r.input_tokens), 0)
                  + COALESCE(SUM(r.output_tokens), 0)) > 0
            THEN CAST(COALESCE(SUM(r.cost_microdollars), 0) AS REAL) * 1000.0
                / (SUM(r.input_tokens) + SUM(r.output_tokens))
            ELSE NULL
        END as avg_cost_per_1k_tokens
    FROM requests r
    WHERE r.started_at >= ? AND r.started_at < ?{account_filter}
    GROUP BY COALESCE(r.original_model_id, r.model_id), r.provider_id
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
    # A real model id can match both live rows and rows that were
    # relinked to the deprecated placeholder after the model was
    # withdrawn upstream. Match either the current or original id.
    model_filter = ""
    if model_id is not None:
        model_filter = " AND (r.model_id = ? OR r.original_model_id = ?)"
        params.extend([model_id, model_id])

    sql = f"""
    SELECT
        strftime(?, r.started_at) as bucket,
        COUNT(*) as request_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0)
            as total_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count,
        COALESCE(SUM(r.bytes_received), 0) as bytes_received,
        COALESCE(SUM(r.bytes_emitted), 0) as bytes_emitted,
        COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)
            as avg_ttft_ms
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
    """Get error class/detail breakdown for a time window."""
    sql = """
    SELECT
        r.error_class,
        r.error_detail,
        COALESCE(r.original_model_id, r.model_id) AS model_id,
        a.name as account_name,
        COUNT(*) as error_count,
        MAX(r.started_at) as last_occurred_at
    FROM requests r
    JOIN accounts a ON a.id = r.account_id
    WHERE r.started_at >= ? AND r.started_at < ?
        AND r.status = 'error'
        AND r.error_class IS NOT NULL
    GROUP BY r.error_class, r.error_detail,
        COALESCE(r.original_model_id, r.model_id), a.name
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
        COALESCE(r.original_model_id, r.model_id) AS model_id,
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


async def fetch_bandwidth_timeseries(
    db: Database,
    start: str,
    end: str,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    """Get daily-bucketed bandwidth for heatmap and detail views."""
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND r.account_id = ?"
        params.append(account_id)

    sql = f"""
    SELECT
        strftime('%Y-%m-%d', r.started_at) as day,
        COALESCE(SUM(r.bytes_received), 0) as bytes_received,
        COALESCE(SUM(r.bytes_emitted), 0) as bytes_emitted,
        COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0)
            as total_tokens,
        COUNT(*) as request_count
    FROM requests r
    WHERE r.started_at >= ? AND r.started_at < ?
        AND r.status != 'pending'
        {account_filter}
    GROUP BY day
    ORDER BY day
    """
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


# Group expressions for fetch_grouped_timeseries.  Each entry maps the
# public ``group_by`` enum to (raw_series_key_expr, raw_series_label_expr).
# The key is what we fold against; the label is what we render.  All four
# expressions project ``provider_id`` / ``model_id`` / ``account_name``
# columns so downstream rendering can still disambiguate even when the
# chosen group_by collapses one of those dimensions.
_GROUP_EXPRESSIONS: dict[str, tuple[str, str]] = {
    "provider": (
        "r.provider_id",
        "r.provider_id",
    ),
    "model": (
        "COALESCE(r.original_model_id, r.model_id)",
        "COALESCE(r.original_model_id, r.model_id)",
    ),
    "provider_model": (
        "r.provider_id || ':' || COALESCE(r.original_model_id, r.model_id)",
        "r.provider_id || ' / ' || COALESCE(r.original_model_id, r.model_id)",
    ),
    "account": (
        "a.name",
        "a.name",
    ),
}


def _resolve_group_exprs(group_by: str) -> tuple[str, str]:
    """Return (raw_series_key_expr, raw_series_label_expr) for a group_by value.

    Unknown values fall back to ``provider_model`` so a typo in a query
    string never yields a SQL fragment with empty alias semantics.
    """
    if group_by not in _GROUP_EXPRESSIONS:
        return _GROUP_EXPRESSIONS["provider_model"]
    return _GROUP_EXPRESSIONS[group_by]


async def fetch_grouped_timeseries(
    db: Database,
    start: str,
    end: str,
    *,
    bucket: str = "hour",
    group_by: str = "provider_model",
    limit: int = 12,
    account_id: int | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Get time-bucketed time series grouped by a chosen dimension.

    Returns a stable dashboard contract with the following keys:

    - ``bucket``            : ``"hour"`` or ``"day"``
    - ``group_by``          : the resolved grouping key
    - ``metric``            : always ``"requests"`` in this implementation;
      preserved as a stable field for future ranking dimensions.
    - ``limit``             : the resolved top-N limit
    - ``series``            : summary metadata per top-N series (+ ``Other``
      when the dataset contains more distinct keys than ``limit``).
    - ``buckets``           : sorted list of unique bucket labels.
    - ``bucket_totals``     : one entry per bucket with totals across all
      series (including ``Other``).
    - ``points``            : one row per ``(bucket, series_key)`` pair.

    Allowed ``bucket`` values: ``"hour"``, ``"day"``.  Any other value
    falls back to ``"hour"`` silently.  Allowed ``group_by`` values:
    ``"provider"``, ``"model"``, ``"provider_model"``, ``"account"``.
    Unknown values fall back to ``"provider_model"``.  Top-N is selected
    by descending ``request_count`` and rows outside the top-N are folded
    into a single ``__other__`` series per bucket so totals remain
    loss-less.

    ``account_id`` and ``model_id`` are optional exact filters; ``model_id``
    matches either the current ``model_id`` or the ``original_model_id``
    so relinked deprecated-model rows still appear under their original
    model name.
    """
    if bucket not in ("hour", "day"):
        bucket = "hour"
    resolved_group_by = group_by if group_by in _GROUP_EXPRESSIONS else "provider_model"

    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    key_expr, label_expr = _resolve_group_exprs(resolved_group_by)

    params: list[Any] = [_format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND r.account_id = ?"
        params.append(account_id)
    model_filter = ""
    if model_id is not None:
        model_filter = " AND (r.model_id = ? OR r.original_model_id = ?)"
        params.extend([model_id, model_id])

    sql = f"""
    SELECT
        strftime('{fmt}', r.started_at) as bucket,
        {key_expr} as raw_series_key,
        {label_expr} as raw_series_label,
        r.provider_id as provider_id,
        COALESCE(r.original_model_id, r.model_id) as model_id,
        a.name as account_name,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count,
        COALESCE(SUM(r.input_tokens), 0) as input_tokens,
        COALESCE(SUM(r.output_tokens), 0) as output_tokens,
        COALESCE(SUM(r.cache_read_tokens), 0) as cache_read_tokens,
        COALESCE(SUM(r.cache_write_tokens), 0) as cache_write_tokens,
        COALESCE(SUM(r.reasoning_tokens), 0) as reasoning_tokens,
        COALESCE(SUM(r.input_tokens), 0) + COALESCE(SUM(r.output_tokens), 0)
            as total_tokens,
        COALESCE(SUM(r.cost_microdollars), 0) as cost_microdollars,
        COALESCE(SUM(r.bytes_received), 0) as bytes_received,
        COALESCE(SUM(r.bytes_emitted), 0) as bytes_emitted,
        COALESCE(AVG(r.upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(AVG(CASE WHEN r.streamed = 1 THEN r.first_byte_ms END), 0)
            as avg_ttft_ms
    FROM requests r
    JOIN accounts a ON a.id = r.account_id
    WHERE r.started_at >= ? AND r.started_at < ?
        {account_filter}{model_filter}
    GROUP BY bucket, raw_series_key, raw_series_label, r.provider_id,
        COALESCE(r.original_model_id, r.model_id), a.name
    ORDER BY bucket, raw_series_label ASC
    """
    rows = await db.fetch_all(sql, tuple(params))

    return postprocess_grouped_timeseries(
        [dict(row) for row in rows],
        bucket=bucket,
        group_by=resolved_group_by,
        limit=limit,
    )


async def fetch_exactness_breakdown(
    db: Database,
    start: str,
    end: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Fetch exactness counts and cost aggregates from the requests table.

    The ``usage_rollups`` rollup table is bucketed by status and does not
    retain the ``exactness`` column, so the rollup-based summary path in
    :meth:`StatsService.get_summary_from_rollups` cannot supply the
    exactness counters the dashboard renders on its index card. This
    helper does one cheap ``GROUP BY`` against ``requests`` to backfill
    them, preserving parity with the live :func:`fetch_summary` path.

    Returns a dict with keys matching the summary contract:
    ``exact_count``, ``derived_count``, ``partial_count``,
    ``estimated_count``, ``unknown_count``, ``provider_reported_count``,
    ``provider_reported_cost_microdollars``,
    ``estimated_cost_sum_microdollars``. All values default to zero.
    """
    account_filter = " AND account_id = ?" if account_id is not None else ""
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    if account_id is not None:
        params.append(account_id)
    sql = f"""
    SELECT
        COALESCE(SUM(CASE WHEN exactness = 'exact' THEN 1 ELSE 0 END), 0)
            as exact_count,
        COALESCE(SUM(CASE WHEN exactness = 'derived' THEN 1 ELSE 0 END), 0)
            as derived_count,
        COALESCE(SUM(CASE WHEN exactness = 'partial' THEN 1 ELSE 0 END), 0)
            as partial_count,
        COALESCE(SUM(CASE WHEN exactness = 'estimated' THEN 1 ELSE 0 END), 0)
            as estimated_count,
        COALESCE(SUM(CASE WHEN exactness = 'unknown' THEN 1 ELSE 0 END), 0)
            as unknown_count,
        COALESCE(SUM(CASE WHEN exactness = 'provider_reported' THEN 1 ELSE 0 END), 0)
            as provider_reported_count,
        COALESCE(SUM(CASE WHEN exactness = 'provider_reported'
            THEN cost_microdollars ELSE 0 END), 0)
            as provider_reported_cost_microdollars,
        COALESCE(SUM(CASE WHEN exactness = 'estimated'
            THEN cost_microdollars ELSE 0 END), 0)
            as estimated_cost_sum_microdollars
    FROM requests
    WHERE started_at >= ? AND started_at < ?{account_filter}
    """
    row = await db.fetch_one(sql, tuple(params))
    if row is None:
        return {
            "exact_count": 0,
            "derived_count": 0,
            "partial_count": 0,
            "estimated_count": 0,
            "unknown_count": 0,
            "provider_reported_count": 0,
            "provider_reported_cost_microdollars": 0,
            "estimated_cost_sum_microdollars": 0,
        }
    data = dict(row)
    return {
        "exact_count": int(data.get("exact_count", 0) or 0),
        "derived_count": int(data.get("derived_count", 0) or 0),
        "partial_count": int(data.get("partial_count", 0) or 0),
        "estimated_count": int(data.get("estimated_count", 0) or 0),
        "unknown_count": int(data.get("unknown_count", 0) or 0),
        "provider_reported_count": int(data.get("provider_reported_count", 0) or 0),
        "provider_reported_cost_microdollars": int(
            data.get("provider_reported_cost_microdollars", 0) or 0
        ),
        "estimated_cost_sum_microdollars": int(
            data.get("estimated_cost_sum_microdollars", 0) or 0
        ),
    }


def _build_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Build a summary dict from a SQL row."""
    total = int(row.get("total_requests", 0))
    errors = int(row.get("error_requests", 0))
    error_rate = (errors / total) if total > 0 else 0.0
    total_input_tokens = int(row.get("total_input_tokens", 0))
    total_cache_read_tokens = int(row.get("total_cache_read_tokens", 0))
    cache_read_ratio = (
        total_cache_read_tokens / total_input_tokens if total_input_tokens > 0 else None
    )
    return {
        "total_requests": total,
        "successful_requests": int(row.get("successful_requests", 0)),
        "error_requests": errors,
        "error_rate": error_rate,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": int(row.get("total_output_tokens", 0)),
        "total_tokens": int(row.get("total_tokens", 0)),
        "total_cost_microdollars": int(row.get("total_cost_microdollars", 0)),
        "avg_latency_ms": float(row.get("avg_latency_ms", 0.0)),
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": int(row.get("total_cache_write_tokens", 0)),
        "total_reasoning_tokens": int(row.get("total_reasoning_tokens", 0)),
        "cache_read_ratio": cache_read_ratio,
        "streamed_requests": int(row.get("streamed_requests", 0)),
        "non_streamed_requests": int(row.get("non_streamed_requests", 0)),
        "exact_count": int(row.get("exact_count", 0)),
        "derived_count": int(row.get("derived_count", 0)),
        "partial_count": int(row.get("partial_count", 0)),
        "estimated_count": int(row.get("estimated_count", 0)),
        "unknown_count": int(row.get("unknown_count", 0)),
        "provider_reported_count": int(row.get("provider_reported_count", 0)),
        "provider_reported_cost_microdollars": int(
            row.get("provider_reported_cost_microdollars", 0)
        ),
        "estimated_cost_sum_microdollars": int(
            row.get("estimated_cost_sum_microdollars", 0)
        ),
        "total_bytes_received": int(row.get("total_bytes_received", 0)),
        "total_bytes_emitted": int(row.get("total_bytes_emitted", 0)),
        "total_providers": int(row.get("total_providers", 0)),
        "avg_ttft_ms": float(row.get("avg_ttft_ms", 0.0)),
        "tokens_per_second": float(row.get("tokens_per_second", 0.0)),
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
        "total_tokens": 0,
        "total_cost_microdollars": 0,
        "avg_latency_ms": 0.0,
        "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0,
        "total_reasoning_tokens": 0,
        "cache_read_ratio": None,
        "streamed_requests": 0,
        "non_streamed_requests": 0,
        "exact_count": 0,
        "derived_count": 0,
        "partial_count": 0,
        "estimated_count": 0,
        "unknown_count": 0,
        "provider_reported_count": 0,
        "provider_reported_cost_microdollars": 0,
        "estimated_cost_sum_microdollars": 0,
        "total_bytes_received": 0,
        "total_bytes_emitted": 0,
        "total_providers": 0,
        "avg_ttft_ms": 0.0,
        "tokens_per_second": 0.0,
        "p50_ttft_ms": 0.0,
        "p99_ttft_ms": 0.0,
    }


async def _fetch_ttft_percentiles(
    db: Database,
    start: str,
    end: str,
    provider_id: str | None = None,
    model_id: str | None = None,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Compute P50 and P99 of first_byte_ms for streamed requests.

    Uses a window-function subquery to find the median and 99th percentile
    value from the sorted distribution. Returns a dict with p50_ttft_ms and
    p99_ttft_ms (floats). Returns zeros when no streamed data exists.
    """
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    extra_filters = ""
    if provider_id is not None:
        extra_filters += " AND provider_id = ?"
        params.append(provider_id)
    if model_id is not None:
        # Real model id may have been relinked to the deprecated
        # placeholder; match either side.
        extra_filters += " AND (model_id = ? OR original_model_id = ?)"
        params.extend([model_id, model_id])
    if account_id is not None:
        extra_filters += " AND account_id = ?"
        params.append(account_id)

    sql = f"""
    SELECT
        AVG(CASE WHEN sub.rn IN (
            CAST((sub.total_count + 1) / 2 AS INTEGER),
            CAST((sub.total_count + 2) / 2 AS INTEGER)
        ) THEN sub.first_byte_ms END) as p50_ttft_ms,
        MAX(CASE WHEN sub.rn = sub.p99_idx THEN sub.first_byte_ms END)
            as p99_ttft_ms
    FROM (
        SELECT
            first_byte_ms,
            ROW_NUMBER() OVER (ORDER BY first_byte_ms) as rn,
            COUNT(*) OVER () as total_count,
            CAST(CEIL(0.99 * COUNT(*) OVER ()) AS INTEGER) as p99_idx
        FROM requests
        WHERE streamed = 1
          AND first_byte_ms IS NOT NULL
          AND started_at >= ? AND started_at < ?
          {extra_filters}
    ) sub
    WHERE sub.rn IN (
            CAST((sub.total_count + 1) / 2 AS INTEGER),
            CAST((sub.total_count + 2) / 2 AS INTEGER)
        )
       OR sub.rn = sub.p99_idx
    """
    row = await db.fetch_one(sql, tuple(params))
    if row is None:
        return {"p50_ttft_ms": 0.0, "p99_ttft_ms": 0.0}
    d = dict(row)
    return {
        "p50_ttft_ms": float(d.get("p50_ttft_ms") or 0.0),
        "p99_ttft_ms": float(d.get("p99_ttft_ms") or 0.0),
    }


async def fetch_provider_model_ttft(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-provider, per-model TTFT breakdown (streamed requests only).

    Deprecated models that have been relinked to the placeholder are
    reported under their ``original_model_id`` so the dashboard
    shows historical usage under the real model name.
    """
    sql = """
    WITH ranked AS (
        SELECT
            r.provider_id,
            COALESCE(r.original_model_id, r.model_id) AS model_id,
            r.first_byte_ms,
            ROW_NUMBER() OVER (
                PARTITION BY r.provider_id,
                    COALESCE(r.original_model_id, r.model_id)
                ORDER BY r.first_byte_ms
            ) as rn,
            COUNT(*) OVER (
                PARTITION BY r.provider_id,
                    COALESCE(r.original_model_id, r.model_id)
            ) as group_count
        FROM requests r
        WHERE r.streamed = 1
          AND r.first_byte_ms IS NOT NULL
          AND r.started_at >= ? AND r.started_at < ?
    )
    SELECT
        provider_id,
        model_id,
        COUNT(*) as request_count,
        COALESCE(AVG(first_byte_ms), 0) as avg_ttft_ms,
        COALESCE(AVG(CASE
            WHEN rn IN (
                CAST((group_count + 1) / 2 AS INTEGER),
                CAST((group_count + 2) / 2 AS INTEGER)
            )
            THEN first_byte_ms END), 0) as p50_ttft_ms,
        COALESCE(MAX(CASE
            WHEN rn = CAST(CEIL(0.99 * group_count) AS INTEGER)
            THEN first_byte_ms END), 0) as p99_ttft_ms
    FROM ranked
    GROUP BY provider_id, model_id
    ORDER BY provider_id, request_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_provider_ttft_summary(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-provider TTFT aggregate (streamed requests only)."""
    sql = """
    WITH ranked AS (
        SELECT
            r.provider_id,
            r.first_byte_ms,
            ROW_NUMBER() OVER (
                PARTITION BY r.provider_id ORDER BY r.first_byte_ms
            ) as rn,
            COUNT(*) OVER (PARTITION BY r.provider_id) as group_count
        FROM requests r
        WHERE r.streamed = 1
          AND r.first_byte_ms IS NOT NULL
          AND r.started_at >= ? AND r.started_at < ?
    )
    SELECT
        provider_id,
        COUNT(*) as request_count,
        COALESCE(AVG(first_byte_ms), 0) as avg_ttft_ms,
        COALESCE(AVG(CASE
            WHEN rn IN (
                CAST((group_count + 1) / 2 AS INTEGER),
                CAST((group_count + 2) / 2 AS INTEGER)
            )
            THEN first_byte_ms END), 0) as p50_ttft_ms,
        COALESCE(MAX(CASE
            WHEN rn = CAST(CEIL(0.99 * group_count) AS INTEGER)
            THEN first_byte_ms END), 0) as p99_ttft_ms
    FROM ranked
    GROUP BY provider_id
    ORDER BY provider_id
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_latency_phase_breakdown(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Aggregate latency-phase decomposition across all requests.

    Returns the four-corner phase totals:
        - ``upstream_connect_ms`` (DNS/TCP/TLS/send)
        - ``upstream_read_ms``    (TTFB minus connect)
        - ``coordinator_overhead_ms`` (eggpool-side: routing, retry, encode)
        - ``total_ms`` (sum of the three)

    Each phase is returned with ``avg``, ``p50``, and ``p99`` computed
    independently.  Phase values are NULL for rows that pre-date the
    0029 migration; those rows are silently dropped from each phase
    aggregate (the per-phase count is exposed so the dashboard can
    warn when coverage is low).
    """
    phases = (
        "upstream_connect_ms",
        "upstream_read_ms",
        "coordinator_overhead_ms",
        "first_byte_ms",
        "upstream_latency_ms",
    )
    result: dict[str, Any] = {
        "phases": {},
        "request_count": 0,
        "window_start": start,
        "window_end": end,
    }
    for phase in phases:
        sql = f"""
        WITH ranked AS (
            SELECT
                {phase} AS value,
                ROW_NUMBER() OVER (ORDER BY {phase}) AS rn,
                COUNT(*) OVER () AS group_count
            FROM requests
            WHERE started_at >= ? AND started_at <= ?
              AND {phase} IS NOT NULL
        )
        SELECT
            COUNT(*) AS sample_count,
            COALESCE(AVG(value), 0) AS avg_ms,
            COALESCE(AVG(CASE
                WHEN rn IN (
                    CAST((group_count + 1) / 2 AS INTEGER),
                    CAST((group_count + 2) / 2 AS INTEGER)
                )
                THEN value END), 0) AS p50_ms,
            COALESCE(MAX(CASE
                WHEN rn = CAST(CEIL(0.99 * group_count) AS INTEGER)
                THEN value END), 0) AS p99_ms
        FROM ranked
        """
        rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
        if rows:
            row = dict(rows[0])
            result["phases"][phase] = {
                "sample_count": int(row["sample_count"]),
                "avg_ms": float(row["avg_ms"]),
                "p50_ms": float(row["p50_ms"]),
                "p99_ms": float(row["p99_ms"]),
            }
        else:
            result["phases"][phase] = {
                "sample_count": 0,
                "avg_ms": 0.0,
                "p50_ms": 0.0,
                "p99_ms": 0.0,
            }
    # Overall request count for the window (regardless of phase coverage).
    count_rows = await db.fetch_all(
        "SELECT COUNT(*) AS c FROM requests WHERE started_at >= ? AND started_at <= ?",
        (_format_dt(start), _format_dt(end)),
    )
    if count_rows:
        result["request_count"] = int(count_rows[0]["c"])
    return result


async def fetch_ip_stats(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Get per-IP statistics for a time window."""
    sql = """
    SELECT
        COALESCE(client_ip, 'unknown') as client_ip,
        COUNT(*) as request_count,
        COALESCE(SUM(input_tokens), 0) as input_tokens,
        COALESCE(SUM(output_tokens), 0) as output_tokens,
        COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
            as total_tokens,
        COALESCE(SUM(cost_microdollars), 0) as cost_microdollars,
        COALESCE(AVG(upstream_latency_ms), 0) as avg_latency_ms,
        COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0)
            as error_count,
        COUNT(DISTINCT model_id) as unique_models,
        MIN(started_at) as first_request_at,
        MAX(started_at) as last_request_at
    FROM requests
    WHERE started_at >= ? AND started_at < ?
    GROUP BY client_ip
    ORDER BY request_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_attempt_stats(
    db: Database,
    start: str,
    end: str,
    *,
    account_id: int | None = None,
    model_id: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate per-attempt statistics over a time window.

    Returns a dict with total_attempts, retry_attempts, success_attempts,
    avg_attempt_latency_ms, p50/p99 attempt latency, and totals for
    bytes_received/bytes_emitted summed across all attempts.

    Per-attempt analytics matter because the same logical request can
    produce multiple attempt rows when failover fires.  Attempt-level
    totals expose retry pressure that request-level aggregates hide.
    """
    filters = ["ra.started_at >= ?", "ra.started_at < ?"]
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    if account_id is not None:
        filters.append("ra.account_id = ?")
        params.append(account_id)
    if model_id is not None:
        filters.append("(ra.model_id = ? OR ra.model_id IS NULL)")
        params.append(model_id)
    if provider_id is not None:
        filters.append("ra.provider_id = ?")
        params.append(provider_id)
    where_clause = " AND ".join(filters)

    aggregate_sql = f"""
    SELECT
        COUNT(*) as total_attempts,
        COALESCE(SUM(CASE WHEN ra.is_retry_outcome = 1 THEN 1 ELSE 0 END), 0)
            as retry_attempts,
        COALESCE(SUM(CASE WHEN ra.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0)
            as success_attempts,
        COALESCE(SUM(CASE WHEN ra.status_code >= 400 OR ra.error_class IS NOT NULL
            THEN 1 ELSE 0 END), 0) as failed_attempts,
        COALESCE(AVG(ra.latency_ms), 0) as avg_attempt_latency_ms,
        COALESCE(SUM(ra.bytes_received), 0) as total_attempt_bytes_received,
        COALESCE(SUM(ra.bytes_emitted), 0) as total_attempt_bytes_emitted,
        COALESCE(SUM(CASE WHEN ra.streamed = 1 THEN 1 ELSE 0 END), 0)
            as streamed_attempts
    FROM request_attempts ra
    WHERE {where_clause}
    """
    aggregate_row = await db.fetch_one(aggregate_sql, tuple(params))
    if aggregate_row is None:
        return _empty_attempt_stats()

    percentile_sql = f"""
    SELECT
        AVG(CASE WHEN sub.rn IN (
                CAST((sub.total_count + 1) / 2 AS INTEGER),
                CAST((sub.total_count + 2) / 2 AS INTEGER)
            ) THEN sub.latency_ms END) as p50_attempt_latency_ms,
        MAX(CASE WHEN sub.rn = sub.p99_idx THEN sub.latency_ms END)
            as p99_attempt_latency_ms
    FROM (
        SELECT
            ra.latency_ms,
            ROW_NUMBER() OVER (ORDER BY ra.latency_ms) as rn,
            COUNT(*) OVER () as total_count,
            CAST(CEIL(0.99 * COUNT(*) OVER ()) AS INTEGER) as p99_idx
        FROM request_attempts ra
        WHERE ra.latency_ms > 0 AND {where_clause}
    ) sub
    WHERE sub.rn IN (
            CAST((sub.total_count + 1) / 2 AS INTEGER),
            CAST((sub.total_count + 2) / 2 AS INTEGER)
        )
       OR sub.rn = sub.p99_idx
    """
    percentile_row = await db.fetch_one(percentile_sql, tuple(params))
    aggregate = dict(aggregate_row)
    if percentile_row is not None:
        pr = dict(percentile_row)
        aggregate["p50_attempt_latency_ms"] = float(
            pr.get("p50_attempt_latency_ms") or 0.0
        )
        aggregate["p99_attempt_latency_ms"] = float(
            pr.get("p99_attempt_latency_ms") or 0.0
        )
    else:
        aggregate["p50_attempt_latency_ms"] = 0.0
        aggregate["p99_attempt_latency_ms"] = 0.0

    aggregate["total_attempts"] = int(aggregate.get("total_attempts", 0) or 0)
    aggregate["retry_attempts"] = int(aggregate.get("retry_attempts", 0) or 0)
    aggregate["success_attempts"] = int(aggregate.get("success_attempts", 0) or 0)
    aggregate["failed_attempts"] = int(aggregate.get("failed_attempts", 0) or 0)
    aggregate["streamed_attempts"] = int(aggregate.get("streamed_attempts", 0) or 0)
    aggregate["avg_attempt_latency_ms"] = float(
        aggregate.get("avg_attempt_latency_ms", 0.0) or 0.0
    )
    aggregate["total_attempt_bytes_received"] = int(
        aggregate.get("total_attempt_bytes_received", 0) or 0
    )
    aggregate["total_attempt_bytes_emitted"] = int(
        aggregate.get("total_attempt_bytes_emitted", 0) or 0
    )
    if aggregate["total_attempts"] > 0:
        aggregate["retry_rate"] = (
            aggregate["retry_attempts"] / aggregate["total_attempts"]
        )
    else:
        aggregate["retry_rate"] = 0.0
    return aggregate


def _empty_attempt_stats() -> dict[str, Any]:
    """Zero-valued attempt stats."""
    return {
        "total_attempts": 0,
        "retry_attempts": 0,
        "success_attempts": 0,
        "failed_attempts": 0,
        "streamed_attempts": 0,
        "avg_attempt_latency_ms": 0.0,
        "p50_attempt_latency_ms": 0.0,
        "p99_attempt_latency_ms": 0.0,
        "retry_rate": 0.0,
        "total_attempt_bytes_received": 0,
        "total_attempt_bytes_emitted": 0,
    }


async def fetch_retry_distribution(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Distribution of attempts by retry_category.

    Each row reports ``retry_category``, ``attempt_count``,
    ``retry_outcome_count`` (attempts that were flagged as
    triggering a retry), and ``avg_attempt_latency_ms``.  Useful for
    "what kind of errors is the proxy hitting?" dashboards.
    """
    sql = """
    SELECT
        COALESCE(ra.retry_category, 'unclassified') as retry_category,
        COUNT(*) as attempt_count,
        COALESCE(SUM(CASE WHEN ra.is_retry_outcome = 1 THEN 1 ELSE 0 END), 0)
            as retry_outcome_count,
        COALESCE(AVG(ra.latency_ms), 0) as avg_attempt_latency_ms,
        COALESCE(SUM(CASE WHEN ra.status_code BETWEEN 200 AND 299
            THEN 1 ELSE 0 END), 0) as success_count,
        COALESCE(SUM(CASE WHEN ra.status_code >= 400 OR ra.error_class IS NOT NULL
            THEN 1 ELSE 0 END), 0) as failure_count
    FROM request_attempts ra
    WHERE ra.started_at >= ? AND ra.started_at < ?
    GROUP BY COALESCE(ra.retry_category, 'unclassified')
    ORDER BY attempt_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_request_attempts(
    db: Database,
    request_id: int,
) -> list[dict[str, Any]]:
    """Get the full attempt chain for one request.

    Returns rows ordered by ``attempt_number`` ASC.  Used by the
    /api/stats/recent/{request_id} trace endpoint and by the
    dashboard's per-request drill-down.
    """
    rows = await db.fetch_all(
        "SELECT "
        "ra.id, ra.request_id, ra.attempt_number, ra.account_id, "
        "a.name as account_name, ra.provider_id, ra.model_id, "
        "ra.protocol, ra.started_at, ra.completed_at, "
        "ra.status_code, ra.error_class, ra.error_detail, "
        "ra.upstream_request_id, ra.bytes_received, ra.bytes_emitted, "
        "ra.latency_ms, ra.streamed, ra.retry_category, "
        "ra.release_reason, ra.is_retry_outcome "
        "FROM request_attempts ra "
        "LEFT JOIN accounts a ON a.id = ra.account_id "
        "WHERE ra.request_id = ? "
        "ORDER BY ra.attempt_number ASC",
        (request_id,),
    )
    return [dict(row) for row in rows]


async def fetch_request_trace(
    db: Database,
    request_id: int,
) -> dict[str, Any] | None:
    """Fetch the parent request row plus its full attempt chain.

    Returns ``None`` when no such request exists; otherwise returns a
    dict with ``request`` (the parent row) and ``attempts`` (the
    attempt chain).  Used by the per-request trace endpoint.
    """
    request_row = await db.fetch_one(
        "SELECT "
        "r.*, "
        "a.name as account_name, "
        "COALESCE(r.original_model_id, r.model_id) as resolved_model_id "
        "FROM requests r LEFT JOIN accounts a ON a.id = r.account_id "
        "WHERE r.id = ?",
        (request_id,),
    )
    if request_row is None:
        return None
    attempts = await fetch_request_attempts(db, request_id)
    return {
        "request": dict(request_row),
        "attempts": attempts,
    }


async def fetch_routing_decisions_for_request(
    db: Database,
    request_id: int,
) -> list[dict[str, Any]]:
    """Return all routing decisions for one request, ordered by attempt."""
    rows = await db.fetch_all(
        "SELECT * FROM routing_decisions WHERE request_id = ? ORDER BY attempt_number",
        (request_id,),
    )
    return [dict(row) for row in rows]


async def fetch_routing_distribution(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-model routing distribution.

    Each row reports ``model_id``, ``provider_id``, ``decision_count``,
    average and p50/p99 ``eligible_count``, ``scored_count``, and
    ``attempted_excluded_count`` plus a per-account histogram of how
    often each account was selected.

    Uses ``<=`` for the end filter so a row inserted in the same second
    as the time-range boundary is included.  ``format_dt`` truncates
    fractional seconds, so the request-side boundary string can match a
    stored ``decision_made_at`` exactly; a strict ``<`` would drop that
    row and the 1-second slop is harmless for dashboard analytics.
    """
    sql = """
    SELECT
        model_id,
        provider_id,
        COUNT(*) as decision_count,
        COALESCE(AVG(eligible_count), 0) as avg_eligible_count,
        COALESCE(AVG(scored_count), 0) as avg_scored_count,
        COALESCE(AVG(attempted_excluded_count), 0)
            as avg_attempted_excluded_count,
        COALESCE(AVG(selected_score), 0) as avg_selected_score,
        COUNT(DISTINCT selected_account_name) as distinct_selected_accounts
    FROM routing_decisions
    WHERE decision_made_at >= ? AND decision_made_at <= ?
    GROUP BY model_id, provider_id
    ORDER BY decision_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_routing_selection_breakdown(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Account-level selection counts from routing_decisions.

    Useful for "how often does each account get selected?" charts.
    Uses ``<=`` for the end filter (see fetch_routing_distribution).
    """
    sql = """
    SELECT
        COALESCE(selected_account_name, 'unknown') as account_name,
        provider_id,
        COUNT(*) as selection_count,
        COALESCE(AVG(selected_tier), 0) as avg_selected_tier,
        COALESCE(AVG(selected_score), 0) as avg_selected_score,
        COALESCE(AVG(eligible_count), 0) as avg_eligible_count
    FROM routing_decisions
    WHERE decision_made_at >= ? AND decision_made_at <= ?
    GROUP BY selected_account_name, provider_id
    ORDER BY selection_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_routing_exclusion_breakdown(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Distribution of exclusion reasons parsed from ``exclude_reasons_json``.

    Returns one row per ``(account_name, reason)`` with a count.  Rows
    come from the JSON array in each routing_decisions row, so the
    parser unpacks ``reason`` per element before aggregating.
    Uses ``<=`` for the end filter (see fetch_routing_distribution).
    """
    sql = """
    SELECT
        json_extract(value, '$.account') as account_name,
        json_extract(value, '$.reason') as reason,
        COUNT(*) as exclusion_count,
        MAX(rd.decision_made_at) as last_seen_at
    FROM routing_decisions rd,
         json_each(rd.exclude_reasons_json)
    WHERE rd.decision_made_at >= ? AND rd.decision_made_at <= ?
      AND json_array_length(rd.exclude_reasons_json) > 0
    GROUP BY account_name, reason
    ORDER BY exclusion_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_operational_event_summary(
    db: Database,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Per-event-type summary of operational_events rows.

    Returns one row per ``event_type`` with ``event_count`` and
    ``last_occurred_at`` plus a numeric breakdown of the typical
    payload keys (``interrupted_requests``, ``leaked_requests``,
    ``released_reservations``, ``affected_accounts``,
    ``expired_reservations``).  Missing JSON keys return 0.
    """
    sql = """
    SELECT
        event_type,
        COUNT(*) as event_count,
        MAX(occurred_at) as last_occurred_at,
        COALESCE(
            SUM(CAST(json_extract(details_json,
                '$.interrupted_requests') AS INTEGER)),
            0
        ) as total_interrupted_requests,
        COALESCE(
            SUM(CAST(json_extract(details_json,
                '$.leaked_requests') AS INTEGER)),
            0
        ) as total_leaked_requests,
        COALESCE(
            SUM(CAST(json_extract(details_json,
                '$.released_reservations') AS INTEGER)),
            0
        ) as total_released_reservations,
        COALESCE(
            SUM(CAST(json_extract(details_json,
                '$.affected_accounts') AS INTEGER)),
            0
        ) as total_affected_accounts,
        COALESCE(
            SUM(CAST(json_extract(details_json,
                '$.expired_reservations') AS INTEGER)),
            0
        ) as total_expired_reservations
    FROM operational_events
    WHERE occurred_at >= ? AND occurred_at <= ?
    GROUP BY event_type
    ORDER BY event_count DESC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [dict(row) for row in rows]


async def fetch_recent_operational_events(
    db: Database,
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Most recent operational_events rows, optionally filtered by type."""
    params: list[Any] = []
    type_filter = ""
    if event_type is not None:
        type_filter = " WHERE event_type = ?"
        params.append(event_type)
    sql = f"""
    SELECT id, event_type, details_json, occurred_at
    FROM operational_events{type_filter}
    ORDER BY occurred_at DESC
    LIMIT ?
    """
    params.append(limit)
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_recent_requests(
    db: Database,
    limit: int = 50,
    account_id: int | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    status: str | None = None,
    include_client_ip: bool = False,
) -> list[dict[str, Any]]:
    """Recent request rows for the bounded debugging view.

    Returns metadata only — no prompt, body, error_detail, or auth
    headers.  Error class is returned (not the raw upstream detail
    string), and client_ip is omitted unless the operator has
    explicitly enabled IP stats (``include_client_ip=True``).

    Filters compose with AND.  ``limit`` is clamped to [1, 200].
    """
    limit = max(1, min(int(limit), 200))
    conditions: list[str] = []
    params: list[Any] = []
    if account_id is not None:
        conditions.append("r.account_id = ?")
        params.append(int(account_id))
    if provider_id is not None:
        conditions.append("r.provider_id = ?")
        params.append(provider_id)
    if model_id is not None:
        conditions.append("(r.model_id = ? OR r.original_model_id = ?)")
        params.extend([model_id, model_id])
    if status is not None:
        conditions.append("r.status = ?")
        params.append(status)
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
    SELECT
        r.id as request_id,
        r.proxy_request_id,
        r.upstream_request_id,
        r.started_at,
        r.completed_at,
        r.account_id,
        (SELECT name FROM accounts WHERE id = r.account_id) AS account_name,
        r.provider_id,
        COALESCE(r.original_model_id, r.model_id) AS model_id,
        r.protocol,
        r.status,
        r.status_code,
        r.error_class,
        r.input_tokens,
        r.output_tokens,
        r.cache_read_tokens,
        r.cache_write_tokens,
        r.reasoning_tokens,
        r.thinking_characters,
        r.cost_microdollars,
        r.exactness,
        r.first_byte_ms,
        r.upstream_latency_ms,
        r.retry_count,
        r.bytes_received,
        r.bytes_emitted,
        r.streamed,
        {"r.client_ip" if include_client_ip else "NULL"} AS client_ip
    FROM requests r
    {where_clause}
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT ?
    """
    params.append(limit)
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_pricing_provenance_stats(
    db: Database,
) -> list[dict[str, Any]]:
    """Aggregate pricing provenance from the latest snapshot per model.

    Returns one row per ``(model_id, provider_id, source_detail,
    catalog_source)`` tuple, including the most recent captured_at and
    a count of categories (input/output/cache_read/cache_write) that
    carry a non-null microdollar rate. Used by the dashboard to surface
    how much of the catalog is exact upstream metadata vs. curated
    alias vs. ambiguous-skip.
    """
    sql = """
    WITH latest AS (
        SELECT
            model_price_snapshots.*,
            ROW_NUMBER() OVER(
                PARTITION BY model_id, provider_id
                ORDER BY captured_at DESC, id DESC
            ) AS snapshot_rank
        FROM model_price_snapshots
    )
    SELECT
        model_id,
        provider_id,
        COALESCE(source_detail, '(unknown)') AS source_detail,
        COALESCE(source_confidence, '(unknown)') AS source_confidence,
        COALESCE(catalog_source, source) AS catalog_source,
        source AS aggregate_source,
        captured_at,
        (
            CASE WHEN input_per_million_microdollars IS NOT NULL THEN 1 ELSE 0 END
            + CASE
                WHEN output_per_million_microdollars IS NOT NULL THEN 1 ELSE 0 END
            + CASE WHEN cache_read_per_million_microdollars IS NOT NULL
                THEN 1 ELSE 0 END
            + CASE WHEN cache_write_per_million_microdollars IS NOT NULL
                THEN 1 ELSE 0 END
        ) AS categories_priced,
        (
            COALESCE(input_per_million_microdollars, 0)
            + COALESCE(output_per_million_microdollars, 0)
        ) AS anchor_rate_microdollars
    FROM latest
    WHERE snapshot_rank = 1
    ORDER BY model_id
    """
    rows = await db.fetch_all(sql)
    return [dict(row) for row in rows]


async def fetch_transcoding_stats(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Get protocol transcoding statistics for a time window.

    Returns a dict with:
    - total_requests: total requests in the window
    - native_count: requests where client protocol == upstream protocol (no transcoding)
    - transcoded_count: requests where client protocol != upstream protocol
    - per_direction: dict mapping (client_proto, upstream_proto) to count
    - top_loss_warnings: list of (warning_kind, count) sorted descending
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # Total and native counts
    count_sql = """
    SELECT
        COUNT(*) as total,
        COALESCE(SUM(CASE WHEN protocol = COALESCE(upstream_protocol, protocol)
            THEN 1 ELSE 0 END), 0) as native_count,
        COALESCE(SUM(CASE WHEN protocol != COALESCE(upstream_protocol, protocol)
            AND upstream_protocol IS NOT NULL
            THEN 1 ELSE 0 END), 0) as transcoded_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
    """
    count_row = await db.fetch_one(count_sql, (start_dt, end_dt))
    total = dict(count_row)["total"] if count_row else 0
    native_count = dict(count_row)["native_count"] if count_row else 0
    transcoded_count = dict(count_row)["transcoded_count"] if count_row else 0

    # Per-direction breakdown
    direction_sql = """
    SELECT
        protocol as client_protocol,
        upstream_protocol,
        COUNT(*) as count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
      AND upstream_protocol IS NOT NULL
      AND protocol != upstream_protocol
    GROUP BY protocol, upstream_protocol
    ORDER BY count DESC
    """
    direction_rows = await db.fetch_all(direction_sql, (start_dt, end_dt))
    per_direction: dict[tuple[str, str], int] = {}
    for row in direction_rows:
        rd = dict(row)
        key = (rd["client_protocol"], rd["upstream_protocol"])
        per_direction[key] = rd["count"]

    # Top loss warnings aggregation. Per-request loss warning counts are
    # not yet persisted to the ``requests`` table, so this is an empty
    # list until that schema work lands; the UI handles the empty
    # state explicitly.
    top_loss_warnings: list[dict[str, Any]] = []
    return {
        "total": total,
        "native_count": native_count,
        "transcoded_count": transcoded_count,
        "per_direction": per_direction,
        "top_loss_warnings": top_loss_warnings,
    }
