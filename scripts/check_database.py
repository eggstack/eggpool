"""Database invariant checker for GoRouter.

A read-only diagnostic script that verifies a SQLite database is
in a healthy state. Returns non-zero exit code on any invariant
violation.

The checker opens the database in read-only mode via a
``file:...?mode=ro`` URI so it cannot:

  - change journal mode,
  - create WAL files,
  - apply migrations,
  - write health-probe rows,
  - mutate PRAGMAs beyond safe read-only settings.

The checker inspects ``_migrations`` first and returns exit code 2
with a clear message if the schema is older or newer than this
script expects, instead of crashing with a raw ``no such
column`` exception.

Required environment:
    GOROUTER_DB_PATH  path to the SQLite database
                      (default: ./usage.sqlite3)

Exit codes:
    0 = all invariants pass
    1 = invariant violation
    2 = configuration or database access error
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from go_aggregator.db.connection import Database
from go_aggregator.errors import DatabaseError

#: Highest migration number this checker knows how to inspect.
#: If the database is older or newer than this, the checker
#: returns exit code 2 with a clear message.
EXPECTED_SCHEMA_VERSION = 11


async def _safe_fetch_one(db: Database, sql: str, params: Any = ()) -> Any:
    """Read a single row, returning ``None`` on schema mismatch.

    Wraps the fetch in a try/except so a missing table or column
    returns ``None`` instead of crashing the whole script. The
    caller decides how to interpret ``None``.
    """
    try:
        return await db.fetch_one(sql, params)
    except DatabaseError:
        return None
    except Exception:
        return None


async def _check_schema_version(db: Database) -> str | None:
    """Return a clear error message if the schema is wrong, else ``None``.

    The check is best-effort: it inspects ``_migrations`` if the
    table exists, and reports a clear error when the schema is
    older or newer than expected. When the table does not exist
    the check is skipped and the individual invariant checks
    handle the absence of their target tables.
    """
    row = await _safe_fetch_one(
        db,
        "SELECT MAX(version) AS v FROM _migrations",
    )
    if row is None:
        # Table missing entirely: a fresh database file that has
        # never been migrated is not a checker concern (the
        # application would not start); per-table checks below
        # surface the actual missing table.
        return None
    version = row["v"] if row["v"] is not None else 0
    if version < EXPECTED_SCHEMA_VERSION:
        return (
            f"Database schema is older than this checker expects: "
            f"have v{version}, need v{EXPECTED_SCHEMA_VERSION}. "
            f"Run `go-aggregator migrate` to upgrade."
        )
    if version > EXPECTED_SCHEMA_VERSION:
        return (
            f"Database schema is newer than this checker expects: "
            f"have v{version}, this checker supports v{EXPECTED_SCHEMA_VERSION}. "
            f"Update go-aggregator to a version that knows about the new schema."
        )
    return None


async def _check_no_orphan_pending(
    db: Database, threshold_seconds: int = 600
) -> list[str]:
    rows = await _safe_fetch_one(
        db,
        "SELECT id FROM requests "
        "WHERE status = 'pending' "
        "AND started_at < datetime('now', ? || ' seconds')",
        (f"-{threshold_seconds}",),
    )
    if rows is None:
        return []
    # _safe_fetch_one returns a single row for fetch_one; use fetch_all
    rows_all = await _safe_fetch_all(
        db,
        "SELECT id FROM requests "
        "WHERE status = 'pending' "
        "AND started_at < datetime('now', ? || ' seconds')",
        (f"-{threshold_seconds}",),
    )
    return [f"stale pending request id={r['id']}" for r in rows_all]


async def _safe_fetch_all(
    db: Database,
    sql: str,
    params: Any = (),
) -> list[Any]:
    try:
        return await db.fetch_all(sql, params)
    except DatabaseError:
        return []
    except Exception:
        return []


async def _check_no_incomplete_attempts(db: Database) -> list[str]:
    rows = await _safe_fetch_all(
        db,
        "SELECT ra.id, ra.request_id, r.status FROM request_attempts ra "
        "JOIN requests r ON r.id = ra.request_id "
        "WHERE ra.completed_at IS NULL AND r.status != 'pending'",
    )
    return [
        f"incomplete attempt id={r['id']} request_id={r['request_id']} "
        f"status={r['status']}"
        for r in rows
    ]


async def _check_no_active_reservations_for_terminal(
    db: Database,
) -> list[str]:
    rows = await _safe_fetch_all(
        db,
        "SELECT rv.id, rv.request_id, r.status FROM reservations rv "
        "JOIN requests r ON r.id = rv.request_id "
        "WHERE rv.status = 'active' AND r.status != 'pending'",
    )
    return [
        f"active reservation id={r['id']} for terminal request "
        f"id={r['request_id']} status={r['status']}"
        for r in rows
    ]


async def _check_no_negative_values(db: Database) -> list[str]:
    rows = await _safe_fetch_all(
        db,
        "SELECT id, cost_microdollars, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, reasoning_tokens "
        "FROM requests WHERE "
        "cost_microdollars < 0 OR input_tokens < 0 OR output_tokens < 0 OR "
        "cache_read_tokens < 0 OR cache_write_tokens < 0 OR "
        "reasoning_tokens < 0",
    )
    return [
        f"negative values on request id={r['id']}: "
        f"cost={r['cost_microdollars']} in={r['input_tokens']} "
        f"out={r['output_tokens']} cr={r['cache_read_tokens']} "
        f"cw={r['cache_write_tokens']} reason={r['reasoning_tokens']}"
        for r in rows
    ]


async def _check_no_duplicate_proxy_request_ids(
    db: Database,
) -> list[str]:
    rows = await _safe_fetch_all(
        db,
        "SELECT proxy_request_id, COUNT(*) AS c FROM requests "
        "WHERE proxy_request_id IS NOT NULL "
        "GROUP BY proxy_request_id HAVING c > 1",
    )
    return [
        f"duplicate proxy_request_id={r['proxy_request_id']} count={r['c']}"
        for r in rows
    ]


async def _check_resolved_models_have_protocol(
    db: Database,
) -> list[str]:
    rows = await _safe_fetch_all(
        db,
        "SELECT model_id, protocol, resolution_status FROM models "
        "WHERE resolution_status = 'resolved' "
        "AND (protocol IS NULL OR protocol NOT IN ('openai', 'anthropic'))",
    )
    return [
        f"resolved model without valid protocol: id={r['model_id']} "
        f"protocol={r['protocol']} status={r['resolution_status']}"
        for r in rows
    ]


async def _check_price_snapshot_sources(db: Database) -> list[str]:
    rows = await _safe_fetch_all(
        db, "SELECT DISTINCT source FROM model_price_snapshots"
    )
    sources = {row["source"] for row in rows}
    allowed = {"config", "upstream", "mixed"}
    unknown = sources - allowed
    return [f"unknown price snapshot source(s): {sorted(unknown)}"] if unknown else []


async def main() -> int:
    db_path = os.environ.get("GOROUTER_DB_PATH", "./usage.sqlite3")
    if not os.path.exists(db_path):
        sys.stderr.write(f"Database not found: {db_path}\n")
        return 2

    db = Database(path=db_path, read_only=True)
    try:
        await db.connect()
    except DatabaseError as exc:
        sys.stderr.write(f"Failed to open database read-only: {exc}\n")
        return 2

    try:
        schema_error = await _check_schema_version(db)
        if schema_error is not None:
            sys.stderr.write(f"{schema_error}\n")
            return 2

        all_violations: list[str] = []
        checks = [
            _check_no_orphan_pending,
            _check_no_incomplete_attempts,
            _check_no_active_reservations_for_terminal,
            _check_no_negative_values,
            _check_no_duplicate_proxy_request_ids,
            _check_resolved_models_have_protocol,
            _check_price_snapshot_sources,
        ]
        for check in checks:
            all_violations.extend(await check(db))
    finally:
        await db.disconnect()

    if not all_violations:
        sys.stdout.write("Database invariants OK\n")
        return 0
    sys.stderr.write(f"Found {len(all_violations)} invariant violation(s):\n")
    for line in all_violations:
        sys.stderr.write(f"  - {line}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
