"""Repository for model-info sidecar tables."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from eggpool.model_info.types import (
    CanonicalModelInfo,
    SourceModelRecord,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)


class ModelInfoRepository:
    """Persistence layer for model-info canonical and observation rows."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_observation(
        self,
        record: SourceModelRecord,
        *,
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> int:
        """Insert or update a source observation by (source, source_model_id, raw_hash).

        Returns the row id of the inserted/updated observation.
        """
        resolved_model_id = model_id or record.model_id
        resolved_provider_id = provider_id or record.provider_id

        normalized_json = json.dumps(record.normalized, sort_keys=True)
        raw_json = json.dumps(record.raw_payload, sort_keys=True)

        async with self._db.transaction():
            row = await self._db.fetch_one(
                "SELECT id FROM model_info_observations "
                "WHERE source = ? AND source_model_id = ? AND raw_hash = ?",
                (record.source, record.source_model_id, record.raw_hash),
            )
            if row is not None:
                await self._db.execute_write(
                    "UPDATE model_info_observations SET "
                    "model_id = ?, provider_id = ?, observed_at = ?, "
                    "confidence = ?, normalized_json = ?, raw_json = ? "
                    "WHERE id = ?",
                    (
                        resolved_model_id,
                        resolved_provider_id,
                        record.observed_at.isoformat(),
                        record.confidence,
                        normalized_json,
                        raw_json,
                        row["id"],
                    ),
                )
                return row["id"]

            cursor = await self._db.execute_insert(
                "INSERT INTO model_info_observations "
                "(model_id, provider_id, source, source_model_id, observed_at, "
                "confidence, raw_hash, normalized_json, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resolved_model_id,
                    resolved_provider_id,
                    record.source,
                    record.source_model_id,
                    record.observed_at.isoformat(),
                    record.confidence,
                    record.raw_hash,
                    normalized_json,
                    raw_json,
                ),
            )
            return cursor

    async def upsert_alias(
        self,
        model_id: str,
        provider_id: str | None,
        alias: str,
        source: str,
        confidence: float = 0.5,
        active: bool = True,
    ) -> None:
        """Insert or update an alias row."""
        async with self._db.transaction():
            await self._db.execute_write(
                "INSERT INTO model_info_aliases "
                "(model_id, provider_id, alias, source, confidence, active) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider_id, alias, source) DO UPDATE SET "
                "model_id = excluded.model_id, confidence = excluded.confidence, "
                "active = excluded.active, last_seen_at = CURRENT_TIMESTAMP",
                (model_id, provider_id, alias, source, confidence, int(active)),
            )

    async def upsert_canonical(self, info: CanonicalModelInfo) -> None:
        """Write canonical status/detail/provenance/conflicts."""
        async with self._db.transaction():
            await self._execute_upsert_canonical(info)

    async def upsert_canonical_batch(self, infos: list[CanonicalModelInfo]) -> int:
        """Write multiple canonical rows inside a single transaction.

        Returns the number of rows actually written (skipped rows where
        the payload is byte-identical to the existing row are not counted
        as writes, though they still acquire the transaction lock).
        """
        if not infos:
            return 0
        written = 0
        async with self._db.transaction():
            for info in infos:
                await self._execute_upsert_canonical(info)
                written += 1
        return written

    async def _execute_upsert_canonical(self, info: CanonicalModelInfo) -> None:
        """Execute a single canonical upsert (must be inside a transaction)."""
        await self._db.execute_write(
            "INSERT INTO model_info_canonical "
            "(model_id, status, summary, detail_json, provenance_json, "
            "conflicts_json, sparse, first_seen_at, last_seen_at, "
            "last_refreshed_at, next_refresh_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET "
            "status = excluded.status, summary = excluded.summary, "
            "detail_json = excluded.detail_json, "
            "provenance_json = excluded.provenance_json, "
            "conflicts_json = excluded.conflicts_json, "
            "sparse = excluded.sparse, last_seen_at = excluded.last_seen_at, "
            "last_refreshed_at = excluded.last_refreshed_at, "
            "next_refresh_at = excluded.next_refresh_at",
            (
                info.model_id,
                info.status,
                info.summary,
                json.dumps(info.detail, sort_keys=True),
                json.dumps(info.provenance, sort_keys=True),
                json.dumps(info.conflicts, sort_keys=True),
                int(info.sparse),
                info.first_seen_at.isoformat(),
                info.last_seen_at.isoformat(),
                (
                    info.last_refreshed_at.isoformat()
                    if info.last_refreshed_at
                    else None
                ),
                (info.next_refresh_at.isoformat() if info.next_refresh_at else None),
            ),
        )

    async def get_canonical(self, model_id: str) -> CanonicalModelInfo | None:
        """Return one canonical record."""
        row = await self._db.fetch_one(
            "SELECT * FROM model_info_canonical WHERE model_id = ?",
            (model_id,),
        )
        if row is None:
            return None
        return self._row_to_canonical(row)

    async def get_canonical_many(
        self, model_ids: list[str] | None = None
    ) -> dict[str, CanonicalModelInfo]:
        """Return canonical records keyed by model ID."""
        if model_ids is not None:
            placeholders = ",".join("?" for _ in model_ids)
            rows = await self._db.fetch_all(
                "SELECT * FROM model_info_canonical "
                f"WHERE model_id IN ({placeholders})",
                tuple(model_ids),
            )
        else:
            rows = await self._db.fetch_all("SELECT * FROM model_info_canonical")
        return {row["model_id"]: self._row_to_canonical(row) for row in rows}

    async def list_due(
        self, limit: int = 50, now: datetime | None = None
    ) -> list[CanonicalModelInfo]:
        """List canonical rows due for refresh, ordered by status priority."""
        if now is None:
            now = datetime.now(UTC)
        rows = await self._db.fetch_all(
            "SELECT * FROM model_info_canonical "
            "WHERE next_refresh_at IS NULL OR next_refresh_at <= ? "
            "ORDER BY "
            "CASE status "
            "  WHEN 'conflicting' THEN 0 "
            "  WHEN 'sparse_new' THEN 1 "
            "  WHEN 'partial' THEN 2 "
            "  WHEN 'stale' THEN 3 "
            "  WHEN 'fresh' THEN 4 "
            "  WHEN 'unmatched' THEN 5 "
            "  WHEN 'source_unavailable' THEN 6 "
            "  WHEN 'manual_override' THEN 7 "
            "  WHEN 'withdrawn' THEN 8 "
            "  ELSE 9 "
            "END, "
            "COALESCE(next_refresh_at, ?) "
            "LIMIT ?",
            (now.isoformat(), now.isoformat(), limit),
        )
        return [self._row_to_canonical(row) for row in rows]

    async def record_source_success(self, source: str) -> None:
        """Record a successful fetch from a source."""
        async with self._db.transaction():
            await self._db.execute_write(
                "INSERT INTO model_info_source_health "
                "(source, last_success_at, failure_count) "
                "VALUES (?, CURRENT_TIMESTAMP, 0) "
                "ON CONFLICT(source) DO UPDATE SET "
                "last_success_at = CURRENT_TIMESTAMP, "
                "last_error_class = NULL, last_error_message = NULL, "
                "failure_count = 0",
                (source,),
            )

    async def record_source_error(
        self,
        source: str,
        exc: Exception,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Record an error from a source, incrementing failure_count."""
        async with self._db.transaction():
            await self._db.execute_write(
                "INSERT INTO model_info_source_health "
                "(source, last_error_at, last_error_class, last_error_message, "
                "cooldown_until, failure_count) "
                "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, 1) "
                "ON CONFLICT(source) DO UPDATE SET "
                "last_error_at = CURRENT_TIMESTAMP, "
                "last_error_class = excluded.last_error_class, "
                "last_error_message = excluded.last_error_message, "
                "cooldown_until = excluded.cooldown_until, "
                "failure_count = failure_count + 1",
                (
                    source,
                    type(exc).__qualname__,
                    str(exc)[:500],
                    cooldown_until.isoformat() if cooldown_until else None,
                ),
            )

    async def source_health_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return source health rows as a dict keyed by source name."""
        rows = await self._db.fetch_all("SELECT * FROM model_info_source_health")
        return {
            row["source"]: {
                "enabled": bool(row["enabled"]),
                "last_success_at": row["last_success_at"],
                "last_error_at": row["last_error_at"],
                "last_error_class": row["last_error_class"],
                "last_error_message": row["last_error_message"],
                "cooldown_until": row["cooldown_until"],
                "failure_count": int(row["failure_count"]),
            }
            for row in rows
        }

    async def get_source_failure_count(self, source: str) -> int:
        """Return the current failure_count for a source (0 if unknown)."""
        row = await self._db.fetch_one(
            "SELECT failure_count FROM model_info_source_health WHERE source = ?",
            (source,),
        )
        if row is None:
            return 0
        return int(row["failure_count"])

    @staticmethod
    def _row_to_canonical(row: Any) -> CanonicalModelInfo:
        """Convert a database row to a CanonicalModelInfo."""
        return CanonicalModelInfo(
            model_id=row["model_id"],
            status=row["status"],
            summary=row["summary"],
            sparse=bool(row["sparse"]),
            detail=json.loads(row["detail_json"]) if row["detail_json"] else {},
            provenance=(
                json.loads(row["provenance_json"]) if row["provenance_json"] else {}
            ),
            conflicts=(
                json.loads(row["conflicts_json"]) if row["conflicts_json"] else {}
            ),
            first_seen_at=_parse_timestamp(row["first_seen_at"]),
            last_seen_at=_parse_timestamp(row["last_seen_at"]),
            last_refreshed_at=(
                _parse_timestamp(row["last_refreshed_at"])
                if row["last_refreshed_at"]
                else None
            ),
            next_refresh_at=(
                _parse_timestamp(row["next_refresh_at"])
                if row["next_refresh_at"]
                else None
            ),
        )


def _parse_timestamp(value: str | None) -> datetime:
    """Parse a SQLite timestamp string to a datetime object."""
    if value is None:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(str(value).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return datetime.now(UTC)
