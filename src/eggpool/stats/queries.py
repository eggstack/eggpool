"""Statistics query layer for SQLite aggregations.

Provides parameterized SQL queries for the statistics API and dashboard.
SQL logic lives here, not in HTTP route handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

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


def bounded_cache_ratio(
    cache_read: float, input_tokens: float, cache_write: float
) -> float | None:
    """Return cache_read share of (input + cache_read + cache_write).

    Bounded in ``[0, 1]`` so dashboards never display ``> 100%``.  Returns
    ``None`` when the denominator is zero (no usage) so the UI can render
    an em-dash instead of a misleading ``0.0%``.
    """
    denom = input_tokens + cache_read + cache_write
    if denom <= 0:
        return None
    return cache_read / denom


def bounded_cache_write_ratio(
    cache_write: float, input_tokens: float, cache_read: float
) -> float | None:
    """Return cache_write share of (input + cache_read + cache_write).

    Bounded in ``[0, 1]``.  Returns ``None`` when the denominator is zero.
    """
    denom = input_tokens + cache_read + cache_write
    if denom <= 0:
        return None
    return cache_write / denom


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
        -- Reservation-fallback canonicalization visibility. A row
        -- whose canonical ``cost_microdollars`` equals its
        -- ``reserved_microdollars`` while carrying a non-null,
        -- smaller ``local_cost_microdollars`` is the exact failure
        -- mode plans/2026-07-03-reservation-fallback-cost-canonicalization-fix
        -- targets: the routing reservation silently overrode the
        -- tighter local estimate. Operators use ``reservation_fallback_*``
        -- to spot inflated spend and ``reservation_fallback_excess_usd``
        -- to quantify the gap for repair tooling.
        COALESCE(SUM(CASE WHEN exactness = 'estimated'
            AND reserved_microdollars IS NOT NULL
            AND cost_microdollars = reserved_microdollars
            AND local_cost_microdollars IS NOT NULL
            AND local_cost_microdollars > 0
            AND local_cost_microdollars < cost_microdollars
            THEN 1 ELSE 0 END), 0)
            as reservation_fallback_rows,
        COALESCE(SUM(CASE WHEN exactness = 'estimated'
            AND reserved_microdollars IS NOT NULL
            AND cost_microdollars = reserved_microdollars
            AND local_cost_microdollars IS NOT NULL
            AND local_cost_microdollars > 0
            AND local_cost_microdollars < cost_microdollars
            THEN cost_microdollars - local_cost_microdollars
            ELSE 0 END), 0)
            as reservation_fallback_excess_microdollars,
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
            WHEN (COALESCE(ps.input_tokens, 0)
                  + COALESCE(ps.cache_read_tokens, 0)
                  + COALESCE(ps.cache_write_tokens, 0)) > 0
            THEN CAST(COALESCE(ps.cache_read_tokens, 0) AS REAL)
                / (COALESCE(ps.input_tokens, 0)
                   + COALESCE(ps.cache_read_tokens, 0)
                   + COALESCE(ps.cache_write_tokens, 0))
            ELSE NULL
        END as cache_read_ratio,
        CASE
            WHEN (COALESCE(ps.input_tokens, 0)
                  + COALESCE(ps.cache_read_tokens, 0)
                  + COALESCE(ps.cache_write_tokens, 0)) > 0
            THEN CAST(COALESCE(ps.cache_write_tokens, 0) AS REAL)
                / (COALESCE(ps.input_tokens, 0)
                   + COALESCE(ps.cache_read_tokens, 0)
                   + COALESCE(ps.cache_write_tokens, 0))
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
            WHEN (COALESCE(SUM(r.input_tokens), 0)
                  + COALESCE(SUM(r.cache_read_tokens), 0)
                  + COALESCE(SUM(r.cache_write_tokens), 0)) > 0
            THEN CAST(COALESCE(SUM(r.cache_read_tokens), 0) AS REAL)
                / (COALESCE(SUM(r.input_tokens), 0)
                   + COALESCE(SUM(r.cache_read_tokens), 0)
                   + COALESCE(SUM(r.cache_write_tokens), 0))
            ELSE NULL
        END as cache_read_ratio,
        CASE
            WHEN (COALESCE(SUM(r.input_tokens), 0)
                  + COALESCE(SUM(r.cache_read_tokens), 0)
                  + COALESCE(SUM(r.cache_write_tokens), 0)) > 0
            THEN CAST(COALESCE(SUM(r.cache_write_tokens), 0) AS REAL)
                / (COALESCE(SUM(r.input_tokens), 0)
                   + COALESCE(SUM(r.cache_read_tokens), 0)
                   + COALESCE(SUM(r.cache_write_tokens), 0))
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
    start: str = "",
    end: str = "",
) -> list[dict[str, Any]]:
    """Get recent account events, optionally filtered by type and time range.

    ``start`` and ``end`` are inclusive/exclusive ISO 8601 bounds against
    ``account_events.created_at``; either or both may be empty to leave
    that side unbounded. They compose with ``event_type`` via AND.
    """
    params: list[Any] = []
    conditions: list[str] = []
    if event_type is not None:
        conditions.append("ae.event_type = ?")
        params.append(event_type)
    if start:
        conditions.append("ae.created_at >= ?")
        params.append(_format_dt(start))
    if end:
        conditions.append("ae.created_at < ?")
        params.append(_format_dt(end))
    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""

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
    {where_clause}
    ORDER BY ae.created_at DESC
    LIMIT ?
    """
    params.append(limit)
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(row) for row in rows]


async def fetch_event_types_in_range(
    db: Database,
    start: str,
    end: str,
) -> list[str]:
    """Distinct ``event_type`` values present in ``account_events`` for the window.

    Returned alphabetically so the dropdown is stable across renders.
    Used by the events page to populate the type filter with only the
    values that actually occur within the selected period.
    """
    sql = """
    SELECT DISTINCT ae.event_type AS event_type
    FROM account_events ae
    WHERE ae.created_at >= ? AND ae.created_at < ?
    ORDER BY ae.event_type ASC
    """
    rows = await db.fetch_all(sql, (_format_dt(start), _format_dt(end)))
    return [str(row["event_type"]) for row in rows]


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


