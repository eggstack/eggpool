"""Tests for the synthetic cache stats query and API endpoint."""

from __future__ import annotations

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
    database = Database(path=str(tmp_path / "synthetic_cache_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_db(db: Database) -> Database:
    """Seed the database with accounts and synthetic cache requests."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("acct_a", "ENV_A", 1),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_x", "openai"),
        )

    async with db.transaction():
        # 3 disabled requests
        for _i in range(3):
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, input_tokens, output_tokens,
                    synthetic_cache_status, synthetic_cache_dry_run,
                    synthetic_cache_candidate_count, synthetic_cache_applied_count,
                    synthetic_cache_warning_count, synthetic_cache_warnings_json,
                    synthetic_cache_policy_name, synthetic_cache_policy_source,
                    synthetic_cache_summary_json
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?,
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed', 100, 200,
                    'disabled', 0,
                    0, 0,
                    0, '[]',
                    NULL, NULL,
                    NULL
                )
                """,
                ("acct_a", "model_x"),
            )
        # 2 dry_run requests
        for _i in range(2):
            dry_run_warnings = '["synthetic_cache_control_dry_run"]'
            dry_run_summary = '{"test": true}'
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, input_tokens, output_tokens,
                    synthetic_cache_status, synthetic_cache_dry_run,
                    synthetic_cache_candidate_count,
                    synthetic_cache_applied_count,
                    synthetic_cache_warning_count,
                    synthetic_cache_warnings_json,
                    synthetic_cache_policy_name,
                    synthetic_cache_policy_source,
                    synthetic_cache_summary_json
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?,
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed', 50, 75,
                    'dry_run', 1,
                    4, 0,
                    1, ?,
                    'anthropic-cache', 'policy:anthropic-cache',
                    ?
                )
                """,
                ("acct_a", "model_x", dry_run_warnings, dry_run_summary),
            )
        # 1 applied request
        warnings_json = (
            '["synthetic_cache_control_synthesized",'
            '"synthetic_cache_control_existing_native_preserved"]'
        )
        summary_json = '{"applied": true}'
        await db.execute_write(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, completed_at,
                status, input_tokens, output_tokens,
                synthetic_cache_status, synthetic_cache_dry_run,
                synthetic_cache_candidate_count, synthetic_cache_applied_count,
                synthetic_cache_warning_count, synthetic_cache_warnings_json,
                synthetic_cache_policy_name, synthetic_cache_policy_source,
                synthetic_cache_summary_json
            ) VALUES (
                (SELECT id FROM accounts WHERE name = ?),
                ?,
                datetime('now', '-10 minutes'),
                datetime('now', '-10 minutes'),
                'completed', 200, 300,
                'applied', 0,
                6, 3,
                2, ?,
                'anthropic-cache', 'policy:anthropic-cache',
                ?
            )
            """,
            ("acct_a", "model_x", warnings_json, summary_json),
        )
    return db


class TestFetchSyntheticCacheSummary:
    """fetch_synthetic_cache_summary returns correct aggregates."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_zeros(self, db: Database) -> None:
        """An empty database returns all-zero totals."""
        result = await queries.fetch_synthetic_cache_summary(
            db, "2000-01-01", "2099-01-01"
        )
        assert result["total_requests"] == 0
        assert result["status_counts"]["disabled"] == 0
        assert result["dry_run_count"] == 0
        assert result["applied_count"] == 0
        assert result["candidate_count_total"] == 0
        assert result["warning_count_total"] == 0
        assert result["by_policy"] == []
        assert result["by_status_timeseries"] is None

    @pytest.mark.asyncio()
    async def test_seeded_db_status_counts(self, seeded_db: Database) -> None:
        """Status counts match seeded data."""
        result = await queries.fetch_synthetic_cache_summary(
            seeded_db, "2000-01-01", "2099-01-01"
        )
        assert result["total_requests"] == 6
        assert result["status_counts"]["disabled"] == 3
        assert result["status_counts"]["dry_run"] == 2
        assert result["status_counts"]["applied"] == 1

    @pytest.mark.asyncio()
    async def test_seeded_db_totals(self, seeded_db: Database) -> None:
        """Aggregate totals match seeded data."""
        result = await queries.fetch_synthetic_cache_summary(
            seeded_db, "2000-01-01", "2099-01-01"
        )
        # dry_run_count: 2 requests have dry_run=1
        assert result["dry_run_count"] == 2
        # applied_count: 1 request has status=applied
        assert result["applied_count"] == 1
        # candidate_count_total: 0+0+0+4+4+6 = 14
        assert result["candidate_count_total"] == 14
        # applied_count_total: 0+0+0+0+0+3 = 3
        assert result["applied_count_total"] == 3
        # warning_count_total: 0+0+0+1+1+2 = 4
        assert result["warning_count_total"] == 4

    @pytest.mark.asyncio()
    async def test_seeded_db_warning_counts(self, seeded_db: Database) -> None:
        """Warning codes are aggregated correctly."""
        result = await queries.fetch_synthetic_cache_summary(
            seeded_db, "2000-01-01", "2099-01-01"
        )
        wc = result["warning_counts"]
        assert wc.get("synthetic_cache_control_dry_run") == 2
        assert wc.get("synthetic_cache_control_synthesized") == 1
        assert wc.get("synthetic_cache_control_existing_native_preserved") == 1

    @pytest.mark.asyncio()
    async def test_seeded_db_by_policy(self, seeded_db: Database) -> None:
        """By-policy rollup groups correctly."""
        result = await queries.fetch_synthetic_cache_summary(
            seeded_db, "2000-01-01", "2099-01-01"
        )
        by_policy = result["by_policy"]
        assert len(by_policy) == 2
        names = {p["policy_name"] for p in by_policy}
        assert "<global>" in names
        assert "anthropic-cache" in names
        global_pol = next(p for p in by_policy if p["policy_name"] == "<global>")
        assert global_pol["request_count"] == 3
        assert global_pol["applied_count"] == 0
        anthropic_pol = next(
            p for p in by_policy if p["policy_name"] == "anthropic-cache"
        )
        assert anthropic_pol["request_count"] == 3
        assert anthropic_pol["applied_count"] == 1

    @pytest.mark.asyncio()
    async def test_window_filter(self, seeded_db: Database) -> None:
        """Narrowing the time window excludes older rows."""
        result = await queries.fetch_synthetic_cache_summary(
            seeded_db, "2099-01-01", "2099-12-31"
        )
        assert result["total_requests"] == 0


class TestSyntheticCacheSummaryService:
    """StatsService.get_synthetic_cache_summary delegates correctly."""

    @pytest.mark.asyncio()
    async def test_service_delegates(self, seeded_db: Database) -> None:
        """The service method returns the same shape as the query function."""
        from eggpool.stats.service import StatsService

        service = StatsService(seeded_db)
        result = await service.get_synthetic_cache_summary("30d")
        assert "total_requests" in result
        assert "status_counts" in result
        assert "by_policy" in result
        assert isinstance(result["by_policy"], list)
