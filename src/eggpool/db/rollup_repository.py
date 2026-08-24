"""Repository for usage_rollups buffered analytics rollups."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from eggpool.constants import SQLITE_INTEGER_MAX, clamp_sqlite_integer

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)

# Valid group_by dimensions for query_timeseries
_VALID_GROUP_BY = frozenset({"provider_model", "provider", "model", "account"})

# SQL fragment mapping group_by dimension to SELECT key expression
_GROUP_KEY_EXPR: dict[str, str] = {
    "provider_model": "provider_id || '/' || model_id",
    "provider": "provider_id",
    "model": "model_id",
    "account": "CAST(account_id AS TEXT)",
}


def _coerce_non_negative_int(value: object) -> int:
    try:
        numeric = int(cast("Any", value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return clamp_sqlite_integer(numeric)


def _append_optional_filters(
    conditions: list[str],
    params: list[Any],
    *,
    provider_id: str | None = None,
    model_id: str | None = None,
    account_id: int | None = None,
) -> None:
    """Append common rollup filters to a WHERE clause under construction."""
    if provider_id is not None:
        conditions.append("provider_id = ?")
        params.append(provider_id)
    if model_id is not None:
        conditions.append("model_id = ?")
        params.append(model_id)
    if account_id is not None:
        conditions.append("account_id = ?")
        params.append(account_id)


def _bucket_expression(bucket_size_s: int) -> str:
    """Return the SQLite expression for a dashboard bucket size."""
    if bucket_size_s == 3600:
        return "strftime('%Y-%m-%d %H:00:00', bucket_start)"
    if bucket_size_s == 86400:
        return "strftime('%Y-%m-%d 00:00:00', bucket_start)"
    return "bucket_start"


class UsageRollupRepository:
    """Operations for the usage_rollups buffered analytics table.

    Counter fields are designed for additive upserts: each flush
    increments the existing row's counters rather than replacing them.
    Latency min/max use CASE/WHEN to converge monotonically within
    each bucket.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def _select_source_bucket_size(
        self,
        *,
        start: str,
        end: str,
        target_bucket_size_s: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        account_id: int | None = None,
    ) -> int | None:
        """Choose the largest available rollup that fits the target bucket.

        Metrics may be persisted at 60 or 300 seconds while dashboard charts
        request hourly or daily buckets. Prefer the largest available source
        bucket not exceeding the requested size so rows are not double-counted
        when an operator changes the metrics bucket configuration.
        """
        conditions = [
            "bucket_start >= ?",
            "bucket_start < ?",
            "bucket_size_s <= ?",
        ]
        params: list[Any] = [start, end, target_bucket_size_s]
        _append_optional_filters(
            conditions,
            params,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )
        row = await self._db.fetch_one(
            "SELECT MAX(bucket_size_s) AS source_bucket_size_s "
            "FROM usage_rollups WHERE " + " AND ".join(conditions),
            tuple(params),
        )
        if row is None or row["source_bucket_size_s"] is None:
            return None
        return int(row["source_bucket_size_s"])

    async def upsert_many(self, rows: list[dict[str, object]]) -> int:
        """Upsert multiple rollup rows. Returns count of rows processed."""
        if not rows:
            return 0

        sql = (
            "INSERT INTO usage_rollups ("
            "bucket_start, bucket_size_s, provider_id, model_id, "
            "account_id, protocol, streamed, status, "
            "request_count, error_count, retry_count, "
            "input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, "
            "reasoning_tokens, thinking_characters, "
            "cost_microdollars, bytes_received, bytes_emitted, "
            "latency_ms_sum, latency_ms_min, latency_ms_max, "
            "first_byte_ms_sum, first_byte_ms_count"
            ") VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, "
            "?, ?, "
            "?, ?, "
            "?, ?, "
            "?, ?, ?, "
            "?, ?, ?, "
            "?, ?"
            ") ON CONFLICT ("
            "bucket_start, bucket_size_s, provider_id, model_id, "
            "account_id, protocol, streamed, status"
            ") DO UPDATE SET "
            "request_count = request_count + excluded.request_count, "
            "error_count = error_count + excluded.error_count, "
            "retry_count = retry_count + excluded.retry_count, "
            "input_tokens = input_tokens + excluded.input_tokens, "
            "output_tokens = output_tokens + excluded.output_tokens, "
            "cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens, "
            "cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens, "
            "reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens, "
            "thinking_characters = thinking_characters + excluded.thinking_characters, "
            "cost_microdollars = CASE "
            "  WHEN excluded.cost_microdollars <= 0 THEN cost_microdollars "
            "  WHEN cost_microdollars >= ? - excluded.cost_microdollars THEN ? "
            "  ELSE cost_microdollars + excluded.cost_microdollars "
            "END, "
            "bytes_received = bytes_received + excluded.bytes_received, "
            "bytes_emitted = bytes_emitted + excluded.bytes_emitted, "
            "latency_ms_sum = latency_ms_sum + excluded.latency_ms_sum, "
            "latency_ms_min = CASE "
            "  WHEN excluded.latency_ms_min IS NULL THEN latency_ms_min "
            "  WHEN latency_ms_min IS NULL THEN excluded.latency_ms_min "
            "  WHEN excluded.latency_ms_min < latency_ms_min "
            "    THEN excluded.latency_ms_min "
            "  ELSE latency_ms_min "
            "END, "
            "latency_ms_max = CASE "
            "  WHEN excluded.latency_ms_max IS NULL THEN latency_ms_max "
            "  WHEN latency_ms_max IS NULL THEN excluded.latency_ms_max "
            "  WHEN excluded.latency_ms_max > latency_ms_max "
            "    THEN excluded.latency_ms_max "
            "  ELSE latency_ms_max "
            "END, "
            "first_byte_ms_sum = first_byte_ms_sum + excluded.first_byte_ms_sum, "
            "first_byte_ms_count = first_byte_ms_count + excluded.first_byte_ms_count, "
            "updated_at = CURRENT_TIMESTAMP"
        )

        params_list = [
            (
                row["bucket_start"],
                row["bucket_size_s"],
                row["provider_id"],
                row["model_id"],
                row["account_id"],
                row["protocol"],
                row["streamed"],
                row["status"],
                row.get("request_count", 0),
                row.get("error_count", 0),
                row.get("retry_count", 0),
                row.get("input_tokens", 0),
                row.get("output_tokens", 0),
                row.get("cache_read_tokens", 0),
                row.get("cache_write_tokens", 0),
                row.get("reasoning_tokens", 0),
                row.get("thinking_characters", 0),
                _coerce_non_negative_int(row.get("cost_microdollars", 0)),
                row.get("bytes_received", 0),
                row.get("bytes_emitted", 0),
                row.get("latency_ms_sum", 0),
                row.get("latency_ms_min"),
                row.get("latency_ms_max"),
                row.get("first_byte_ms_sum", 0),
                row.get("first_byte_ms_count", 0),
                SQLITE_INTEGER_MAX,
                SQLITE_INTEGER_MAX,
            )
            for row in rows
        ]

        async with self._db.transaction():
            await self._db.execute_many(sql, params_list)

        return len(rows)

    async def query_timeseries(
        self,
        *,
        start: str,
        end: str,
        bucket_size_s: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        account_id: int | None = None,
        group_by: str = "provider_model",
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Query rollups for timeseries grouped data.

        Returns points ordered by bucket_start, each containing the
        grouped series_key and per-bucket counters.  Derived averages
        (avg_latency_ms, avg_ttft_ms) are computed from sums/counts.
        """
        if group_by not in _VALID_GROUP_BY:
            raise ValueError(
                f"Invalid group_by {group_by!r}; "
                f"must be one of {sorted(_VALID_GROUP_BY)}"
            )

        group_expr = _GROUP_KEY_EXPR[group_by]

        source_bucket_size_s = await self._select_source_bucket_size(
            start=start,
            end=end,
            target_bucket_size_s=bucket_size_s,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )
        if source_bucket_size_s is None:
            return []

        bucket_expr = _bucket_expression(bucket_size_s)
        conditions = ["bucket_start >= ?", "bucket_start < ?", "bucket_size_s = ?"]
        params: list[Any] = [start, end, source_bucket_size_s]

        _append_optional_filters(
            conditions,
            params,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )

        where = " AND ".join(conditions)

        sql = (
            f"SELECT "
            f"{bucket_expr} AS bucket, "
            f"{group_expr} AS series_key, "
            f"SUM(request_count) AS request_count, "
            f"SUM(error_count) AS error_count, "
            f"SUM(retry_count) AS retry_count, "
            f"SUM(input_tokens) AS input_tokens, "
            f"SUM(output_tokens) AS output_tokens, "
            f"SUM(cache_read_tokens) AS cache_read_tokens, "
            f"SUM(cache_write_tokens) AS cache_write_tokens, "
            f"SUM(reasoning_tokens) AS reasoning_tokens, "
            f"SUM(thinking_characters) AS thinking_characters, "
            f"SUM(cost_microdollars) AS cost_microdollars, "
            f"SUM(bytes_received) AS bytes_received, "
            f"SUM(bytes_emitted) AS bytes_emitted, "
            f"SUM(latency_ms_sum) AS latency_ms_sum, "
            f"MIN(latency_ms_min) AS latency_ms_min, "
            f"MAX(latency_ms_max) AS latency_ms_max, "
            f"SUM(first_byte_ms_sum) AS first_byte_ms_sum, "
            f"SUM(first_byte_ms_count) AS first_byte_ms_count, "
            f"COALESCE("
            f"  SUM(CASE WHEN streamed = 1 THEN request_count ELSE 0 END), 0"
            f") AS streamed, "
            f"CASE WHEN SUM(first_byte_ms_count) > 0 "
            f"  THEN CAST(SUM(first_byte_ms_sum) AS REAL) "
            f"       / SUM(first_byte_ms_count) "
            f"  ELSE 0 END AS avg_ttft_ms, "
            f"CASE WHEN SUM(request_count) > 0 "
            f"  THEN CAST(SUM(latency_ms_sum) AS REAL) "
            f"       / SUM(request_count) "
            f"  ELSE 0 END AS avg_latency_ms "
            f"FROM usage_rollups "
            f"WHERE {where} "
            f"GROUP BY {bucket_expr}, series_key "
            f"ORDER BY bucket "
        )
        if source_bucket_size_s == bucket_size_s:
            sql += " LIMIT ?"
            params.append(limit)

        rows = await self._db.fetch_all(sql, tuple(params))
        return [dict(row) for row in rows]

    async def query_summary(
        self,
        *,
        start: str,
        end: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        account_id: int | None = None,
    ) -> dict[str, object]:
        """Query aggregate summary from rollups."""
        conditions = ["bucket_start >= ?", "bucket_start < ?"]
        params: list[Any] = [start, end]
        _append_optional_filters(
            conditions,
            params,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )
        where = " AND ".join(conditions)

        sql = (
            "SELECT "
            "COALESCE(SUM(request_count), 0) AS total_requests, "
            "COALESCE(SUM(error_count), 0) AS error_requests, "
            "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS total_output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read_tokens, "
            "COALESCE(SUM(cache_write_tokens), 0) AS total_cache_write_tokens, "
            "COALESCE(SUM(reasoning_tokens), 0) AS total_reasoning_tokens, "
            "COALESCE(SUM(cost_microdollars), 0) AS total_cost_microdollars, "
            "COALESCE(SUM(bytes_received), 0) AS total_bytes_received, "
            "COALESCE(SUM(bytes_emitted), 0) AS total_bytes_emitted, "
            "COALESCE("
            "  SUM(CASE WHEN streamed = 1 THEN request_count ELSE 0 END), 0"
            ") AS streamed_requests, "
            "CASE WHEN SUM(request_count) > 0 "
            "  THEN CAST("
            "    SUM(CASE WHEN streamed = 0 THEN request_count ELSE 0 END) AS REAL"
            "  ) "
            "  ELSE 0 END AS non_streamed_requests, "
            "CASE WHEN SUM(request_count) > 0 "
            "  THEN CAST(SUM(latency_ms_sum) AS REAL) / SUM(request_count) "
            "  ELSE 0 END AS avg_latency_ms, "
            "CASE WHEN SUM(first_byte_ms_count) > 0 "
            "  THEN CAST(SUM(first_byte_ms_sum) AS REAL) "
            "       / SUM(first_byte_ms_count) "
            "  ELSE 0 END AS avg_ttft_ms, "
            "CASE WHEN COALESCE("
            "    SUM(CASE WHEN status != 'pending' THEN latency_ms_sum ELSE 0 END), "
            "    0"
            "  ) > 0 "
            "  THEN CAST("
            "    SUM(CASE WHEN status != 'pending' THEN output_tokens ELSE 0 END) "
            "    AS REAL"
            "  ) * 1000.0 "
            "  / SUM(CASE WHEN status != 'pending' THEN latency_ms_sum ELSE 0 END) "
            "  ELSE 0 END AS tokens_per_second "
            "FROM usage_rollups "
            f"WHERE {where}"
        )
        row = await self._db.fetch_one(sql, tuple(params))
        if row is None:
            return {
                "total_requests": 0,
                "error_requests": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_read_tokens": 0,
                "total_cache_write_tokens": 0,
                "total_reasoning_tokens": 0,
                "total_cost_microdollars": 0,
                "total_bytes_received": 0,
                "total_bytes_emitted": 0,
                "streamed_requests": 0,
                "non_streamed_requests": 0,
                "avg_latency_ms": 0.0,
                "avg_ttft_ms": 0.0,
                "tokens_per_second": 0.0,
            }
        return dict(row)

    async def query_flat_timeseries(
        self,
        *,
        start: str,
        end: str,
        bucket_size_s: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        account_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Query rollups for flat (non-grouped) timeseries.

        Returns one row per bucket with sums across all series.
        """
        source_bucket_size_s = await self._select_source_bucket_size(
            start=start,
            end=end,
            target_bucket_size_s=bucket_size_s,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )
        if source_bucket_size_s is None:
            return []

        bucket_expr = _bucket_expression(bucket_size_s)
        conditions = ["bucket_start >= ?", "bucket_start < ?", "bucket_size_s = ?"]
        params: list[Any] = [start, end, source_bucket_size_s]
        _append_optional_filters(
            conditions,
            params,
            provider_id=provider_id,
            model_id=model_id,
            account_id=account_id,
        )
        where = " AND ".join(conditions)

        sql = (
            "SELECT "
            f"{bucket_expr} AS bucket, "
            "SUM(request_count) AS request_count, "
            "SUM(error_count) AS error_count, "
            "SUM(retry_count) AS retry_count, "
            "SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_write_tokens) AS cache_write_tokens, "
            "SUM(reasoning_tokens) AS reasoning_tokens, "
            "SUM(cost_microdollars) AS cost_microdollars, "
            "SUM(bytes_received) AS bytes_received, "
            "SUM(bytes_emitted) AS bytes_emitted, "
            "SUM(latency_ms_sum) AS latency_ms_sum, "
            "CASE WHEN SUM(request_count) > 0 "
            "  THEN CAST(SUM(latency_ms_sum) AS REAL) "
            "       / SUM(request_count) "
            "  ELSE 0 END AS avg_latency_ms, "
            "CASE WHEN SUM(first_byte_ms_count) > 0 "
            "  THEN CAST(SUM(first_byte_ms_sum) AS REAL) "
            "       / SUM(first_byte_ms_count) "
            "  ELSE 0 END AS avg_ttft_ms "
            "FROM usage_rollups "
            f"WHERE {where} "
            f"GROUP BY {bucket_expr} "
            "ORDER BY bucket"
        )
        rows = await self._db.fetch_all(sql, tuple(params))
        return [dict(row) for row in rows]

    async def latest_bucket_start(
        self,
        *,
        end: str,
        account_id: int | None = None,
    ) -> str | None:
        """Return the most recent ``bucket_start`` <= ``end`` for any rollup row.

        Used by ``StatsService._rollup_is_fresh`` to compare against the
        live ``requests`` table.  Returns ``None`` when no rollups
        exist in the window (caller falls back to live aggregation).
        The comparison bound is inclusive on ``end`` so the freshness
        check tolerates clock skew between the writer and the
        scheduler that picks the time range.
        """
        conditions = ["bucket_start <= ?"]
        params: list[Any] = [end]
        _append_optional_filters(
            conditions,
            params,
            account_id=account_id,
        )
        where = " AND ".join(conditions)
        row = await self._db.fetch_one(
            f"SELECT MAX(bucket_start) AS latest FROM usage_rollups WHERE {where}",
            tuple(params),
        )
        if row is None:
            return None
        latest = row["latest"]
        if latest is None:
            return None
        return str(latest)
