"""Diagnostic EXPLAIN QUERY PLAN for dashboard queries.

Extractable helper that returns query plans and timings for each
core dashboard query against the configured database.  Used by
the ``eggpool stats explain-dashboard`` CLI command and by tests
that verify index coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.db.connection import Database

DASHBOARD_QUERY_NAMES = (
    "fetch_timeseries",
    "fetch_grouped_timeseries",
    "fetch_summary",
    "fetch_account_stats",
    "fetch_model_stats",
    "fetch_bandwidth_timeseries",
)


@dataclass
class QueryPlanEntry:
    name: str
    plan_lines: list[str]
    elapsed_ms: float


@dataclass
class ExplainResult:
    period: str
    bucket: str
    group_by: str
    queries: list[QueryPlanEntry] = field(default_factory=lambda: [])


async def explain_dashboard_queries(
    db: Database,
    period: str = "24h",
    bucket: str = "hour",
    group_by: str = "provider_model",
) -> ExplainResult:
    """Run EXPLAIN QUERY PLAN on each dashboard query and return plans + timings.

    Returns an :class:`ExplainResult` with one entry per dashboard query.
    Each entry contains the query name, the plan lines, and wall-clock
    elapsed time in milliseconds.
    """
    start, end = _resolve_period(period)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")

    queries = _build_queries(start_str, end_str, bucket, group_by)

    result = ExplainResult(period=period, bucket=bucket, group_by=group_by)
    for name, sql, params in queries:
        explain_sql = f"EXPLAIN QUERY PLAN {sql}"
        # Warm-up run (excluded from timing)
        await db.fetch_all(explain_sql, params)
        t0 = perf_counter()
        rows = await db.fetch_all(explain_sql, params)
        elapsed_ms = (perf_counter() - t0) * 1000
        plan_lines = [str(row["detail"]) for row in rows]
        result.queries.append(
            QueryPlanEntry(name=name, plan_lines=plan_lines, elapsed_ms=elapsed_ms)
        )

    return result


def _resolve_period(period: str) -> tuple[datetime, datetime]:
    """Resolve a period string into (start, end) UTC datetimes."""
    now = datetime.now(UTC)
    if period == "1h":
        return now - timedelta(hours=1), now
    if period == "7d":
        return now - timedelta(days=7), now
    if period == "30d":
        return now - timedelta(days=30), now
    return now - timedelta(hours=24), now


def _build_queries(
    start_str: str,
    end_str: str,
    bucket: str,
    group_by: str,
) -> list[tuple[str, str, tuple[Any, ...]]]:
    """Build (name, sql, params) tuples for each dashboard query."""
    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    ts_params: tuple[Any, ...] = (fmt, start_str, end_str)
    range_params: tuple[Any, ...] = (start_str, end_str)
    grouped_params: tuple[Any, ...] = (start_str, end_str)

    key_expr, label_expr = _resolve_group_exprs(group_by)

    timeseries_sql = """
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
    WHERE r.started_at >= ? AND r.started_at < ?
    GROUP BY bucket
    ORDER BY bucket
    """

    grouped_timeseries_sql = f"""
    SELECT
        strftime('{fmt}', r.started_at) as bucket,
        {key_expr} as raw_series_key,
        {label_expr} as raw_series_label,
        r.provider_id as provider_id,
        COALESCE(r.original_model_id, r.model_id) as model_id,
        a.name as account_name,
        COUNT(*) as request_count,
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
    GROUP BY bucket, raw_series_key, raw_series_label, r.provider_id,
        COALESCE(r.original_model_id, r.model_id), a.name
    ORDER BY bucket, raw_series_label ASC
    """

    summary_sql = """
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
    WHERE started_at >= ? AND started_at < ?
    """

    account_stats_sql = """
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
    ORDER BY a.name
    """

    model_stats_sql = """
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
        COALESCE(SUM(CASE WHEN r.exactness = 'unknown'
            OR r.exactness IS NULL THEN 1 ELSE 0 END), 0) as unknown_count,
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
    WHERE r.started_at >= ? AND r.started_at < ?
    GROUP BY COALESCE(r.original_model_id, r.model_id), r.provider_id
    ORDER BY request_count DESC
    """

    bandwidth_sql = """
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
    GROUP BY day
    ORDER BY day
    """

    return [
        ("fetch_timeseries", timeseries_sql, ts_params),
        ("fetch_grouped_timeseries", grouped_timeseries_sql, grouped_params),
        ("fetch_summary", summary_sql, range_params),
        ("fetch_account_stats", account_stats_sql, range_params),
        ("fetch_model_stats", model_stats_sql, range_params),
        ("fetch_bandwidth_timeseries", bandwidth_sql, range_params),
    ]


_GROUP_EXPRESSIONS: dict[str, tuple[str, str]] = {
    "provider": ("r.provider_id", "r.provider_id"),
    "model": (
        "COALESCE(r.original_model_id, r.model_id)",
        "COALESCE(r.original_model_id, r.model_id)",
    ),
    "provider_model": (
        "r.provider_id || ':' || COALESCE(r.original_model_id, r.model_id)",
        "r.provider_id || ' / ' || COALESCE(r.original_model_id, r.model_id)",
    ),
    "account": ("a.name", "a.name"),
}


def _resolve_group_exprs(group_by: str) -> tuple[str, str]:
    if group_by not in _GROUP_EXPRESSIONS:
        return _GROUP_EXPRESSIONS["provider_model"]
    return _GROUP_EXPRESSIONS[group_by]
