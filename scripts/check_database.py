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

import argparse
import asyncio
import os
import sys
import tomllib
from typing import TYPE_CHECKING, Any

from go_aggregator.db.connection import Database
from go_aggregator.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite


class CheckerError(Exception):
    """Base class for internal checker errors."""


class SchemaCompatibilityError(CheckerError):
    """Raised when the database schema is missing or incompatible."""


class InvariantQueryError(CheckerError):
    """Raised when an invariant query cannot be executed."""


#: Highest migration number this checker knows how to inspect.
#: If the database is older or newer than this, the checker
#: returns exit code 2 with a clear message.
EXPECTED_SCHEMA_VERSION = 15

#: Required tables for the production GoRouter schema.
REQUIRED_TABLES: frozenset[str] = frozenset(
    {
        "accounts",
        "models",
        "account_models",
        "requests",
        "request_attempts",
        "reservations",
        "model_price_snapshots",
        "account_events",
        "health_probe",
        "providers",
        "_migrations",
    }
)

#: Required columns for the production GoRouter schema, grouped by table.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "requests": frozenset(
        {
            "id",
            "proxy_request_id",
            "status",
            "started_at",
            "cost_microdollars",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        }
    ),
    "request_attempts": frozenset({"id", "request_id", "completed_at"}),
    "reservations": frozenset({"id", "request_id", "status"}),
    "models": frozenset({"model_id", "protocol", "resolution_status", "provider_id"}),
    "model_price_snapshots": frozenset({"source"}),
}


async def _fetch_one_checked(
    db: Database,
    sql: str,
    params: Sequence[Any] = (),
    *,
    check_name: str,
) -> aiosqlite.Row | None:
    """Fetch a single row or ``None``, raising on query failure.

    The :class:`InvariantQueryError` deliberately does NOT include the
    SQL text or parameter values so an operator-facing error does not
    echo arbitrary content into logs.
    """
    try:
        return await db.fetch_one(sql, params)
    except DatabaseError as exc:
        raise InvariantQueryError(f"Invariant query failed: {check_name}") from exc


async def _fetch_all_checked(
    db: Database,
    sql: str,
    params: Sequence[Any] = (),
    *,
    check_name: str,
) -> list[aiosqlite.Row]:
    """Fetch all rows, raising :class:`InvariantQueryError` on failure.

    The error message intentionally excludes SQL text and parameter
    values to avoid leaking arbitrary content into operator-visible logs.
    """
    try:
        return await db.fetch_all(sql, params)
    except DatabaseError as exc:
        raise InvariantQueryError(f"Invariant query failed: {check_name}") from exc


async def _table_column_names(
    db: Database,
    table_name: str,
) -> set[str]:
    """Return the column names for ``table_name`` via ``PRAGMA table_info``.

    Returns an empty set if the table does not exist (caller decides how
    to interpret the result).
    """
    rows = await _fetch_all_checked(
        db,
        f"PRAGMA table_info({table_name})",
        check_name=f"pragma_table_info:{table_name}",
    )
    return {row["name"] for row in rows}


async def _table_names(db: Database) -> set[str]:
    """Return the set of user-visible table names.

    Excludes internal ``_migrations`` and any ``sqlite_*`` tables.
    """
    rows = await _fetch_all_checked(
        db,
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        check_name="sqlite_master:tables",
    )
    return {row["name"] for row in rows}


async def _validate_required_schema(db: Database) -> None:
    """Preflight-check the required tables and columns.

    Raises :class:`SchemaCompatibilityError` if a required table or
    column is missing. This is a read-only check: no schema mutation
    is performed.
    """
    tables = await _table_names(db)
    missing_tables = REQUIRED_TABLES - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise SchemaCompatibilityError(
            f"Database is missing required tables: {missing}. "
            "Run `go-aggregator migrate` before checking invariants."
        )

    for table, required_cols in REQUIRED_COLUMNS.items():
        columns = await _table_column_names(db, table)
        missing_cols = required_cols - columns
        if missing_cols:
            missing = ", ".join(sorted(missing_cols))
            raise SchemaCompatibilityError(
                f"Table {table!r} is missing required columns: {missing}. "
                "Run `go-aggregator migrate` before checking invariants."
            )


