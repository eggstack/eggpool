"""Routing invariant validator for EggPool.

Read-only diagnostic script that validates routing-decision data
against the acceptance criteria in the account-skew corrective pass
plan.  Returns non-zero exit code on any invariant violation.

The validator opens the database in read-only mode and never
mutates state.

Checks performed:
    1. Migration 0035 applied (score_components_json column exists).
    2. Every routing_decisions row has valid JSON in
       score_components_json.
    3. No single account receives more than 80% of selections when
       multiple accounts are eligible (skew check, advisory only).
    4. Score components for recent decisions contain the required
       keys.
    5. top_candidates entries have the expected shape.

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
import contextlib
import json
import os
import sys
from typing import TYPE_CHECKING, Any, cast

from eggpool.db.connection import Database
from eggpool.errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import Sequence


REQUIRED_SCORE_COMPONENT_KEYS = frozenset(
    {
        "account_name",
        "quota_score",
        "inflight_penalty",
        "health_penalty",
        "final_score",
        "weight",
        "active_request_count",
        "reserved_microdollars",
        "cost_5h_microdollars",
        "cost_7d_microdollars",
        "cost_30d_microdollars",
        "capacity_5h_microdollars",
        "capacity_7d_microdollars",
        "capacity_30d_microdollars",
        "tier",
        "requires_transcode",
        "top_candidates",
    }
)

SKEW_WARNING_THRESHOLD = 0.80


class ValidationError(Exception):
    """Raised when a routing invariant is violated."""


async def _check_migration(db: Database) -> None:
    """Verify migration 0035 was applied."""
    try:
        columns = await db.fetch_all("PRAGMA table_info(routing_decisions)")
    except DatabaseError as exc:
        raise ValidationError(f"Cannot read routing_decisions table: {exc}") from exc
    if not columns:
        raise ValidationError("routing_decisions table does not exist")
    col_names = {dict(c).get("name") for c in columns}
    if "score_components_json" not in col_names:
        raise ValidationError(
            "Migration 0035 not applied: score_components_json "
            "column missing from routing_decisions"
        )


async def _check_score_components_json(db: Database) -> int:
    """Verify score_components_json is valid JSON on recent rows.

    Returns the number of rows checked.
    """
    rows = await db.fetch_all(
        """
        SELECT id, score_components_json
        FROM routing_decisions
        ORDER BY id DESC
        LIMIT 100
        """
    )
    bad: list[int] = []
    for row in rows:
        row_dict = dict(row)
        raw = row_dict.get("score_components_json")
        if raw is None:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            bad.append(int(row_dict["id"]))
            continue
        if not isinstance(parsed, dict):
            bad.append(int(row_dict["id"]))

    if bad:
        raise ValidationError(
            f"{len(bad)} routing_decisions row(s) have invalid "
            f"score_components_json: ids {bad[:10]}"
        )
    return len(rows)


async def _check_score_component_keys(db: Database) -> int:
    """Verify score components have required keys.

    Returns the number of rows checked.
    """
    rows = await db.fetch_all(
        """
        SELECT id, selected_account_name, score_components_json
        FROM routing_decisions
        WHERE score_components_json IS NOT NULL
          AND score_components_json != '{}'
        ORDER BY id DESC
        LIMIT 50
        """
    )
    bad: list[tuple[int, list[str]]] = []
    for row in rows:
        row_dict = dict(row)
        raw = row_dict.get("score_components_json", "{}")
        try:
            parsed_raw: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed_raw, dict):
            continue
        parsed_dict = cast("dict[str, Any]", parsed_raw)
        parsed_keys = set(parsed_dict.keys())
        missing = sorted(REQUIRED_SCORE_COMPONENT_KEYS - parsed_keys)
        if missing:
            bad.append((int(row_dict["id"]), missing))

    if bad:
        sample = bad[:5]
        details = "; ".join(f"id={rid}: missing {mk}" for rid, mk in sample)
        raise ValidationError(
            f"{len(bad)} routing_decisions row(s) have incomplete "
            f"score components: {details}"
        )
    return len(rows)


async def _check_selection_skew(db: Database) -> None:
    """Advisory skew check: warn if one account dominates."""
    rows = await db.fetch_all(
        """
        SELECT
            selected_account_name,
            COUNT(*) as cnt
        FROM routing_decisions
        WHERE selected_account_name IS NOT NULL
        GROUP BY selected_account_name
        ORDER BY cnt DESC
        """
    )
    if len(rows) < 2:
        return
    total = sum(dict(r).get("cnt", 0) for r in rows)
    if total < 20:
        return
    top = dict(rows[0])
    top_name = top.get("selected_account_name", "?")
    top_count = int(top.get("cnt", 0))
    fraction = top_count / total if total > 0 else 0.0
    if fraction > SKEW_WARNING_THRESHOLD:
        print(
            f"WARNING: Account {top_name!r} received "
            f"{fraction:.0%} of selections "
            f"({top_count}/{total}). "
            f"This may indicate residual routing skew.",
            file=sys.stderr,
        )


async def _check_top_candidates(db: Database) -> None:
    """Verify top_candidates has expected shape."""
    rows = await db.fetch_all(
        """
        SELECT id, score_components_json
        FROM routing_decisions
        WHERE score_components_json IS NOT NULL
          AND score_components_json != '{}'
        ORDER BY id DESC
        LIMIT 50
        """
    )
    bad: list[int] = []
    for row in rows:
        row_dict = dict(row)
        raw = row_dict.get("score_components_json", "{}")
        try:
            parsed_raw: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed_raw, dict):
            continue
        parsed_dict = cast("dict[str, Any]", parsed_raw)
        top_raw = parsed_dict.get("top_candidates")
        if not isinstance(top_raw, list):
            bad.append(int(row_dict["id"]))
            continue
        top = cast("list[dict[str, Any]]", top_raw)
        for entry in top:
            if "account_name" not in entry or "final_score" not in entry:
                bad.append(int(row_dict["id"]))
                break

    if bad:
        raise ValidationError(
            f"{len(bad)} routing_decisions row(s) have malformed "
            f"top_candidates in score_components_json"
        )


async def run_validation(db_path: str) -> int:
    """Run all routing invariant checks.  Returns exit code."""
    errors: list[str] = []
    db = Database(path=db_path, read_only=True)
    try:
        await db.connect()
    except DatabaseError as exc:
        sys.stderr.write(f"Failed to open database: {exc}\n")
        return 2

    try:
        await _check_migration(db)
        print("  [OK] Migration 0035 applied (score_components_json exists)")

        rows_checked = await _check_score_components_json(db)
        print(
            f"  [OK] score_components_json is valid JSON on {rows_checked} recent rows"
        )

        keys_checked = await _check_score_component_keys(db)
        print(f"  [OK] Score components contain required keys on {keys_checked} rows")

        await _check_top_candidates(db)
        print("  [OK] top_candidates shape is valid on recent rows")

        await _check_selection_skew(db)

    except ValidationError as exc:
        errors.append(str(exc))
    except DatabaseError as exc:
        sys.stderr.write(f"ERROR: Database query failed: {exc}\n")
        return 2
    finally:
        with contextlib.suppress(Exception):
            await db.disconnect()

    if errors:
        for err in errors:
            sys.stderr.write(f"FAIL: {err}\n")
        return 1

    print("\nAll routing invariants pass.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the routing validator."""
    parser = argparse.ArgumentParser(
        description=("Validate routing-decision invariants in an EggPool database."),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("GOROUTER_DB_PATH", "./usage.sqlite3"),
        help=(
            "Path to the SQLite database "
            "(default: $GOROUTER_DB_PATH or ./usage.sqlite3)"
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_validation(args.db))


if __name__ == "__main__":
    sys.exit(main())