async def fetch_latest_started_at(
    db: Database,
    start: str,
    end: str,
    *,
    account_id: int | None = None,
) -> str | None:
    """Return the most recent ``started_at`` in the requests table
    within ``[start, end)``.

    Used by ``StatsService._rollup_is_fresh`` to compare against the
    rollup table's latest ``bucket_start`` so a stalled coalescer can't
    cause the dashboard to under-report the in-flight hour.  Returns
    ``None`` when no rows exist in the window.
    """
    params: list[Any] = [_format_dt(start), _format_dt(end)]
    account_filter = ""
    if account_id is not None:
        account_filter = " AND account_id = ?"
        params.append(account_id)
    row = await db.fetch_one(
        f"SELECT MAX(started_at) AS latest FROM requests "
        f"WHERE started_at >= ? AND started_at < ?{account_filter}",
        tuple(params),
    )
    if row is None:
        return None
    latest = row["latest"]
    if latest is None:
        return None
    return str(latest)


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
    total_cache_write_tokens = int(row.get("total_cache_write_tokens", 0))
    total_output_tokens = int(row.get("total_output_tokens", 0))
    cache_read_ratio = bounded_cache_ratio(
        total_cache_read_tokens, total_input_tokens, total_cache_write_tokens
    )
    return {
        "total_requests": total,
        "successful_requests": int(row.get("successful_requests", 0)),
        "error_requests": errors,
        "error_rate": error_rate,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": int(row.get("total_tokens", 0)),
        "fresh_tokens": total_input_tokens + total_output_tokens,
        "accounted_tokens": (
            total_input_tokens
            + total_output_tokens
            + total_cache_read_tokens
            + total_cache_write_tokens
        ),
        "total_cost_microdollars": int(row.get("total_cost_microdollars", 0)),
        "avg_latency_ms": float(row.get("avg_latency_ms", 0.0)),
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_cache_write_tokens": total_cache_write_tokens,
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
        # Reservation-fallback canonicalization visibility. See
        # plans/2026-07-03-reservation-fallback-cost-canonicalization-fix.
        # When ``reservation_fallback_rows > 0`` the dashboard renders
        # a notice and the repair CLI surfaces the offending rows.
        "reservation_fallback_rows": int(row.get("reservation_fallback_rows", 0)),
        "reservation_fallback_excess_microdollars": int(
            row.get("reservation_fallback_excess_microdollars", 0)
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
        "fresh_tokens": 0,
        "accounted_tokens": 0,
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
        "reservation_fallback_rows": 0,
        "reservation_fallback_excess_microdollars": 0,
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
    Includes ``last_selected_at`` and ``last_selected_score`` from the
    most recent routing decision per account so the dashboard can show
    when each account was last chosen and what score it received.
    Uses ``<=`` for the end filter (see fetch_routing_distribution).
    """
    sql = """
    SELECT
        COALESCE(rd.selected_account_name, 'unknown') as account_name,
        rd.provider_id,
        COUNT(*) as selection_count,
        COALESCE(AVG(rd.selected_tier), 0) as avg_selected_tier,
        COALESCE(AVG(rd.selected_score), 0) as avg_selected_score,
        COALESCE(AVG(rd.eligible_count), 0) as avg_eligible_count,
        (SELECT sub.decision_made_at
         FROM routing_decisions sub
         WHERE sub.selected_account_name = rd.selected_account_name
           AND sub.decision_made_at >= ? AND sub.decision_made_at <= ?
         ORDER BY sub.decision_made_at DESC
         LIMIT 1) as last_selected_at,
        (SELECT sub.selected_score
         FROM routing_decisions sub
         WHERE sub.selected_account_name = rd.selected_account_name
           AND sub.decision_made_at >= ? AND sub.decision_made_at <= ?
         ORDER BY sub.decision_made_at DESC
         LIMIT 1) as last_selected_score,
        (SELECT sub.selected_tier
         FROM routing_decisions sub
         WHERE sub.selected_account_name = rd.selected_account_name
           AND sub.decision_made_at >= ? AND sub.decision_made_at <= ?
         ORDER BY sub.decision_made_at DESC
         LIMIT 1) as last_selected_tier
    FROM routing_decisions rd
    WHERE rd.decision_made_at >= ? AND rd.decision_made_at <= ?
    GROUP BY rd.selected_account_name, rd.provider_id
    ORDER BY selection_count DESC
    """
    fmt_start = _format_dt(start)
    fmt_end = _format_dt(end)
    rows = await db.fetch_all(
        sql,
        (
            fmt_start,
            fmt_end,
            fmt_start,
            fmt_end,
            fmt_start,
            fmt_end,
            fmt_start,
            fmt_end,
        ),
    )
    return [dict(row) for row in rows]


async def fetch_routing_skew_summary(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Routing selection skew summary across all accounts.

    Returns a single dict with aggregate metrics: total selections,
    number of distinct accounts selected, max/min selection counts,
    the ratio between them, and the names of the most/least selected
    accounts.  Used by the Routing dashboard page to surface skew at
    a glance.
    """
    sql = """
    WITH account_counts AS (
        SELECT
            COALESCE(selected_account_name, 'unknown') as account_name,
            COUNT(*) as cnt
        FROM routing_decisions
        WHERE decision_made_at >= ? AND decision_made_at <= ?
          AND selected_account_name IS NOT NULL
        GROUP BY selected_account_name
    )
    SELECT
        COALESCE(SUM(cnt), 0) as total_selections,
        COUNT(*) as distinct_accounts,
        COALESCE(MAX(cnt), 0) as max_selections,
        COALESCE(MIN(cnt), 0) as min_selections,
        (SELECT account_name FROM account_counts ORDER BY cnt DESC LIMIT 1)
            as most_selected_account,
        (SELECT account_name FROM account_counts ORDER BY cnt ASC LIMIT 1)
            as least_selected_account
    FROM account_counts
    """
    row = await db.fetch_one(sql, (_format_dt(start), _format_dt(end)))
    if row is None:
        return {
            "total_selections": 0,
            "distinct_accounts": 0,
            "max_selections": 0,
            "min_selections": 0,
            "skew_ratio": 0.0,
            "most_selected_account": None,
            "least_selected_account": None,
        }
    result = dict(row)
    max_s = int(result.get("max_selections", 0) or 0)
    min_s = int(result.get("min_selections", 0) or 0)
    result["skew_ratio"] = float(max_s) / float(min_s) if min_s > 0 else 0.0
    return result


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


async def fetch_cache_observability(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 1 cache-counter observability aggregates.

    Reads only the new ``cache_counter_status`` and supporting
    cache-token columns populated by
    :func:`eggpool.proxy.normalized_usage.normalize_usage`.  Returns a
    dict with:

    - ``total_requests``                  : total finalized rows in the
      window.  Also exposed as ``requests_total`` at top level.
    - ``by_status``                       : ``{"reported", "not_reported",
      "unknown_format"} -> request_count``.  The plan's literal field
      names ``cache_counter_reported_requests`` and
      ``cache_counter_unknown_requests`` are also surfaced as
      top-level keys for backward compatibility with the plan shape.
    - ``per_protocol_status``             : ``(provider_id,
      upstream_protocol) -> {"reported", "not_reported",
      "unknown_format"}``.
    - ``per_account_status``              : ``account_id -> {"reported",
      "not_reported", "unknown_format", "total_requests",
      "total_cached_input_tokens"}``.
    - ``per_model_status``                : ``model_id -> {"reported",
      "not_reported", "unknown_format", "total_requests",
      "total_cached_input_tokens"}``.
    - ``input_tokens_total``              : sum of ``input_tokens``
      across all rows (used by dashboards as a global denominator).
    - ``output_tokens_total``             : sum of ``output_tokens``
      across all rows.
    - ``total_cached_input_tokens``       : sum of
      ``cached_input_tokens`` across rows with
      status="reported".
    - ``total_cache_read_input_tokens``   : Anthropic-specific cache
      read.
    - ``total_cache_creation_input_tokens``: Anthropic-specific cache
      write.
    - ``cache_hit_ratio_known_only``      : ``cached_input_tokens /
      input_tokens`` restricted to rows where status="reported" so
      the ratio never silently mixes zero with missing.
    - ``transcoded_requests``             : rows where the request
      required protocol transcoding.

    All numeric fields default to 0; ratios default to ``None`` when
    the denominator is zero.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # --- per-status breakdown ---
    status_sql = """
    SELECT
        COALESCE(cache_counter_status, 'not_reported') as cache_counter_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY cache_counter_status
    """
    rows = await db.fetch_all(status_sql, (start_dt, end_dt))
    by_status = {
        "reported": 0,
        "not_reported": 0,
        "unknown_format": 0,
    }
    for row in rows:
        d = dict(row)
        status = d["cache_counter_status"]
        if status not in by_status:
            # Forward-compatible: unknown statuses are surfaced under
            # ``unknown_format`` so dashboards never silently drop rows.
            status = "unknown_format"
        by_status[status] = int(d["request_count"])

    # --- per (provider, upstream_protocol) breakdown ---
    protocol_status_sql = """
    SELECT
        COALESCE(provider_id, 'unknown') as provider_id,
        COALESCE(upstream_protocol, 'unknown') as upstream_protocol,
        COALESCE(cache_counter_status, 'not_reported') as cache_counter_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY provider_id, upstream_protocol, cache_counter_status
    """
    protocol_rows = await db.fetch_all(protocol_status_sql, (start_dt, end_dt))
    per_protocol_status: dict[tuple[str, str], dict[str, int]] = {}
    for row in protocol_rows:
        d = dict(row)
        key = (d["provider_id"], d["upstream_protocol"])
        bucket = per_protocol_status.setdefault(
            key,
            {"reported": 0, "not_reported": 0, "unknown_format": 0},
        )
        status = d["cache_counter_status"]
        if status not in bucket:
            status = "unknown_format"
        bucket[status] += int(d["request_count"])

    # --- per-account breakdown ---
    account_status_sql = """
    SELECT
        COALESCE(account_id, 0) as account_id,
        COALESCE(cache_counter_status, 'not_reported') as cache_counter_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cached_input_tokens ELSE 0 END), 0) as total_cached_input_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY account_id, cache_counter_status
    """
    account_rows = await db.fetch_all(account_status_sql, (start_dt, end_dt))
    _status_bucket: dict[str, int]  # type: ignore[annotation-unchecked]
    per_account_status: dict[int, dict[str, Any]] = {}
    for row in account_rows:
        d = dict(row)
        acct_id = int(d["account_id"])
        bucket = per_account_status.setdefault(
            acct_id,
            {
                "reported": 0,
                "not_reported": 0,
                "unknown_format": 0,
                "total_requests": 0,
                "total_cached_input_tokens": 0,
            },
        )
        status = d["cache_counter_status"]
        if status not in ("reported", "not_reported", "unknown_format"):
            status = "unknown_format"
        bucket[status] += int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["total_cached_input_tokens"] += int(d["total_cached_input_tokens"])

    # --- per-model breakdown ---
    model_status_sql = """
    SELECT
        COALESCE(model_id, 'unknown') as model_id,
        COALESCE(cache_counter_status, 'not_reported') as cache_counter_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cached_input_tokens ELSE 0 END), 0) as total_cached_input_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY model_id, cache_counter_status
    """
    model_rows = await db.fetch_all(model_status_sql, (start_dt, end_dt))
    per_model_status: dict[str, dict[str, Any]] = {}
    for row in model_rows:
        d = dict(row)
        mid = d["model_id"]
        bucket = per_model_status.setdefault(
            mid,
            {
                "reported": 0,
                "not_reported": 0,
                "unknown_format": 0,
                "total_requests": 0,
                "total_cached_input_tokens": 0,
            },
        )
        status = d["cache_counter_status"]
        if status not in ("reported", "not_reported", "unknown_format"):
            status = "unknown_format"
        bucket[status] += int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["total_cached_input_tokens"] += int(d["total_cached_input_tokens"])

    # --- global totals ---
    totals_sql = """
    SELECT
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cached_input_tokens ELSE 0 END), 0) as total_cached_input_tokens,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cache_read_input_tokens ELSE 0 END), 0)
            as total_cache_read_input_tokens,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cache_creation_input_tokens ELSE 0 END), 0)
            as total_cache_creation_input_tokens,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN cache_write_input_tokens ELSE 0 END), 0)
            as total_cache_write_input_tokens,
        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
        COALESCE(SUM(CASE WHEN cache_counter_status = 'reported'
            THEN input_tokens ELSE 0 END), 0) as total_input_tokens_reported,
        COALESCE(SUM(CASE WHEN transcoded = 1 THEN 1 ELSE 0 END), 0)
            as transcoded_requests
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    totals_row = await db.fetch_one(totals_sql, (start_dt, end_dt))
    totals = dict(totals_row) if totals_row else {}

    total_cached = int(totals.get("total_cached_input_tokens", 0) or 0)
    total_input_reported = int(totals.get("total_input_tokens_reported", 0) or 0)
    cache_hit_ratio_known_only = (
        total_cached / total_input_reported if total_input_reported > 0 else None
    )

    total = sum(by_status.values())

    return {
        # Top-level aliases matching the plan's literal field names.
        "requests_total": total,
        "input_tokens_total": int(totals.get("total_input_tokens", 0) or 0),
        "output_tokens_total": int(totals.get("total_output_tokens", 0) or 0),
        "cache_counter_reported_requests": by_status["reported"],
        "cache_counter_unknown_requests": by_status["unknown_format"],
        # Backward-compatible nested dicts kept for the dashboard.
        "total_requests": total,
        "by_status": by_status,
        "per_protocol_status": per_protocol_status,
        "per_account_status": per_account_status,
        "per_model_status": per_model_status,
        "total_cached_input_tokens": total_cached,
        "total_cache_read_input_tokens": int(
            totals.get("total_cache_read_input_tokens", 0) or 0
        ),
        "total_cache_creation_input_tokens": int(
            totals.get("total_cache_creation_input_tokens", 0) or 0
        ),
        "total_cache_write_input_tokens": int(
            totals.get("total_cache_write_input_tokens", 0) or 0
        ),
        "cache_hit_ratio_known_only": cache_hit_ratio_known_only,
        "transcoded_requests": int(totals.get("transcoded_requests", 0) or 0),
    }


async def fetch_canonical_request_segmentation(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 2 canonical request segmentation aggregates.

    Reads only the segmentation columns populated by
    :func:`eggpool.transcoder.segmentation.segment_request` and
    persisted by :meth:`RequestRepository.finalize_if_pending`.  The
    segmentation pass is observational: it never mutates the payload
    and never changes routing.  This query surfaces:

    - ``total_requests``              : total finalized rows in the window.
    - ``by_status``                   : ``{"segmented", "empty_request",
      "parse_failure"} -> request_count``.
    - ``per_provider_status``         : ``(provider_id, upstream_protocol) ->
      {"segmented", "empty_request", "parse_failure"}``.
    - ``per_model_status``            : ``model_id -> {"segmented",
      "empty_request", "parse_failure", "total_requests",
      "stable_prefix_estimated_tokens", "volatile_estimated_tokens"}``.
    - ``token_totals``                : ``{"stable_prefix",
      "semi_stable", "volatile", "all"} -> total_estimated_tokens`` across
      the window (None is treated as 0).
    - ``byte_totals``                 : ``{"stable_prefix", "semi_stable",
      "volatile", "all"} -> total_bytes`` across the window.
    - ``compressible_candidate_requests`` : count of rows that produced
      at least one ``compressible_candidate=True`` segment.
    - ``protected_requests``          : count of rows with
      ``stable_prefix_bytes > 0`` (i.e. a protected prefix exists).

    All numeric fields default to 0; ratios default to ``None`` when
    the denominator is zero.  This query is reading-only: it never
    mutates the database and never depends on request lifecycle state.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # --- per-status breakdown ---
    status_sql = """
    SELECT
        COALESCE(segmentation_status, 'empty_request') as segmentation_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY segmentation_status
    """
    rows = await db.fetch_all(status_sql, (start_dt, end_dt))
    by_status = {
        "segmented": 0,
        "empty_request": 0,
        "parse_failure": 0,
    }
    for row in rows:
        d = dict(row)
        status = d["segmentation_status"]
        if status not in by_status:
            status = "parse_failure"  # forward-compat
        by_status[status] = int(d["request_count"])

    # --- per (provider, upstream_protocol) breakdown ---
    protocol_status_sql = """
    SELECT
        COALESCE(provider_id, 'unknown') as provider_id,
        COALESCE(upstream_protocol, 'unknown') as upstream_protocol,
        COALESCE(segmentation_status, 'empty_request') as segmentation_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY provider_id, upstream_protocol, segmentation_status
    """
    protocol_rows = await db.fetch_all(protocol_status_sql, (start_dt, end_dt))
    per_provider_status: dict[tuple[str, str], dict[str, int]] = {}
    for row in protocol_rows:
        d = dict(row)
        key = (d["provider_id"], d["upstream_protocol"])
        bucket = per_provider_status.setdefault(
            key,
            {"segmented": 0, "empty_request": 0, "parse_failure": 0},
        )
        status = d["segmentation_status"]
        if status not in bucket:
            status = "parse_failure"
        bucket[status] += int(d["request_count"])

    # --- per-model breakdown ---
    model_status_sql = """
    SELECT
        COALESCE(model_id, 'unknown') as model_id,
        COALESCE(segmentation_status, 'empty_request') as segmentation_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN stable_prefix_estimated_tokens IS NOT NULL
            THEN stable_prefix_estimated_tokens ELSE 0 END), 0)
            as total_stable_prefix_estimated_tokens,
        COALESCE(SUM(CASE WHEN volatile_estimated_tokens IS NOT NULL
            THEN volatile_estimated_tokens ELSE 0 END), 0)
            as total_volatile_estimated_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY model_id, segmentation_status
    """
    model_rows = await db.fetch_all(model_status_sql, (start_dt, end_dt))
    per_model_status: dict[str, dict[str, Any]] = {}
    for row in model_rows:
        d = dict(row)
        mid = d["model_id"]
        bucket = per_model_status.setdefault(
            mid,
            {
                "segmented": 0,
                "empty_request": 0,
                "parse_failure": 0,
                "total_requests": 0,
                "stable_prefix_estimated_tokens": 0,
                "volatile_estimated_tokens": 0,
            },
        )
        status = d["segmentation_status"]
        if status not in ("segmented", "empty_request", "parse_failure"):
            status = "parse_failure"
        bucket[status] += int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["stable_prefix_estimated_tokens"] += int(
            d["total_stable_prefix_estimated_tokens"] or 0
        )
        bucket["volatile_estimated_tokens"] += int(
            d["total_volatile_estimated_tokens"] or 0
        )

    # --- global totals ---
    totals_sql = """
    SELECT
        COALESCE(SUM(CASE WHEN stable_prefix_estimated_tokens IS NOT NULL
            THEN stable_prefix_estimated_tokens ELSE 0 END), 0)
            as total_stable_prefix_estimated_tokens,
        COALESCE(SUM(CASE WHEN semi_stable_estimated_tokens IS NOT NULL
            THEN semi_stable_estimated_tokens ELSE 0 END), 0)
            as total_semi_stable_estimated_tokens,
        COALESCE(SUM(CASE WHEN volatile_estimated_tokens IS NOT NULL
            THEN volatile_estimated_tokens ELSE 0 END), 0)
            as total_volatile_estimated_tokens,
        COALESCE(SUM(stable_prefix_bytes), 0) as total_stable_prefix_bytes,
        COALESCE(SUM(semi_stable_bytes), 0) as total_semi_stable_bytes,
        COALESCE(SUM(volatile_bytes), 0) as total_volatile_bytes,
        COALESCE(SUM(CASE WHEN volatile_bytes > 0 THEN 1 ELSE 0 END), 0)
            as compressible_candidate_requests,
        COALESCE(SUM(CASE WHEN stable_prefix_bytes > 0 THEN 1 ELSE 0 END), 0)
            as protected_requests
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    totals_row = await db.fetch_one(totals_sql, (start_dt, end_dt))
    totals = dict(totals_row) if totals_row else {}

    stable_tokens = int(totals.get("total_stable_prefix_estimated_tokens", 0) or 0)
    semi_tokens = int(totals.get("total_semi_stable_estimated_tokens", 0) or 0)
    volatile_tokens = int(totals.get("total_volatile_estimated_tokens", 0) or 0)
    stable_bytes = int(totals.get("total_stable_prefix_bytes", 0) or 0)
    semi_bytes = int(totals.get("total_semi_stable_bytes", 0) or 0)
    volatile_bytes = int(totals.get("total_volatile_bytes", 0) or 0)

    total = sum(by_status.values())
    return {
        "total_requests": total,
        "by_status": by_status,
        "per_provider_status": per_provider_status,
        "per_model_status": per_model_status,
        "token_totals": {
            "stable_prefix": stable_tokens,
            "semi_stable": semi_tokens,
            "volatile": volatile_tokens,
            "all": stable_tokens + semi_tokens + volatile_tokens,
        },
        "byte_totals": {
            "stable_prefix": stable_bytes,
            "semi_stable": semi_bytes,
            "volatile": volatile_bytes,
            "all": stable_bytes + semi_bytes + volatile_bytes,
        },
        "compressible_candidate_requests": int(
            totals.get("compressible_candidate_requests", 0) or 0
        ),
        "protected_requests": int(totals.get("protected_requests", 0) or 0),
    }


async def fetch_compression_observability(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 4 observe-mode compression accounting aggregates.

    Reads only the compression columns populated by
    :func:`eggpool.transcoder.compression.analyze_compression` and
    persisted by
    :meth:`RequestRepository.finalize_if_pending`.  The analyzer is
    observational: it never mutates the payload and never changes
    routing.  This query surfaces:

    - ``total_requests``                     : finalized rows in window.
    - ``by_status``                          : ``{"disabled",
      "observed", "safe", "balanced"} -> request_count``.  Phase 4
      only ever emits ``"disabled"`` or ``"observed"``.
    - ``by_mode``                            : ``{"observe", ...} ->
      request_count``.  Always ``"observe"`` in Phase 4.
    - ``per_provider_status``                : ``(provider_id,
      upstream_protocol) -> {"disabled", "observed", ...}``.
    - ``per_account_status``                 : ``account_id -> {per-
      status, total_requests, candidate_count, eligible_count,
      estimated_savings_tokens}``.
    - ``per_model_status``                   : ``model_id -> {per-
      status, total_requests, candidate_count, eligible_count,
      estimated_savings_tokens}``.
    - ``totals``                             : aggregate
      ``candidate_count``, ``eligible_count``,
      ``suppressed_count``, ``estimated_original_tokens``,
      ``estimated_compressed_tokens``,
      ``estimated_savings_tokens``,
      ``analyzer_latency_ms`` (sum + median + p95),
      ``warning_count``, plus ``observed_requests``.
    - ``top_reason_codes``                   : top-N reason codes
      aggregated across the window, returned as ``[(code,
      count), ...]`` so the dashboard can render them without
      re-parsing the JSON.
    - ``by_policy``                         : Phase 6 resolved-policy
      rollup.  ``policy_name -> {source, total_requests, by_status,
      candidate_count, eligible_count, applied_count,
      transform_count, warning_count}``.  ``"<global>"`` is the
      sentinel for requests that did not match any
      ``[[compression.policies]]`` entry; operator-chosen names are
      the value of the entry's ``name`` field.
    - ``by_policy_source``                  : ``{"global",
      "policy:<name>", ...} -> request_count``.  Stable audit
      strings suitable for time-series grouping.
    - ``policy_warning_count_total``       : sum of
      ``compression_warning_count`` across all resolved-policy
      rows in window.  Does not include Phase 5 compression
      applier warnings (those are persisted separately in
      ``compression_warnings_json``).

    All numeric fields default to 0; ratios default to ``None``
    when the denominator is zero.  This query is reading-only: it
    never mutates the database and never depends on request
    lifecycle state.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # --- per-status breakdown ---
    status_sql = """
    SELECT
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY compression_status
    """
    rows = await db.fetch_all(status_sql, (start_dt, end_dt))
    by_status: dict[str, int] = {
        "disabled": 0,
        "observed": 0,
    }
    for row in rows:
        d = dict(row)
        status = d["compression_status"]
        if status not in by_status:
            # forward-compat: future phases may add 'safe' / 'balanced'
            by_status[status] = 0
        by_status[status] = int(d["request_count"])

    # --- by-mode breakdown ---
    mode_sql = """
    SELECT
        COALESCE(compression_mode, 'observe') as compression_mode,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_status = 'observed'
    GROUP BY compression_mode
    """
    mode_rows = await db.fetch_all(mode_sql, (start_dt, end_dt))
    by_mode: dict[str, int] = {}
    for row in mode_rows:
        d = dict(row)
        mode = d["compression_mode"] or "observe"
        by_mode[mode] = int(d["request_count"])

    # --- per (provider, upstream_protocol) breakdown ---
    protocol_status_sql = """
    SELECT
        COALESCE(provider_id, 'unknown') as provider_id,
        COALESCE(upstream_protocol, 'unknown') as upstream_protocol,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY provider_id, upstream_protocol, compression_status
    """
    protocol_rows = await db.fetch_all(protocol_status_sql, (start_dt, end_dt))
    per_provider_status: dict[tuple[str, str], dict[str, int]] = {}
    for row in protocol_rows:
        d = dict(row)
        key = (d["provider_id"], d["upstream_protocol"])
        bucket = per_provider_status.setdefault(key, {})
        status = d["compression_status"]
        bucket[status] = int(d["request_count"])

    # --- per-account breakdown ---
    account_status_sql = """
    SELECT
        COALESCE(account_id, 'unknown') as account_id,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN compression_candidate_count IS NOT NULL
            THEN compression_candidate_count ELSE 0 END), 0)
            as total_candidate_count,
        COALESCE(SUM(CASE WHEN compression_eligible_candidate_count IS NOT NULL
            THEN compression_eligible_candidate_count ELSE 0 END), 0)
            as total_eligible_count,
        COALESCE(SUM(CASE WHEN compression_estimated_savings_tokens IS NOT NULL
            THEN compression_estimated_savings_tokens ELSE 0 END), 0)
            as total_estimated_savings_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY account_id, compression_status
    """
    account_rows = await db.fetch_all(account_status_sql, (start_dt, end_dt))
    per_account_status: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        d = dict(row)
        aid = d["account_id"]
        bucket = per_account_status.setdefault(
            aid,
            {
                "disabled": 0,
                "observed": 0,
                "total_requests": 0,
                "candidate_count": 0,
                "eligible_count": 0,
                "estimated_savings_tokens": 0,
            },
        )
        status = d["compression_status"]
        bucket[status] = int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["candidate_count"] += int(d["total_candidate_count"] or 0)
        bucket["eligible_count"] += int(d["total_eligible_count"] or 0)
        bucket["estimated_savings_tokens"] += int(
            d["total_estimated_savings_tokens"] or 0
        )

    # --- per-model breakdown ---
    model_status_sql = """
    SELECT
        COALESCE(model_id, 'unknown') as model_id,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN compression_candidate_count IS NOT NULL
            THEN compression_candidate_count ELSE 0 END), 0)
            as total_candidate_count,
        COALESCE(SUM(CASE WHEN compression_eligible_candidate_count IS NOT NULL
            THEN compression_eligible_candidate_count ELSE 0 END), 0)
            as total_eligible_count,
        COALESCE(SUM(CASE WHEN compression_estimated_savings_tokens IS NOT NULL
            THEN compression_estimated_savings_tokens ELSE 0 END), 0)
            as total_estimated_savings_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY model_id, compression_status
    """
    model_rows = await db.fetch_all(model_status_sql, (start_dt, end_dt))
    per_model_status: dict[str, dict[str, Any]] = {}
    for row in model_rows:
        d = dict(row)
        mid = d["model_id"]
        bucket = per_model_status.setdefault(
            mid,
            {
                "disabled": 0,
                "observed": 0,
                "total_requests": 0,
                "candidate_count": 0,
                "eligible_count": 0,
                "estimated_savings_tokens": 0,
            },
        )
        status = d["compression_status"]
        bucket[status] = int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["candidate_count"] += int(d["total_candidate_count"] or 0)
        bucket["eligible_count"] += int(d["total_eligible_count"] or 0)
        bucket["estimated_savings_tokens"] += int(
            d["total_estimated_savings_tokens"] or 0
        )

    # --- global totals ---
    totals_sql = """
    SELECT
        COALESCE(SUM(compression_candidate_count), 0) as total_candidate_count,
        COALESCE(SUM(compression_eligible_candidate_count), 0)
            as total_eligible_count,
        COALESCE(SUM(compression_suppressed_candidate_count), 0)
            as total_suppressed_count,
        COALESCE(SUM(CASE WHEN compression_estimated_original_tokens IS NOT NULL
            THEN compression_estimated_original_tokens ELSE 0 END), 0)
            as total_estimated_original_tokens,
        COALESCE(SUM(CASE WHEN compression_estimated_compressed_tokens IS NOT NULL
            THEN compression_estimated_compressed_tokens ELSE 0 END), 0)
            as total_estimated_compressed_tokens,
        COALESCE(SUM(CASE WHEN compression_estimated_savings_tokens IS NOT NULL
            THEN compression_estimated_savings_tokens ELSE 0 END), 0)
            as total_estimated_savings_tokens,
        COALESCE(SUM(compression_analyzer_latency_ms), 0)
            as total_analyzer_latency_ms,
        COALESCE(SUM(compression_warning_count), 0) as total_warning_count,
        COALESCE(SUM(CASE WHEN compression_status = 'observed' THEN 1 ELSE 0 END), 0)
            as observed_requests
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    totals_row = await db.fetch_one(totals_sql, (start_dt, end_dt))
    totals = dict(totals_row) if totals_row else {}

    total = sum(by_status.values())
    observed_requests = int(totals.get("observed_requests", 0) or 0)

    # --- latency distribution (median / p95) over observed rows ---
    latency_sql = """
    SELECT compression_analyzer_latency_ms
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_status = 'observed'
        AND compression_analyzer_latency_ms IS NOT NULL
    ORDER BY compression_analyzer_latency_ms
    """
    latency_rows = await db.fetch_all(latency_sql, (start_dt, end_dt))
    latencies = [float(r["compression_analyzer_latency_ms"]) for r in latency_rows]
    median_latency: float | None = None
    p95_latency: float | None = None
    if latencies:
        median_index = len(latencies) // 2
        median_latency = latencies[median_index]
        p95_index = max(0, int(round(0.95 * (len(latencies) - 1))))
        p95_latency = latencies[p95_index]

    # --- top reason codes (top 10) ---
    reason_rows = await db.fetch_all(
        """
        SELECT compression_reason_code_counts_json
        FROM requests
        WHERE started_at >= ? AND started_at < ?
            AND status != 'pending'
            AND compression_status = 'observed'
            AND compression_reason_code_counts_json IS NOT NULL
        """,
        (start_dt, end_dt),
    )
    reason_totals: dict[str, int] = {}
    import json as _json

    for row in reason_rows:
        raw = row["compression_reason_code_counts_json"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        parsed_dict: dict[str, int] = {}
        for raw_key, raw_value in parsed.items():  # type: ignore[union-attr]
            if not isinstance(raw_key, str):
                continue
            try:
                count_int = int(raw_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            parsed_dict[raw_key] = count_int
        for code_obj, count_int in parsed_dict.items():
            reason_totals[code_obj] = reason_totals.get(code_obj, 0) + count_int
    top_reason_codes = sorted(
        reason_totals.items(), key=lambda item: item[1], reverse=True
    )[:10]

    # ===================================================================
    # Phase 5: safe-mode applied breakdown
    # ===================================================================

    # --- applied-mode aggregate totals ---
    applied_totals_sql = """
    SELECT
        COUNT(*) as applied_count,
        COALESCE(SUM(compression_transform_count), 0)
            as total_transform_count,
        COALESCE(SUM(CASE WHEN compression_savings_tokens IS NOT NULL
            THEN compression_savings_tokens ELSE 0 END), 0)
            as total_savings_tokens,
        COALESCE(SUM(CASE WHEN compression_stable_prefix_preserved = 1
            THEN 1 ELSE 0 END), 0)
            as stable_prefix_preserved_count,
        COALESCE(SUM(CASE WHEN compression_failed_fallback = 1
            THEN 1 ELSE 0 END), 0)
            as failed_fallback_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
    """
    applied_totals_row = await db.fetch_one(applied_totals_sql, (start_dt, end_dt))
    at = dict(applied_totals_row) if applied_totals_row else {}

    requests_with_compression_applied = int(at.get("applied_count", 0) or 0)
    applied_transform_count_total = int(at.get("total_transform_count", 0) or 0)
    applied_total_savings_tokens = int(at.get("total_savings_tokens", 0) or 0)
    applied_stable_prefix_preserved_count = int(
        at.get("stable_prefix_preserved_count", 0) or 0
    )
    applied_failed_fallback_count = int(at.get("failed_fallback_count", 0) or 0)

    # --- savings latency distribution (median / p95) over applied rows ---
    applied_savings_sql = """
    SELECT compression_savings_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
        AND compression_savings_tokens IS NOT NULL
    ORDER BY compression_savings_tokens
    """
    applied_savings_rows = await db.fetch_all(applied_savings_sql, (start_dt, end_dt))
    applied_savings = [
        float(r["compression_savings_tokens"]) for r in applied_savings_rows
    ]
    applied_median_savings: float | None = None
    applied_p95_savings: float | None = None
    if applied_savings:
        idx_m = len(applied_savings) // 2
        applied_median_savings = applied_savings[idx_m]
        idx_p95 = max(0, int(round(0.95 * (len(applied_savings) - 1))))
        applied_p95_savings = applied_savings[idx_p95]

    # --- applied latency distribution (median / p95) ---
    applied_latency_sql = """
    SELECT compression_latency_ms
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
        AND compression_latency_ms IS NOT NULL
    ORDER BY compression_latency_ms
    """
    applied_latency_rows = await db.fetch_all(applied_latency_sql, (start_dt, end_dt))
    applied_latencies = [
        float(r["compression_latency_ms"]) for r in applied_latency_rows
    ]
    applied_median_latency: float | None = None
    applied_p95_latency: float | None = None
    if applied_latencies:
        idx_m = len(applied_latencies) // 2
        applied_median_latency = applied_latencies[idx_m]
        idx_p95 = max(0, int(round(0.95 * (len(applied_latencies) - 1))))
        applied_p95_latency = applied_latencies[idx_p95]

    # --- top applied reason codes (top 10) from transforms_by_reason_json ---
    applied_reason_rows = await db.fetch_all(
        """
        SELECT compression_transforms_by_reason_json
        FROM requests
        WHERE started_at >= ? AND started_at < ?
            AND status != 'pending'
            AND compression_applied = 1
            AND compression_transforms_by_reason_json IS NOT NULL
        """,
        (start_dt, end_dt),
    )
    applied_reason_totals: dict[str, int] = {}
    for row in applied_reason_rows:
        raw = row["compression_transforms_by_reason_json"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for raw_key, raw_value in parsed.items():  # type: ignore[union-attr]
            if not isinstance(raw_key, str):
                continue
            try:
                count_int = int(raw_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            applied_reason_totals[raw_key] = (
                applied_reason_totals.get(raw_key, 0) + count_int
            )
    top_applied_reason_codes = sorted(
        applied_reason_totals.items(), key=lambda item: item[1], reverse=True
    )[:10]

    # --- applied per-mode breakdown ---
    applied_mode_sql = """
    SELECT
        COALESCE(compression_mode, 'observe') as compression_mode,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
    GROUP BY compression_mode
    """
    applied_mode_rows = await db.fetch_all(applied_mode_sql, (start_dt, end_dt))
    applied_per_mode: dict[str, int] = {}
    for row in applied_mode_rows:
        d = dict(row)
        mode = d["compression_mode"] or "observe"
        applied_per_mode[mode] = int(d["request_count"])

    # --- applied per (provider, upstream_protocol) breakdown ---
    applied_protocol_sql = """
    SELECT
        COALESCE(provider_id, 'unknown') as provider_id,
        COALESCE(upstream_protocol, 'unknown') as upstream_protocol,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
    GROUP BY provider_id, upstream_protocol, compression_status
    """
    applied_protocol_rows = await db.fetch_all(applied_protocol_sql, (start_dt, end_dt))
    applied_per_provider_status: dict[tuple[str, str], dict[str, int]] = {}
    for row in applied_protocol_rows:
        d = dict(row)
        key = (d["provider_id"], d["upstream_protocol"])
        bucket = applied_per_provider_status.setdefault(key, {})
        status = d["compression_status"]
        bucket[status] = int(d["request_count"])

    # --- applied per-model breakdown ---
    applied_model_sql = """
    SELECT
        COALESCE(model_id, 'unknown') as model_id,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN compression_transform_count IS NOT NULL
            THEN compression_transform_count ELSE 0 END), 0)
            as total_transform_count,
        COALESCE(SUM(CASE WHEN compression_savings_tokens IS NOT NULL
            THEN compression_savings_tokens ELSE 0 END), 0)
            as total_savings_tokens
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
    GROUP BY model_id, compression_status
    """
    applied_model_rows = await db.fetch_all(applied_model_sql, (start_dt, end_dt))
    applied_per_model_status: dict[str, dict[str, Any]] = {}
    for row in applied_model_rows:
        d = dict(row)
        mid = d["model_id"]
        bucket = applied_per_model_status.setdefault(
            mid,
            {
                "disabled": 0,
                "observed": 0,
                "total_requests": 0,
                "transform_count": 0,
                "savings_tokens": 0,
            },
        )
        status = d["compression_status"]
        bucket[status] = int(d["request_count"])
        bucket["total_requests"] += int(d["request_count"])
        bucket["transform_count"] += int(d["total_transform_count"] or 0)
        bucket["savings_tokens"] += int(d["total_savings_tokens"] or 0)

    # --- Phase 6: per-policy rollup ---
    #
    # Each resolved-policy audit row carries
    # ``compression_policy_name`` and ``compression_policy_source``
    # (added by migration 0044).  We aggregate per resolved name and
    # per source so dashboards can answer "how many requests resolved
    # under policy X?" without scanning the JSON summary column.  The
    # candidate counts and applied counts reuse the Phase 4 / Phase 5
    # columns; the breakdown stays forward-compatible because the
    # query aliases the ``NULL`` name to ``"<global>"`` (the resolver
    # sentinel) and adds new source values to the dict on first sight.
    policy_status_sql = """
    SELECT
        COALESCE(compression_policy_name, '<global>') as policy_name,
        COALESCE(compression_policy_source, 'global') as policy_source,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN compression_candidate_count IS NOT NULL
            THEN compression_candidate_count ELSE 0 END), 0)
            as total_candidate_count,
        COALESCE(SUM(CASE WHEN compression_eligible_candidate_count IS NOT NULL
            THEN compression_eligible_candidate_count ELSE 0 END), 0)
            as total_eligible_count,
        COALESCE(SUM(CASE WHEN compression_warning_count IS NOT NULL
            THEN compression_warning_count ELSE 0 END), 0)
            as total_warning_count,
        COALESCE(SUM(CASE WHEN compression_applied IS NOT NULL
            THEN compression_applied ELSE 0 END), 0)
            as total_applied_count,
        COALESCE(SUM(CASE WHEN compression_transform_count IS NOT NULL
            THEN compression_transform_count ELSE 0 END), 0)
            as total_transform_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY compression_policy_name, compression_policy_source, compression_status
    """
    policy_rows = await db.fetch_all(policy_status_sql, (start_dt, end_dt))
    by_policy: dict[str, dict[str, Any]] = {}
    by_policy_source: dict[str, int] = {}
    policy_warning_total = 0
    for row in policy_rows:
        d = dict(row)
        name = d["policy_name"]
        source = d["policy_source"]
        status = d["compression_status"]
        bucket = by_policy.setdefault(
            name,
            {
                "source": source,
                "total_requests": 0,
                "by_status": {
                    "disabled": 0,
                    "observed": 0,
                },
                "candidate_count": 0,
                "eligible_count": 0,
                "applied_count": 0,
                "transform_count": 0,
                "warning_count": 0,
            },
        )
        bucket["total_requests"] += int(d["request_count"])
        bucket_status = bucket["by_status"]
        if status not in bucket_status:
            bucket_status[status] = 0
        bucket_status[status] += int(d["request_count"])
        bucket["candidate_count"] += int(d["total_candidate_count"] or 0)
        bucket["eligible_count"] += int(d["total_eligible_count"] or 0)
        bucket["applied_count"] += int(d["total_applied_count"] or 0)
        bucket["transform_count"] += int(d["total_transform_count"] or 0)
        bucket["warning_count"] += int(d["total_warning_count"] or 0)
        by_policy_source[source] = by_policy_source.get(source, 0) + int(
            d["request_count"]
        )
        policy_warning_total += int(d["total_warning_count"] or 0)

    return {
        "total_requests": total,
        "by_status": by_status,
        "by_mode": by_mode,
        "per_provider_status": per_provider_status,
        "per_account_status": per_account_status,
        "per_model_status": per_model_status,
        "totals": {
            "candidate_count": int(totals.get("total_candidate_count", 0) or 0),
            "eligible_count": int(totals.get("total_eligible_count", 0) or 0),
            "suppressed_count": int(totals.get("total_suppressed_count", 0) or 0),
            "estimated_original_tokens": int(
                totals.get("total_estimated_original_tokens", 0) or 0
            ),
            "estimated_compressed_tokens": int(
                totals.get("total_estimated_compressed_tokens", 0) or 0
            ),
            "estimated_savings_tokens": int(
                totals.get("total_estimated_savings_tokens", 0) or 0
            ),
            "analyzer_latency_ms_total": float(
                totals.get("total_analyzer_latency_ms", 0) or 0
            ),
            "analyzer_latency_ms_median": median_latency,
            "analyzer_latency_ms_p95": p95_latency,
            "warning_count": int(totals.get("total_warning_count", 0) or 0),
            "observed_requests": observed_requests,
        },
        "top_reason_codes": top_reason_codes,
        # Phase 5: safe-mode applied breakdown
        "requests_with_compression_applied": requests_with_compression_applied,
        "applied_transform_count_total": applied_transform_count_total,
        "applied_total_savings_tokens": applied_total_savings_tokens,
        "applied_median_savings_tokens": applied_median_savings,
        "applied_p95_savings_tokens": applied_p95_savings,
        "applied_median_latency_ms": applied_median_latency,
        "applied_p95_latency_ms": applied_p95_latency,
        "applied_stable_prefix_preserved_count": applied_stable_prefix_preserved_count,
        "applied_failed_fallback_count": applied_failed_fallback_count,
        "top_applied_reason_codes": top_applied_reason_codes,
        "applied_per_mode": applied_per_mode,
        "applied_per_provider_status": applied_per_provider_status,
        "applied_per_model_status": applied_per_model_status,
        # Phase 6: resolved-policy rollup (migration 0044).  These
        # three keys are advisory / audit; they do not influence
        # routing, scoring, or compression decisioning.  Names use
        # the resolver's ``<global>`` sentinel for the no-override
        # path so dashboards can render the rollup without
        # special-casing ``NULL``.
        "by_policy": by_policy,
        "by_policy_source": by_policy_source,
        "policy_warning_count_total": policy_warning_total,
    }


async def fetch_compression_runtime(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 7 runtime compression aggregates for operator dashboards.

    Phase 7 makes the existing Phase 4 / Phase 5 / Phase 6 data
    operationally usable.  This query surfaces the answers an
    operator actually needs:

    - Is compression running in observe or safe mode?
    - How many requests actually compressed?
    - How often did fail-closed fallbacks occur?
    - Which transforms are doing useful work?
    - Is compression latency within SBC-safe budgets?
    - Are stable-prefix hashes preserved across compression?

    The response is intentionally narrow: a ``window`` block
    (``request_count``), a ``mode_counts`` block (count per
    ``compression_mode``), and a small set of headline metrics.
    Raw per-request data stays in the ``requests`` table; the
    dashboard never sees payloads.

    Returns::

        {
          "window": {"seconds": <int>, "request_count": <int>},
          "mode_counts": {"disabled": int, "observe": int, "safe": int},
          "applied_count": <int>,
          "failed_fallback_count": <int>,
          "candidate_count": <int>,
          "estimated_savings_tokens": <int>,
          "actual_savings_tokens": <int>,
          "latency_ms": {"avg": float|None, "p50": float|None,
                         "p95": float|None, "max": float|None},
          "transforms": {
            "<reason_code>": {"applied": int, "tokens_saved": int},
            ...
          },
          "warnings": {"stable_prefix_hash_mismatch": <int>, ...},
          "cache_safety": {
            "stable_prefix_preserved": <int>,
            "stable_prefix_mismatch": <int>,
          },
        }
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # --- window size in seconds (best effort) ---
    try:
        start_obj = datetime.fromisoformat(start_dt)
        end_obj = datetime.fromisoformat(end_dt)
        window_seconds = max(0, int((end_obj - start_obj).total_seconds()))
    except ValueError:
        window_seconds = 0

    # --- window request count and per-mode counts ---
    window_sql = """
    SELECT COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    window_row = await db.fetch_one(window_sql, (start_dt, end_dt))
    if window_row:
        request_count = int(dict(window_row).get("request_count", 0) or 0)
    else:
        request_count = 0

    mode_sql = """
    SELECT
        COALESCE(compression_mode, 'observe') as compression_mode,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY compression_mode, compression_status
    """
    mode_rows = await db.fetch_all(mode_sql, (start_dt, end_dt))
    mode_counts: dict[str, int] = {
        "disabled": 0,
        "observe": 0,
        "safe": 0,
    }
    for row in mode_rows:
        d = dict(row)
        mode = d["compression_mode"] or "observe"
        status = d["compression_status"] or "disabled"
        # Prefer status for disabled bucket (mode may be NULL when disabled).
        bucket = status if status == "disabled" else mode
        mode_counts[bucket] = mode_counts.get(bucket, 0) + int(d["request_count"])

    # --- applied count + savings totals ---
    applied_sql = """
    SELECT
        COUNT(*) as applied_count,
        COALESCE(SUM(CASE WHEN compression_savings_tokens IS NOT NULL
            THEN compression_savings_tokens ELSE 0 END), 0)
            as actual_savings_tokens,
        COALESCE(SUM(CASE WHEN compression_candidate_count IS NOT NULL
            THEN compression_candidate_count ELSE 0 END), 0)
            as total_candidate_count,
        COALESCE(SUM(CASE WHEN compression_estimated_savings_tokens IS NOT NULL
            THEN compression_estimated_savings_tokens ELSE 0 END), 0)
            as total_estimated_savings_tokens,
        COALESCE(SUM(CASE WHEN compression_failed_fallback = 1 THEN 1 ELSE 0 END), 0)
            as failed_fallback_count,
        COALESCE(SUM(CASE WHEN compression_stable_prefix_preserved = 1
            THEN 1 ELSE 0 END), 0)
            as stable_prefix_preserved_count,
        COALESCE(SUM(CASE WHEN compression_stable_prefix_preserved = 0
            THEN 1 ELSE 0 END), 0)
            as stable_prefix_mismatch_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    applied_row = await db.fetch_one(applied_sql, (start_dt, end_dt))
    ad = dict(applied_row) if applied_row else {}
    applied_count = int(ad.get("applied_count", 0) or 0)
    actual_savings_tokens = int(ad.get("actual_savings_tokens", 0) or 0)
    candidate_count = int(ad.get("total_candidate_count", 0) or 0)
    estimated_savings_tokens = int(ad.get("total_estimated_savings_tokens", 0) or 0)
    failed_fallback_count = int(ad.get("failed_fallback_count", 0) or 0)
    stable_prefix_preserved_count = int(ad.get("stable_prefix_preserved_count", 0) or 0)
    stable_prefix_mismatch_count = int(ad.get("stable_prefix_mismatch_count", 0) or 0)

    # --- latency stats (avg / p50 / p95 / max) ---
    latency_sql = """
    SELECT compression_latency_ms
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_latency_ms IS NOT NULL
        AND compression_latency_ms > 0
    ORDER BY compression_latency_ms
    """
    latency_rows = await db.fetch_all(latency_sql, (start_dt, end_dt))
    latencies = [float(r["compression_latency_ms"]) for r in latency_rows]
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        idx_m = len(latencies) // 2
        median_latency = latencies[idx_m]
        idx_p95 = max(0, int(round(0.95 * (len(latencies) - 1))))
        p95_latency = latencies[idx_p95]
        max_latency = latencies[-1]
    else:
        avg_latency = None
        median_latency = None
        p95_latency = None
        max_latency = None

    # --- per-transform aggregates (applied only) ---
    transform_sql = """
    SELECT compression_transforms_by_reason_json
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
        AND compression_transforms_by_reason_json IS NOT NULL
    """
    transform_rows = await db.fetch_all(transform_sql, (start_dt, end_dt))
    import json as _json

    transforms: dict[str, dict[str, int]] = {}
    for row in transform_rows:
        raw = row["compression_transforms_by_reason_json"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for code_obj, count_value in parsed.items():  # type: ignore[union-attr]
            if not isinstance(code_obj, str):
                continue
            try:
                count_int = int(count_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            bucket = transforms.setdefault(code_obj, {"applied": 0, "tokens_saved": 0})
            bucket["applied"] += count_int

    # --- per-transform savings (link via compression_savings_tokens) ---
    savings_per_transform_sql = """
    SELECT compression_savings_tokens, compression_transforms_by_reason_json
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_applied = 1
        AND compression_transforms_by_reason_json IS NOT NULL
        AND compression_savings_tokens IS NOT NULL
        AND compression_savings_tokens > 0
    """
    savings_rows = await db.fetch_all(savings_per_transform_sql, (start_dt, end_dt))
    for row in savings_rows:
        raw = row["compression_transforms_by_reason_json"]
        savings = float(row["compression_savings_tokens"] or 0)
        if not raw or savings <= 0:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        total_for_row = 0
        for v in parsed.values():  # type: ignore[union-attr]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total_for_row += int(v)
        if total_for_row <= 0:
            continue
        per_unit = savings / total_for_row
        for code_obj, count_value in parsed.items():  # type: ignore[union-attr]
            if not isinstance(code_obj, str):
                continue
            try:
                count_int = int(count_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if count_int <= 0:
                continue
            bucket = transforms.setdefault(code_obj, {"applied": 0, "tokens_saved": 0})
            bucket["tokens_saved"] += int(round(per_unit * count_int))

    # --- warnings rollup from compression_warnings_json ---
    warnings_sql = """
    SELECT compression_warnings_json
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND compression_warnings_json IS NOT NULL
    """
    warning_rows = await db.fetch_all(warnings_sql, (start_dt, end_dt))
    warnings: dict[str, int] = {}
    for row in warning_rows:
        raw = row["compression_warnings_json"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:  # type: ignore[union-attr]
            if not isinstance(entry, str):
                continue
            warnings[entry] = warnings.get(entry, 0) + 1

    return {
        "window": {
            "seconds": window_seconds,
            "request_count": request_count,
        },
        "mode_counts": mode_counts,
        "applied_count": applied_count,
        "failed_fallback_count": failed_fallback_count,
        "candidate_count": candidate_count,
        "estimated_savings_tokens": estimated_savings_tokens,
        "actual_savings_tokens": actual_savings_tokens,
        "latency_ms": {
            "avg": avg_latency,
            "p50": median_latency,
            "p95": p95_latency,
            "max": max_latency,
        },
        "transforms": transforms,
        "warnings": warnings,
        "cache_safety": {
            "stable_prefix_preserved": stable_prefix_preserved_count,
            "stable_prefix_mismatch": stable_prefix_mismatch_count,
        },
    }


async def fetch_compression_policy_stats(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 7 per-policy compression rollup for operator dashboards.

    Phase 6 added ``compression_policy_name`` and
    ``compression_policy_source`` columns (migration 0044).  This
    query projects them into a dashboard-friendly shape:

    - One ``policy_counts`` entry per resolved policy
      (``policy_name = "<global>"`` sentinel for the no-override path)
    - ``mode_counts`` per policy (observe vs safe vs disabled)
    - ``applied`` / ``failed_fallback`` per policy
    - ``warning_count`` per policy (advisory only; the QuotaFairScorer
      does not consume policy fields)

    Returns::

        {
          "policy_counts": [
            {
              "policy_name": "<global>",
              "policy_source": "global",
              "requests": <int>,
              "mode_counts": {"disabled": int, "observe": int, "safe": int},
              "applied": <int>,
              "failed_fallback": <int>,
              "candidate_count": <int>,
              "warning_count": <int>,
            },
            ...
          ],
          "total_requests": <int>,
          "total_policies": <int>,
        }
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    policy_sql = """
    SELECT
        COALESCE(compression_policy_name, '<global>') as policy_name,
        COALESCE(compression_policy_source, 'global') as policy_source,
        COALESCE(compression_mode, 'observe') as compression_mode,
        COALESCE(compression_status, 'disabled') as compression_status,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN compression_candidate_count IS NOT NULL
            THEN compression_candidate_count ELSE 0 END), 0)
            as total_candidate_count,
        COALESCE(SUM(CASE WHEN compression_applied IS NOT NULL
            THEN compression_applied ELSE 0 END), 0)
            as total_applied,
        COALESCE(SUM(CASE WHEN compression_failed_fallback IS NOT NULL
            THEN compression_failed_fallback ELSE 0 END), 0)
            as total_failed_fallback,
        COALESCE(SUM(CASE WHEN compression_warning_count IS NOT NULL
            THEN compression_warning_count ELSE 0 END), 0)
            as total_warning_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY compression_policy_name, compression_policy_source,
             compression_mode, compression_status
    ORDER BY policy_name, policy_source
    """
    policy_rows = await db.fetch_all(policy_sql, (start_dt, end_dt))

    policy_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    total_requests = 0
    for row in policy_rows:
        d = dict(row)
        name = d["policy_name"]
        source = d["policy_source"]
        mode = d["compression_mode"] or "observe"
        status = d["compression_status"] or "disabled"
        request_count = int(d["request_count"])
        total_requests += request_count
        key = (name, source)
        bucket = policy_buckets.setdefault(
            key,
            {
                "policy_name": name,
                "policy_source": source,
                "requests": 0,
                "mode_counts": {"disabled": 0, "observe": 0, "safe": 0},
                "applied": 0,
                "failed_fallback": 0,
                "candidate_count": 0,
                "warning_count": 0,
            },
        )
        bucket["requests"] += request_count
        bucket["candidate_count"] += int(d["total_candidate_count"] or 0)
        bucket["applied"] += int(d["total_applied"] or 0)
        bucket["failed_fallback"] += int(d["total_failed_fallback"] or 0)
        bucket["warning_count"] += int(d["total_warning_count"] or 0)
        # Status drives the disabled bucket; otherwise mode wins.
        target_key = status if status == "disabled" else mode
        bucket["mode_counts"][target_key] = (
            bucket["mode_counts"].get(target_key, 0) + request_count
        )

    # Stable ordering: global sentinel first, then alphabetical.
    policy_counts = sorted(
        policy_buckets.values(),
        key=lambda b: (
            0 if b["policy_name"] == "<global>" else 1,
            b["policy_name"],
        ),
    )

    return {
        "policy_counts": policy_counts,
        "total_requests": total_requests,
        "total_policies": len(policy_counts),
    }


async def fetch_cache_stability_summary(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 7 cache-stability observability summary.

    Phase 3 transcoder cache stability is in-memory on
    :class:`TranscodeContext.cache_boundary_tracker` and is not
    persisted to the ``requests`` table.  We surface what is
    observable from durable state only:

    - ``transcoded_request_count`` — number of finalized requests
      with ``transcoded = 1``
    - ``transcoded_with_warnings_count`` — transcoded requests where
      the transcoder emitted any loss warning (proxy-recorded via the
      normalized warning rollup; currently best-effort zero when no
      counter is set)

    The ``notes`` field is intentionally honest: cache-stability
    detail lives on the per-request transcoder trace, not in the
    aggregate.  This endpoint is a reporting-only signal that the
    Phase 3 boundary tracker is wired and operating.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    transcoded_sql = """
    SELECT COUNT(*) as transcoded_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
        AND transcoded = 1
    """
    row = await db.fetch_one(transcoded_sql, (start_dt, end_dt))
    transcoded_count = int(dict(row).get("transcoded_count", 0) or 0) if row else 0

    return {
        "transcoded_request_count": transcoded_count,
        "notes": (
            "Phase 3 cache-stability is per-request and in-memory on "
            "TranscodeContext.cache_boundary_tracker; durable summary "
            "counts are reported-only."
        ),
    }


async def fetch_synthetic_cache_summary(
    db: Database,
    start: str,
    end: str,
) -> dict[str, Any]:
    """Phase 9 synthetic cache controls observability aggregates.

    Reads the ``synthetic_cache_*`` columns populated by
    :func:`eggpool.transcoder.cache_synthesis.run_synthetic_cache_synthesis`
    and persisted by :meth:`RequestRepository.finalize_if_pending`.
    The selector is disabled by default and dry-run by default when
    enabled.  This query surfaces:

    - ``total_requests``                     : finalized rows in window.
    - ``status_counts``                      : ``{"disabled",
      "dry_run", "applied", "no_candidates",
      "policy_required", "provider_unsupported"} -> request_count``.
    - ``dry_run_count``                      : requests where
      ``synthetic_cache_dry_run = 1``.
    - ``applied_count``                      : requests where
      ``synthetic_cache_status = 'applied'``.
    - ``candidate_count_total``              : sum of
      ``synthetic_cache_candidate_count``.
    - ``applied_count_total``                : sum of
      ``synthetic_cache_applied_count``.
    - ``warning_count_total``                : sum of
      ``synthetic_cache_warning_count``.
    - ``warning_counts``                     : ``{warning_code: count}``
      aggregated from ``synthetic_cache_warnings_json``.
    - ``by_policy``                          : per resolved-policy
      rollup with ``policy_name``, ``policy_source``,
      ``request_count``, ``applied_count``, ``candidate_count``.
      ``<global>`` sentinel for requests without a policy override.
    - ``by_status_timeseries``               : ``None`` (bucketing
      not yet wired; callers can extend).

    All numeric fields default to 0.  This query is reading-only: it
    never mutates the database and never depends on request lifecycle
    state.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)

    # --- per-status breakdown ---
    status_sql = """
    SELECT
        COALESCE(synthetic_cache_status, 'disabled') as synthetic_cache_status,
        COUNT(*) as request_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY synthetic_cache_status
    """
    rows = await db.fetch_all(status_sql, (start_dt, end_dt))
    status_counts: dict[str, int] = {
        "disabled": 0,
        "dry_run": 0,
        "applied": 0,
        "no_candidates": 0,
        "policy_required": 0,
        "provider_unsupported": 0,
    }
    for row in rows:
        d = dict(row)
        status = d["synthetic_cache_status"]
        if status not in status_counts:
            status_counts[status] = 0
        status_counts[status] = int(d["request_count"])

    total_requests = sum(status_counts.values())

    # --- dry_run / applied / candidate / applied_count / warning totals ---
    totals_sql = """
    SELECT
        COALESCE(SUM(synthetic_cache_dry_run), 0) as dry_run_count,
        COALESCE(SUM(CASE WHEN synthetic_cache_status = 'applied'
            THEN 1 ELSE 0 END), 0) as applied_count,
        COALESCE(SUM(COALESCE(synthetic_cache_candidate_count, 0)), 0)
            as candidate_count_total,
        COALESCE(SUM(COALESCE(synthetic_cache_applied_count, 0)), 0)
            as applied_count_total,
        COALESCE(SUM(COALESCE(synthetic_cache_warning_count, 0)), 0)
            as warning_count_total
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    """
    totals_row = await db.fetch_one(totals_sql, (start_dt, end_dt))
    totals = dict(totals_row) if totals_row else {}

    # --- warning codes aggregation ---
    warning_rows = await db.fetch_all(
        """
        SELECT synthetic_cache_warnings_json
        FROM requests
        WHERE started_at >= ? AND started_at < ?
            AND status != 'pending'
            AND synthetic_cache_warnings_json IS NOT NULL
            AND synthetic_cache_warnings_json != '[]'
        """,
        (start_dt, end_dt),
    )
    warning_totals: dict[str, int] = {}
    import json as _json

    for row in warning_rows:
        raw = row["synthetic_cache_warnings_json"]
        if not raw:
            continue
        try:
            parsed = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for item in parsed:  # type: ignore[reportUnknownVariableType]
            if isinstance(item, str):
                warning_totals[item] = warning_totals.get(item, 0) + 1

    # --- by-policy rollup ---
    policy_sql = """
    SELECT
        COALESCE(synthetic_cache_policy_name, '<global>') as policy_name,
        COALESCE(synthetic_cache_policy_source, 'global') as policy_source,
        COUNT(*) as request_count,
        COALESCE(SUM(CASE WHEN synthetic_cache_status = 'applied'
            THEN 1 ELSE 0 END), 0) as applied_count,
        COALESCE(SUM(COALESCE(synthetic_cache_candidate_count, 0)), 0)
            as candidate_count
    FROM requests
    WHERE started_at >= ? AND started_at < ?
        AND status != 'pending'
    GROUP BY synthetic_cache_policy_name, synthetic_cache_policy_source
    ORDER BY request_count DESC
    """
    policy_rows = await db.fetch_all(policy_sql, (start_dt, end_dt))
    by_policy: list[dict[str, Any]] = []
    for row in policy_rows:
        d = dict(row)
        by_policy.append(
            {
                "policy_name": str(d["policy_name"]),
                "policy_source": str(d["policy_source"]),
                "request_count": int(d["request_count"]),
                "applied_count": int(d["applied_count"] or 0),
                "candidate_count": int(d["candidate_count"] or 0),
            }
        )

    return {
        "total_requests": total_requests,
        "status_counts": status_counts,
        "dry_run_count": int(totals.get("dry_run_count", 0) or 0),
        "applied_count": int(totals.get("applied_count", 0) or 0),
        "candidate_count_total": int(totals.get("candidate_count_total", 0) or 0),
        "applied_count_total": int(totals.get("applied_count_total", 0) or 0),
        "warning_count_total": int(totals.get("warning_count_total", 0) or 0),
        "warning_counts": warning_totals,
        "by_policy": by_policy,
        "by_status_timeseries": None,
    }


