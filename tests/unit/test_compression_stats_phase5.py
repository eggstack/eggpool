"""Tests for Phase 5 compression stats fields.

Phase 5 adds ``compression_applied`` and related columns to the
``requests`` table via migration 0043.  The current
``fetch_compression_observability`` query handles the new
``by_status`` entries via forward-compatible dict lookups.
These tests verify:

- Schema columns exist after migration 0043.
- Rows with ``compression_applied = 1`` are visible in the query.
- Existing Phase 4 fields remain intact.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.stats import queries

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "stats_phase5_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaColumns:
    """Migration 0043 columns must be present."""

    def test_migration_0043_columns_present(
        self, db: Database, tmp_path: pytest.TempPathFactory
    ) -> None:
        """After all migrations the 0043 columns exist on requests."""
        # Use a raw SQLite connection to inspect schema
        conn = sqlite3.connect(str(tmp_path / "schema_check.sqlite3"))
        try:
            # Re-run migration via the same path so we get the real schema
            from eggpool.db.connection import Database as DbConn
            from eggpool.db.migrations import MigrationRunner as Runner

            async def _setup() -> None:
                db2 = DbConn(path=str(tmp_path / "schema_check.sqlite3"))
                await db2.connect()
                r = Runner(db2)
                await r.run()
                await db2.disconnect()

            import asyncio

            asyncio.run(_setup())

            rows = conn.execute("PRAGMA table_info(requests)").fetchall()
            columns = {row[1] for row in rows}
        finally:
            conn.close()

        expected = {
            "compression_applied",
            "compression_transform_count",
            "compression_transforms_by_reason_json",
            "compression_original_tokens",
            "compression_compressed_tokens",
            "compression_savings_tokens",
            "compression_pre_stable_prefix_hash",
            "compression_post_stable_prefix_hash",
            "compression_stable_prefix_preserved",
            "compression_warnings_json",
            "compression_latency_ms",
            "compression_failed_fallback",
            "compression_applied_summary_json",
        }
        missing = expected - columns
        assert not missing, f"Missing 0043 columns: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Applied-mode query handling
# ---------------------------------------------------------------------------


class TestAppliedModeStats:
    """Test that fetch_compression_observability handles applied-mode rows."""

    @pytest.mark.asyncio()
    async def test_applied_status_visible_in_by_status(self, db: Database) -> None:
        """Rows with compression_status='applied' appear in by_status."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_applied", "ENV_APPLIED", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_applied", "openai"),
            )
            # Insert a request with compression_status='applied' and
            # compression_applied=1
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_applied,
                    compression_transform_count,
                    compression_savings_tokens,
                    compression_stable_prefix_preserved,
                    compression_failed_fallback,
                    compression_latency_ms
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'applied', 1, 3, 500, 1, 0, 12.5
                )
                """,
                ("acct_applied", "model_applied"),
            )
            # Insert a second request with compression_status='disabled'
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_applied
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-45 minutes'),
                    datetime('now', '-45 minutes'),
                    'completed',
                    'disabled', 0
                )
                """,
                ("acct_applied", "model_applied"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )

        # The 'applied' status must appear in by_status
        assert result["by_status"].get("applied", 0) == 1
        assert result["by_status"].get("disabled", 0) == 1
        assert result["total_requests"] == 2

    @pytest.mark.asyncio()
    async def test_applied_mode_visible_in_by_mode(self, db: Database) -> None:
        """Rows with compression_mode='safe' and status='observed' appear in by_mode."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_mode", "ENV_MODE", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_mode", "openai"),
            )
            # by_mode only queries compression_status='observed' rows,
            # so insert with that status but mode='safe'
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode, compression_applied
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'observed', 'safe', 1
                )
                """,
                ("acct_mode", "model_mode"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["by_mode"].get("safe", 0) == 1

    @pytest.mark.asyncio()
    async def test_phase4_fields_still_present(self, db: Database) -> None:
        """Existing Phase 4 observe-mode fields work with Phase 5 rows."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_compat", "ENV_COMPAT", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_compat", "openai"),
            )
            # Insert an observed request (Phase 4 style)
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode,
                    compression_candidate_count,
                    compression_eligible_candidate_count,
                    compression_estimated_savings_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'observed', 'observe', 5, 3, 1200
                )
                """,
                ("acct_compat", "model_compat"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )

        # Phase 4 fields must still work
        totals = result["totals"]
        assert totals["candidate_count"] == 5
        assert totals["eligible_count"] == 3
        assert totals["estimated_savings_tokens"] == 1200
        assert result["by_status"].get("observed", 0) == 1
        assert result["by_mode"].get("observe", 0) == 1

    @pytest.mark.asyncio()
    async def test_top_reason_codes_aggregated(self, db: Database) -> None:
        """Phase 4 reason code JSON is aggregated into top_reason_codes."""
        reason_json = json.dumps(
            {"repeated_line_run": 10, "log_compaction": 5, "base64_elision": 2}
        )
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_reasons", "ENV_REASONS", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_reasons", "openai"),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode,
                    compression_reason_code_counts_json
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'observed', 'observe', ?
                )
                """,
                ("acct_reasons", "model_reasons", reason_json),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        top = dict(result["top_reason_codes"])
        assert top.get("repeated_line_run", 0) == 10
        assert top.get("log_compaction", 0) == 5
        assert top.get("base64_elision", 0) == 2