async def _check_schema_version(db: Database) -> None:
    """Verify the database is initialized with the expected migration set.

    Raises :class:`SchemaCompatibilityError` when:

    - ``_migrations`` is missing entirely (uninitialized database);
    - ``_migrations`` has no recorded version (empty initialization);
    - the highest recorded version is older or newer than
      :data:`EXPECTED_SCHEMA_VERSION`.
    """
    tables = await _table_names(db)
    if "_migrations" not in tables:
        raise SchemaCompatibilityError(
            "Database is not initialized with the expected GoRouter schema. "
            "Run `go-aggregator migrate` before checking invariants."
        )

    row = await _fetch_one_checked(
        db,
        "SELECT MAX(version) AS v FROM _migrations",
        check_name="_migrations:max_version",
    )
    if row is None or row["v"] is None:
        raise SchemaCompatibilityError(
            "Database `_migrations` table is empty. "
            "Run `go-aggregator migrate` before checking invariants."
        )

    version = int(row["v"])
    if version < EXPECTED_SCHEMA_VERSION:
        raise SchemaCompatibilityError(
            f"Database schema is older than this checker expects: "
            f"have v{version}, need v{EXPECTED_SCHEMA_VERSION}. "
            "Run `go-aggregator migrate` to upgrade."
        )
    if version > EXPECTED_SCHEMA_VERSION:
        raise SchemaCompatibilityError(
            f"Database schema is newer than this checker expects: "
            f"have v{version}, this checker supports v{EXPECTED_SCHEMA_VERSION}. "
            "Update go-aggregator to a version that knows about the new schema."
        )


async def _check_no_orphan_pending(
    db: Database, threshold_seconds: int = 600
) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT id FROM requests "
        "WHERE status = 'pending' "
        "AND started_at < datetime('now', ? || ' seconds')",
        (f"-{threshold_seconds}",),
        check_name="requests:stale_pending",
    )
    return [f"stale pending request id={r['id']}" for r in rows]


async def _check_no_incomplete_attempts(db: Database) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT ra.id, ra.request_id, r.status FROM request_attempts ra "
        "JOIN requests r ON r.id = ra.request_id "
        "WHERE ra.completed_at IS NULL AND r.status != 'pending'",
        check_name="request_attempts:incomplete",
    )
    return [
        f"incomplete attempt id={r['id']} request_id={r['request_id']} "
        f"status={r['status']}"
        for r in rows
    ]


async def _check_no_active_reservations_for_terminal(
    db: Database,
) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT rv.id, rv.request_id, r.status FROM reservations rv "
        "JOIN requests r ON r.id = rv.request_id "
        "WHERE rv.status = 'active' AND r.status != 'pending'",
        check_name="reservations:active_for_terminal",
    )
    return [
        f"active reservation id={r['id']} for terminal request "
        f"id={r['request_id']} status={r['status']}"
        for r in rows
    ]


async def _check_no_negative_values(db: Database) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT id, cost_microdollars, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, reasoning_tokens "
        "FROM requests WHERE "
        "cost_microdollars < 0 OR input_tokens < 0 OR output_tokens < 0 OR "
        "cache_read_tokens < 0 OR cache_write_tokens < 0 OR "
        "reasoning_tokens < 0",
        check_name="requests:negative_values",
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
    rows = await _fetch_all_checked(
        db,
        "SELECT proxy_request_id, COUNT(*) AS c FROM requests "
        "WHERE proxy_request_id IS NOT NULL "
        "GROUP BY proxy_request_id HAVING c > 1",
        check_name="requests:duplicate_proxy_request_id",
    )
    return [
        f"duplicate proxy_request_id={r['proxy_request_id']} count={r['c']}"
        for r in rows
    ]


