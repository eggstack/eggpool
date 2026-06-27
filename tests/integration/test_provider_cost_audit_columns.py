"""Integration tests for the Phase 1 provider-reported cost audit columns.

Verifies that migration 0033 adds the four nullable audit columns to
``requests`` and that ``RequestRepository.finalize_if_pending`` (and
its sibling completion methods) persist them correctly.  Also
confirms that the existing finalizer behaviour is unchanged when the
new fields default to ``None``.
"""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository

AUDIT_COLUMN_NAMES = (
    "provider_cost_microdollars",
    "provider_cost_source",
    "local_cost_microdollars",
    "local_cost_exactness",
)


async def _fresh_db() -> Database:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    return db


async def _seed_minimum(db: Database) -> int:
    """Insert a single account and a single model so the requests FK
    columns resolve.  Returns the account id."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("audit-acct", "AUDIT_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, 'openai')",
            ("audit-model",),
        )
    account_row = await db.fetch_one(
        "SELECT id FROM accounts WHERE name = ?",
        ("audit-acct",),
    )
    assert account_row is not None
    return int(account_row["id"])


def _column_set(rows: list[dict[str, Any]]) -> set[str]:
    return {row["name"] for row in rows}


class TestMigration0033:
    """Migration 0033 must add the four audit columns to ``requests``."""

    @pytest.mark.asyncio
    async def test_requests_table_has_audit_columns(self) -> None:
        db = await _fresh_db()
        try:
            rows = await db.fetch_all("PRAGMA table_info(requests)")
            names = _column_set(rows)
            for column in AUDIT_COLUMN_NAMES:
                assert column in names, (
                    f"Column {column!r} missing from requests table; "
                    f"have: {sorted(names)}"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_audit_columns_are_nullable(self) -> None:
        """The four new columns must allow NULL values (legacy rows)."""
        db = await _fresh_db()
        try:
            rows = await db.fetch_all("PRAGMA table_info(requests)")
            by_name = {row["name"]: row for row in rows}
            for column in AUDIT_COLUMN_NAMES:
                info = by_name[column]
                assert int(info["notnull"]) == 0, (
                    f"Column {column!r} must be nullable; notnull={info['notnull']}"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_audit_columns_default_to_null(self) -> None:
        """A pending request inserted without the new fields must
        have ``None`` for each audit column."""
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-pending-1",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id,
                )
            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            for column in AUDIT_COLUMN_NAMES:
                assert row[column] is None, (
                    f"Column {column!r} must default to NULL on insert; "
                    f"got {row[column]!r}"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_migration_runner_is_idempotent_after_0033(self) -> None:
        """Running the migration runner twice must not fail on the
        already-applied 0033 (the runner's version tracking guards
        against re-execution)."""
        db = await _fresh_db()
        try:
            runner = MigrationRunner(db)
            applied_before = await runner._applied_versions()  # noqa: SLF001
            assert 33 in applied_before, "0033 must be applied on a fresh DB"
            await runner.run()
            applied_after = await runner._applied_versions()  # noqa: SLF001
            assert applied_before == applied_after
        finally:
            await db.disconnect()


class TestFinalizeIfPending:
    """``finalize_if_pending`` must accept and persist the four audit
    columns and remain backwards-compatible when they are omitted."""

    @pytest.mark.asyncio
    async def test_finalize_persists_audit_fields_when_provided(self) -> None:
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-finalize-1",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id,
                )

                updated = await request_repo.finalize_if_pending(
                    request_id,
                    status="success",
                    input_tokens=10,
                    output_tokens=20,
                    cost_microdollars=300,
                    exactness="derived",
                    provider_cost_microdollars=12_000_000,
                    provider_cost_source="opencode_go:usage.cost_usd",
                    local_cost_microdollars=15_000_000,
                    local_cost_exactness="derived",
                )
            assert updated is True

            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness, "
                "cost_microdollars, exactness, status "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert row["status"] == "success"
            assert int(row["cost_microdollars"]) == 300
            assert row["exactness"] == "derived"
            assert int(row["provider_cost_microdollars"]) == 12_000_000
            assert row["provider_cost_source"] == "opencode_go:usage.cost_usd"
            assert int(row["local_cost_microdollars"]) == 15_000_000
            assert row["local_cost_exactness"] == "derived"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_finalize_with_no_audit_fields_defaults_to_null(self) -> None:
        """Calling ``finalize_if_pending`` without the audit fields must
        continue to work and leave the new columns NULL."""
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-finalize-2",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id,
                )

                updated = await request_repo.finalize_if_pending(
                    request_id,
                    status="success",
                    input_tokens=10,
                    output_tokens=20,
                    cost_microdollars=300,
                    exactness="derived",
                )
            assert updated is True

            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            for column in AUDIT_COLUMN_NAMES:
                assert row[column] is None, (
                    f"Column {column!r} must remain NULL when the caller "
                    f"does not supply it; got {row[column]!r}"
                )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_update_after_completion_persists_audit_fields(self) -> None:
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-update-after-1",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id,
                )
                await request_repo.update_after_completion(
                    request_id,
                    status="success",
                    input_tokens=10,
                    output_tokens=20,
                    cost_microdollars=300,
                    provider_cost_microdollars=5_000_000,
                    provider_cost_source="openai_compatible:usage.cost_usd",
                    local_cost_microdollars=8_000_000,
                    local_cost_exactness="partial",
                )
            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["provider_cost_microdollars"]) == 5_000_000
            assert row["provider_cost_source"] == "openai_compatible:usage.cost_usd"
            assert int(row["local_cost_microdollars"]) == 8_000_000
            assert row["local_cost_exactness"] == "partial"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_update_streaming_final_persists_audit_fields(self) -> None:
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-streaming-1",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=True,
                    account_id=account_id,
                )
                await request_repo.update_streaming_final(
                    request_id,
                    status="success",
                    input_tokens=10,
                    output_tokens=20,
                    cost_microdollars=300,
                    provider_cost_microdollars=7_000_000,
                    provider_cost_source="opencode_go:usage.cost_usd",
                    local_cost_microdollars=9_000_000,
                    local_cost_exactness="estimated",
                )
            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["provider_cost_microdollars"]) == 7_000_000
            assert row["provider_cost_source"] == "opencode_go:usage.cost_usd"
            assert int(row["local_cost_microdollars"]) == 9_000_000
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_finalize_on_already_terminal_request_is_idempotent(self) -> None:
        """Calling ``finalize_if_pending`` twice must leave the audit
        fields at their first-write values (the WHERE clause still
        filters on ``status = 'pending'``)."""
        db = await _fresh_db()
        try:
            account_id = await _seed_minimum(db)
            request_repo = RequestRepository(db)
            async with db.transaction():
                request_id = await request_repo.create_pending(
                    request_id="audit-idempotent-1",
                    model_id="audit-model",
                    protocol="openai",
                    streamed=False,
                    account_id=account_id,
                )

                first = await request_repo.finalize_if_pending(
                    request_id,
                    status="success",
                    provider_cost_microdollars=1_000_000,
                    provider_cost_source="opencode_go:usage.cost_usd",
                    local_cost_microdollars=2_000_000,
                    local_cost_exactness="derived",
                )
            assert first is True

            async with db.transaction():
                second = await request_repo.finalize_if_pending(
                    request_id,
                    status="success",
                    provider_cost_microdollars=99_000_000,
                    provider_cost_source="should-not-stick",
                    local_cost_microdollars=99_000_000,
                    local_cost_exactness="estimated",
                )
            assert second is False

            row = await db.fetch_one(
                "SELECT provider_cost_microdollars, provider_cost_source, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["provider_cost_microdollars"]) == 1_000_000
            assert row["provider_cost_source"] == "opencode_go:usage.cost_usd"
            assert int(row["local_cost_microdollars"]) == 2_000_000
            assert row["local_cost_exactness"] == "derived"
        finally:
            await db.disconnect()
