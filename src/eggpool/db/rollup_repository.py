"""Repository for usage_rollups buffered analytics rollups."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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


class UsageRollupRepository:
    """Operations for the usage_rollups buffered analytics table.

    Counter fields are designed for additive upserts: each flush
    increments the existing row's counters rather than replacing them.
    Latency min/max use CASE/WHEN to converge monotonically within
    each bucket.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

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
            "cost_microdollars = cost_microdollars + excluded.cost_microdollars, "
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
                row.get("cost_microdollars", 0),
                row.get("bytes_received", 0),
                row.get("bytes_emitted", 0),
                row.get("latency_ms_sum", 0),
                row.get("latency_ms_min"),
                row.get("latency_ms_max"),
                row.get("first_byte_ms_sum", 0),
                row.get("first_byte_ms_count", 0),
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

        conditions = ["bucket_start >= ?", "bucket_start < ?", "bucket_size_s = ?"]
        params: list[Any] = [start, end, bucket_size_s]

        if provider_id is not None:
            conditions.append("provider_id = ?")
            params.append(provider_id)
        if model_id is not None:
            conditions.append("model_id = ?")
            params.append(model_id)
        if account_id is not None:
            conditions.append("account_id = ?")
            params.append(account_id)

        where = " AND ".join(conditions)

        sql = (
            f"SELECT "
            f"bucket_start AS bucket, "
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
            f"SUM(streamed) AS streamed, "
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
            f"GROUP BY bucket_start, series_key "
            f"ORDER BY bucket_start "
            f"LIMIT ?"
        )
        params.append(limit)

        rows = await self._db.fetch_all(sql, tuple(params))
        return [dict(row) for row in rows]

    async def query_summary(
        self,
        *,
        start: str,
        end: str,
    ) -> dict[str, object]:
        """Query aggregate summary from rollups.

        Returns a single dict with total_requests, total_input_tokens,
        total_output_tokens, total_cost_microdollars, total_bytes_received,
        total_bytes_emitted, total_latency_ms_sum, total_latency_count,
        and avg_latency_ms.
        """
        sql = (
            "SELECT "
            "COALESCE(SUM(request_count), 0) AS total_requests, "
            "COALESCE(SUM(input_tokens), 0) AS total_input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS total_output_tokens, "
            "COALESCE(SUM(cost_microdollars), 0) AS total_cost_microdollars, "
            "COALESCE(SUM(bytes_received), 0) AS total_bytes_received, "
            "COALESCE(SUM(bytes_emitted), 0) AS total_bytes_emitted, "
            "COALESCE(SUM(latency_ms_sum), 0) AS total_latency_ms_sum, "
            "COALESCE(SUM(request_count), 0) AS total_latency_count, "
            "CASE WHEN SUM(request_count) > 0 "
            "  THEN CAST(SUM(latency_ms_sum) AS REAL) / SUM(request_count) "
            "  ELSE 0 END AS avg_latency_ms "
            "FROM usage_rollups "
            "WHERE bucket_start >= ? AND bucket_start < ?"
        )
        row = await self._db.fetch_one(sql, (start, end))
        if row is None:
            return {
                "total_requests": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_microdollars": 0,
                "total_bytes_received": 0,
                "total_bytes_emitted": 0,
                "total_latency_ms_sum": 0,
                "total_latency_count": 0,
                "avg_latency_ms": 0.0,
            }
        return dict(row)

    async def cleanup_old_rollups(self, retain_days: int, max_rows: int = 5000) -> int:
        """Delete old rollup buckets. Returns rows deleted.

        Uses chunked deletes with a LIMIT to avoid holding the write
        lock for too long on large tables.
        """
        total_deleted = 0
        while True:
            async with self._db.transaction():
                deleted = await self._db.execute_write(
                    "DELETE FROM usage_rollups "
                    "WHERE rowid IN ("
                    "  SELECT rowid FROM usage_rollups "
                    "  WHERE bucket_start < datetime('now', ? || ' days') "
                    "  LIMIT ?"
                    ")",
                    (f"-{retain_days}", max_rows),
                )
            total_deleted += deleted
            if deleted < max_rows:
                break

        if total_deleted > 0:
            logger.info(
                "Deleted %d old usage_rollups rows (retention=%d days)",
                total_deleted,
                retain_days,
            )
        return total_deleted