async def _check_resolved_models_have_protocol(
    db: Database,
) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT model_id, protocol, resolution_status FROM models "
        "WHERE resolution_status = 'resolved' "
        "AND (protocol IS NULL OR protocol NOT IN ('openai', 'anthropic'))",
        check_name="models:resolved_protocol",
    )
    return [
        f"resolved model without valid protocol: id={r['model_id']} "
        f"protocol={r['protocol']} status={r['resolution_status']}"
        for r in rows
    ]


async def _check_price_snapshot_sources(db: Database) -> list[str]:
    rows = await _fetch_all_checked(
        db,
        "SELECT DISTINCT source FROM model_price_snapshots",
        check_name="model_price_snapshots:sources",
    )
    sources = {row["source"] for row in rows}
    allowed = {"config", "upstream", "mixed"}
    unknown = sources - allowed
    return [f"unknown price snapshot source(s): {sorted(unknown)}"] if unknown else []


def _read_db_path_from_config(config_path: str) -> str:
    """Read the database path from a TOML config file."""
    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        sys.stderr.write(f"Config file not found: {config_path}\n")
        sys.exit(2)
    except tomllib.TOMLDecodeError as exc:
        sys.stderr.write(f"Failed to parse config file: {exc}\n")
        sys.exit(2)
    db_section = config.get("database", {})
    path = db_section.get("path")
    if not path:
        sys.stderr.write(
            f"Config file {config_path!r} has no [database] section or 'path' key\n"
        )
        sys.exit(2)
    return path


def _db_path(config_path: str | None = None) -> str:
    if config_path:
        return _read_db_path_from_config(config_path)
    return os.environ.get("GOROUTER_DB_PATH", "./usage.sqlite3")


def _open_readonly_database(path: str) -> Database:
    """Construct a read-only :class:`Database` for the given path.

    Returning the unconnected Database (rather than awaiting connect)
    keeps the helper free of any I/O so it is safe to call from
    :func:`main` and from import-time smoke tests.
    """
    return Database(path=path, read_only=True)


async def _run_schema_preflight(db: Database) -> None:
    """Run the schema preflight in the documented order.

    Validates required tables/columns first, then the migration
    version. Either failure raises :class:`SchemaCompatibilityError`
    which :func:`main` translates to exit code 2.
    """
    await _validate_required_schema(db)
    await _check_schema_version(db)


async def _run_invariants(db: Database) -> list[str]:
    """Execute every invariant check and return the merged violation list."""
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
    return all_violations


async def main(config_path: str | None = None) -> int:
    db_path = _db_path(config_path)
    if not os.path.exists(db_path):
        sys.stderr.write(f"Database not found: {db_path}\n")
        return 2

    db = _open_readonly_database(db_path)
    try:
        try:
            await db.connect()
        except DatabaseError as exc:
            sys.stderr.write(f"Failed to open database read-only: {exc}\n")
            return 2

        try:
            await _run_schema_preflight(db)
        except SchemaCompatibilityError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2

        try:
            all_violations = await _run_invariants(db)
        except InvariantQueryError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    finally:
        await db.disconnect()

    if not all_violations:
        sys.stdout.write("Database invariants OK\n")
        return 0
    sys.stderr.write(f"Found {len(all_violations)} invariant violation(s):\n")
    for line in all_violations:
        sys.stderr.write(f"  - {line}\n")
    return 1


def main_sync() -> int:
    """Synchronous entry point for tests and operational scripts.

    This wrapper exists so the script can be invoked from synchronous
    test bodies and operational wrappers without each caller having to
    know the asyncio.run dance. It always raises a ``SystemExit`` only
    when invoked as ``__main__``; callers (tests, Click commands) get
    the exit code as the return value.
    """
    parser = argparse.ArgumentParser(
        prog="check_database.py",
        description="Read-only database invariant checker for GoRouter.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "Path to the TOML configuration file. "
            "When provided, the database path is read from the [database].path key. "
            "When not provided, the GOROUTER_DB_PATH environment variable is used "
            "(default: ./usage.sqlite3)."
        ),
    )
    args, _ = parser.parse_known_args()
    return asyncio.run(main(config_path=args.config))


if __name__ == "__main__":
    raise SystemExit(main_sync())
