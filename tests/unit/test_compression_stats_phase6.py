"""Tests for Phase 6 compression stats fields.

Phase 6 adds ``compression_policy_name``, ``compression_policy_source``,
and ``compression_policy_warnings_json`` columns to the ``requests``
table via migration 0044.  The ``fetch_compression_observability``
query aggregates per-policy rollups into ``by_policy``,
``by_policy_source``, and ``policy_warning_count_total``.

These tests verify:

- New Phase 6 fields are present in the query result (even when empty).
- Rows with policy metadata appear in ``by_policy`` and ``by_policy_source``.
- ``policy_warning_count_total`` sums ``compression_warning_count`` per policy.
- Existing Phase 4/5 fields remain intact (regression check).
"""

from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.stats import queries


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> Database:
    database = Database(path=str(tmp_path / "stats_phase6_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


class TestSchemaColumns:
    """Migration 0044 columns must be present."""

    def test_migration_0044_columns_present(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """After all migrations the 0044 columns exist on requests."""

        async def _setup() -> None:
            db2 = Database(path=str(tmp_path / "schema_check_0044.sqlite3"))
            await db2.connect()
            r = MigrationRunner(db2)
            await r.run()
            await db2.disconnect()

        import asyncio

        asyncio.run(_setup())

        conn = sqlite3.connect(str(tmp_path / "schema_check_0044.sqlite3"))
        try:
            rows = conn.execute("PRAGMA table_info(requests)").fetchall()
            columns = {row[1] for row in rows}
        finally:
            conn.close()

        expected = {
            "compression_policy_name",
            "compression_policy_source",
            "compression_policy_warnings_json",
        }
        missing = expected - columns
        assert not missing, f"Missing 0044 columns: {sorted(missing)}"


class TestPhase6PolicyStats:
    """Phase 6 policy rollup fields in fetch_compression_observability."""

    @pytest.mark.asyncio()
    async def test_empty_db_has_phase6_fields(self, db: Database) -> None:
        """All new Phase 6 keys are present but empty/zero."""
        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert "by_policy" in result
        assert "by_policy_source" in result
        assert "policy_warning_count_total" in result
        assert result["by_policy"] == {}
        assert result["by_policy_source"] == {}
        assert result["policy_warning_count_total"] == 0

    @pytest.mark.asyncio()
    async def test_by_policy_request_counts(self, db: Database) -> None:
        """Requests with different policy names appear in by_policy."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_policy1", "ENV_POLICY1", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_policy1", "openai"),
            )
            # Request with global policy (NULL name/source)
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-1 hour'), datetime('now', '-1 hour'),
                    'completed'
                )""",
                ("acct_policy1", "model_policy1"),
            )
            # Request with override policy
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_policy_name, compression_policy_source
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    'my_policy', 'policy:my_policy'
                )""",
                ("acct_policy1", "model_policy1"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert "<global>" in result["by_policy"]
        assert "my_policy" in result["by_policy"]
        assert result["by_policy"]["<global>"]["total_requests"] == 1
        assert result["by_policy"]["my_policy"]["total_requests"] == 1

    @pytest.mark.asyncio()
    async def test_by_policy_source_counts(self, db: Database) -> None:
        """Requests with different policy sources appear in by_policy_source."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_source1", "ENV_SOURCE1", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_source1", "openai"),
            )
            # Global source
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-1 hour'), datetime('now', '-1 hour'),
                    'completed'
                )""",
                ("acct_source1", "model_source1"),
            )
            # Policy source
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_policy_source
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    'policy:my_policy'
                )""",
                ("acct_source1", "model_source1"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["by_policy_source"].get("global", 0) == 1
        assert result["by_policy_source"].get("policy:my_policy", 0) == 1

    @pytest.mark.asyncio()
    async def test_policy_warning_count_total(self, db: Database) -> None:
        """policy_warning_count_total sums compression_warning_count."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_warn1", "ENV_WARN1", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_warn1", "openai"),
            )
            # Row with 3 warnings
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_warning_count
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-1 hour'), datetime('now', '-1 hour'),
                    'completed',
                    3
                )""",
                ("acct_warn1", "model_warn1"),
            )
            # Row with 1 warning
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_warning_count
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    1
                )""",
                ("acct_warn1", "model_warn1"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["policy_warning_count_total"] == 4

    @pytest.mark.asyncio()
    async def test_phase4_fields_still_present(self, db: Database) -> None:
        """Existing Phase 4 observe-mode fields remain intact."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_phase4", "ENV_PHASE4", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_phase4", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
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
                )""",
                ("acct_phase4", "model_phase4"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        totals = result["totals"]
        assert totals["candidate_count"] == 5
        assert totals["eligible_count"] == 3
        assert totals["estimated_savings_tokens"] == 1200
        assert result["by_status"].get("observed", 0) == 1
        assert result["by_mode"].get("observe", 0) == 1

    @pytest.mark.asyncio()
    async def test_phase5_fields_still_present(self, db: Database) -> None:
        """Existing Phase 5 applied-mode fields remain intact."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_phase5", "ENV_PHASE5", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_phase5", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
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
                )""",
                ("acct_phase5", "model_phase5"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["by_status"].get("applied", 0) == 1
        assert result["requests_with_compression_applied"] >= 1

    @pytest.mark.asyncio()
    async def test_mix_global_and_override_requests(self, db: Database) -> None:
        """Both global and override keys present in by_policy."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_mix", "ENV_MIX", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_mix", "openai"),
            )
            # Two global requests
            for _ in range(2):
                await db.execute_write(
                    """INSERT INTO requests (
                        account_id, model_id, provider_id, upstream_protocol,
                        started_at, completed_at, status
                    ) VALUES (
                        (SELECT id FROM accounts WHERE name = ?),
                        (SELECT model_id FROM models WHERE model_id = ?),
                        'provider1', 'openai',
                        datetime('now', '-1 hour'),
                        datetime('now', '-1 hour'),
                        'completed'
                    )""",
                    ("acct_mix", "model_mix"),
                )
            # One override request
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_policy_name, compression_policy_source
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'provider1', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    'custom', 'policy:custom'
                )""",
                ("acct_mix", "model_mix"),
            )

        result = await queries.fetch_compression_observability(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["by_policy"]["<global>"]["total_requests"] == 2
        assert result["by_policy"]["custom"]["total_requests"] == 1
        assert result["by_policy_source"].get("global", 0) == 2
        assert result["by_policy_source"].get("policy:custom", 0) == 1