# ---------------------------------------------------------------------------
# Phase 10: closed-loop threshold tuning
# ---------------------------------------------------------------------------


async def fetch_compression_tuning_window_metrics(
    db: Database,
    start: str,
    end: str,
    *,
    window_requests: int = 500,
) -> dict[str, Any]:
    """Phase 10 per-policy window metrics for the tuning engine.

    Reads the Phase 4/5/6 ``compression_*`` columns populated by
    :mod:`eggpool.transcoder.compression.analyzer` /
    :mod:`eggpool.transcoder.compression.apply` finalizers and
    aggregates one :class:`TuningWindowMetrics` per resolved
    policy (``<global>`` sentinel for requests without a Phase 6
    override).

    The query is bounded by ``window_requests`` per policy: only the
    most recent N rows are scanned, so the dashboard stays fast even
    on heavy traffic.  All rates are computed in SQL; the
    ``TuningWindowMetrics`` values are flattened into the response so
    the dashboard can render without re-importing the tuning module.

    The output is keyed by ``policy_name`` (using ``<global>`` as
    the sentinel) and contains the exact field names consumed by
    :func:`eggpool.transcoder.compression.tuning.compute_recommendation`.

    No raw prompts, tool outputs, system messages, request bodies,
    or auth headers are ever read by this query.
    """
    start_dt = _format_dt(start)
    end_dt = _format_dt(end)
    bounded_window = max(int(window_requests), 1)

    # --- per-policy aggregate row (latest N requests) ---
    per_policy_sql = """
    WITH ranked AS (
        SELECT
            COALESCE(compression_policy_name, '<global>') AS policy_name,
            COALESCE(compression_status, 'disabled') AS compression_status,
            COALESCE(compression_applied, 0) AS compression_applied,
            COALESCE(compression_failed_fallback, 0) AS compression_failed_fallback,
            COALESCE(compression_savings_tokens, 0) AS compression_savings_tokens,
            COALESCE(compression_latency_ms, 0) AS compression_latency_ms,
            COALESCE(compression_warning_count, 0) AS compression_warning_count,
            compression_reason_code_counts_json,
            compression_warnings_json,
            started_at,
            ROW_NUMBER() OVER (
                PARTITION BY COALESCE(compression_policy_name, '<global>')
                ORDER BY started_at DESC
            ) AS rn
        FROM requests
        WHERE started_at >= ? AND started_at < ?
            AND status != 'pending'
    )
    SELECT
        policy_name,
        COUNT(*) AS total_requests,
        COALESCE(SUM(compression_applied), 0) AS applied_count,
        COALESCE(SUM(compression_failed_fallback), 0) AS failed_fallback_count,
        COALESCE(SUM(compression_warning_count), 0)
            AS latency_budget_warning_count_proxy,
        COALESCE(SUM(CASE WHEN compression_status IN (
            'disabled', 'skipped', 'observe_skipped'
        ) THEN 1 ELSE 0 END), 0) AS suppressed_count
    FROM ranked
    WHERE rn <= ?
    GROUP BY policy_name
    """
    rows = await db.fetch_all(
        per_policy_sql,
        (start_dt, end_dt, bounded_window),
    )

    # --- per-policy latency / savings sorted samples for percentiles ---
    samples_sql = """
    WITH ranked AS (
        SELECT
            COALESCE(compression_policy_name, '<global>') AS policy_name,
            COALESCE(compression_latency_ms, 0) AS compression_latency_ms,
            COALESCE(compression_savings_tokens, 0) AS compression_savings_tokens,
            COALESCE(compression_applied, 0) AS compression_applied,
            compression_warnings_json,
            compression_reason_code_counts_json,
            ROW_NUMBER() OVER (
                PARTITION BY COALESCE(compression_policy_name, '<global>')
                ORDER BY started_at DESC
            ) AS rn
        FROM requests
        WHERE started_at >= ? AND started_at < ?
            AND status != 'pending'
            AND compression_applied = 1
    )
    SELECT policy_name, compression_latency_ms, compression_savings_tokens,
           compression_warnings_json, compression_reason_code_counts_json
    FROM ranked
    WHERE rn <= ?
    ORDER BY policy_name, compression_latency_ms
    """
    sample_rows = await db.fetch_all(
        samples_sql,
        (start_dt, end_dt, bounded_window),
    )

    # Bucket samples per policy.
    samples_by_policy: dict[str, list[tuple[float, float]]] = {}
    warning_by_policy: dict[str, dict[str, int]] = {}
    reason_by_policy: dict[str, dict[str, int]] = {}
    for row in sample_rows:
        d = dict(row)
        policy = str(d["policy_name"])
        latency = float(d["compression_latency_ms"] or 0)
        savings = float(d["compression_savings_tokens"] or 0)
        samples_by_policy.setdefault(policy, []).append((latency, savings))
        import json as _json

        warnings_raw = d.get("compression_warnings_json")
        if warnings_raw:
            try:
                parsed = _json.loads(warnings_raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                bucket = warning_by_policy.setdefault(policy, {})
                for item in cast("list[str]", parsed):
                    bucket[item] = bucket.get(item, 0) + 1
        reason_raw = d.get("compression_reason_code_counts_json")
        if reason_raw:
            try:
                parsed = _json.loads(reason_raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                bucket = reason_by_policy.setdefault(policy, {})
                for code_key, count_value in cast("dict[str, Any]", parsed).items():
                    if isinstance(count_value, (int, float)):
                        bucket[code_key] = bucket.get(code_key, 0) + int(count_value)

    # --- assemble the per-policy dict ---
    out: dict[str, Any] = {}
    for row in rows:
        d = dict(row)
        policy = str(d["policy_name"])
        total = int(d["total_requests"] or 0)
        applied = int(d["applied_count"] or 0)
        samples = samples_by_policy.get(policy, [])
        latencies = sorted(s for s, _ in samples)
        savings_list = sorted((s for _, s in samples), key=lambda x: x)

        def _percentile(values: list[float], pct: float) -> float:
            if not values:
                return 0.0
            idx = int(round((len(values) - 1) * pct))
            return float(values[idx])

        # Count warning kinds for below-threshold suppression; these
        # come from per-request reason_code_counts which is the
        # canonical record of why a request was suppressed.
        reason_counts = reason_by_policy.get(policy, {})
        below_min_candidate = int(reason_counts.get("below_min_candidate_tokens", 0))
        below_min_savings = int(reason_counts.get("below_min_savings_tokens", 0))
        # positive_savings_count is "applied requests with savings > 0".
        positive_savings = sum(1 for _, s in samples if s > 0)
        # Latency-budget warnings: surfaced via compression_warning_count
        # when compression_warnings_json contains a "latency_budget"
        # entry.  Approximation: count rows whose warnings include
        # latency_budget_exceeded.
        latency_budget_warning_count = sum(
            int(v)
            for code, v in warning_by_policy.get(policy, {}).items()
            if "latency_budget" in code
        )

        out[policy] = {
            "total_requests": total,
            "applied_count": applied,
            "suppressed_count": int(d["suppressed_count"] or 0),
            "below_min_candidate_count": below_min_candidate,
            "below_min_savings_count": below_min_savings,
            "latency_budget_warning_count": latency_budget_warning_count,
            "failed_fallback_count": int(d["failed_fallback_count"] or 0),
            "positive_savings_count": positive_savings,
            "p95_latency_ms": _percentile(latencies, 0.95),
            "median_latency_ms": _percentile(latencies, 0.50),
            "median_savings_tokens": _percentile(savings_list, 0.50),
            "p95_savings_tokens": _percentile(savings_list, 0.95),
            "warning_counts": dict(warning_by_policy.get(policy, {})),
            "reason_counts": dict(reason_counts),
        }
    return out


async def fetch_compression_tuning_recommendations(
    db: Database,
) -> list[dict[str, Any]]:
    """Return the persisted Phase 10 recommendations.

    Reads the ``compression_tuning_recommendations`` table populated
    by the recommendation registry.  No raw prompts or content are
    ever stored.  Returns one row per policy (``<global>`` sentinel
    for the no-override path).
    """
    rows = await db.fetch_all(
        """
        SELECT policy_name, status, recommendation_json, generated_at
        FROM compression_tuning_recommendations
        ORDER BY generated_at DESC
        """,
    )
    import json as _json

    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        raw = d.get("recommendation_json")
        parsed: Any = None
        if raw:
            try:
                parsed = _json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
        out.append(
            {
                "policy_name": str(d["policy_name"]),
                "status": str(d["status"]),
                "recommendation": parsed,
                "generated_at": str(d["generated_at"]),
            },
        )
    return out


async def upsert_compression_tuning_recommendation(
    db: Database,
    *,
    policy_name: str,
    status: str,
    recommendation_json: str,
    generated_at: str,
) -> None:
    """Insert or update the recommendation row for one policy.

    The caller (the registry) is responsible for serialising the
    :class:`CompressionTuningRecommendation` payload to JSON; this
    function never inspects the body, so it cannot accidentally
    persist raw prompt content.  Writes use an owned transaction so
    the audit row is atomic with respect to other registry writes.
    """
    async with db.transaction():
        await db._execute_cursor(  # pyright: ignore[reportPrivateUsage]
            """
            INSERT INTO compression_tuning_recommendations
                (policy_name, status, recommendation_json, generated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(policy_name) DO UPDATE SET
                status = excluded.status,
                recommendation_json = excluded.recommendation_json,
                generated_at = excluded.generated_at
            """,
            (policy_name, status, recommendation_json, generated_at),
        )


async def fetch_compression_tuning_overrides(
    db: Database,
) -> list[dict[str, Any]]:
    """Return currently-active runtime overrides.

    Reads ``compression_tuning_overrides`` for the audit trail.  Rows
    whose ``expires_at`` is in the past are returned with
    ``is_expired`` set so dashboards can show the historic audit
    without losing context.
    """
    rows = await db.fetch_all(
        """
        SELECT id, policy_name, fields_json, reason_codes_json,
               generated_at, expires_at
        FROM compression_tuning_overrides
        ORDER BY generated_at DESC
        LIMIT 50
        """,
    )
    import json as _json
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        fields = None
        reasons = None
        try:
            fields = _json.loads(d["fields_json"])
        except (TypeError, ValueError):
            fields = None
        try:
            reasons = _json.loads(d["reason_codes_json"])
        except (TypeError, ValueError):
            reasons = None
        expires_raw = d.get("expires_at")
        expires_at: datetime | None = None
        is_expired = False
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw))
                is_expired = expires_at <= now
            except ValueError:
                expires_at = None
        out.append(
            {
                "id": int(d["id"]),
                "policy_name": str(d["policy_name"]),
                "fields": fields,
                "reason_codes": reasons,
                "generated_at": str(d["generated_at"]),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "is_expired": is_expired,
            },
        )
    return out
