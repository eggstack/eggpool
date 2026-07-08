"""Tests for the dashboard EXPLAIN diagnostic and index coverage."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.stats.dashboard_explain import (
    ExplainResult,
    explain_dashboard_queries,
)


async def _create_migrated_db(path: str = ":memory:") -> Database:
    """Open an in-memory database and run all migrations."""
    db = Database(path=path)
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    return db


async def _seed_requests(db: Database, count: int = 20) -> None:
    """Insert representative request rows for EXPLAIN analysis."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("claude-3", "anthropic"),
        )
        for i in range(count):
            model = "gpt-4" if i % 2 == 0 else "claude-3"
            status = "completed" if i % 5 != 0 else "error"
            provider = "openai" if i % 2 == 0 else "anthropic"
            streamed = 1 if i % 3 == 0 else 0
            first_byte_ms = 150 if streamed else None
            await db.execute_write(
                "INSERT INTO requests "
                "(account_id, model_id, provider_id, started_at, status, "
                " input_tokens, output_tokens, cost_microdollars, "
                " upstream_latency_ms, streamed, first_byte_ms, "
                " bytes_received, bytes_emitted, "
                " cache_read_tokens, cache_write_tokens, reasoning_tokens) "
                "VALUES (?, ?, ?, datetime('now', ?), ?, ?, ?, ?, ?, ?, "
                "        ?, ?, ?, ?, ?, ?)",
                (
                    1,
                    model,
                    provider,
                    f"-{count - i} hours",
                    status,
                    100 + i,
                    200 + i,
                    50 + i,
                    100.0 + i,
                    streamed,
                    first_byte_ms,
                    1000 + i,
                    500 + i,
                    0,
                    0,
                    0,
                ),
            )


class TestExplainDashboardQueries:
    """Test the explain_dashboard_queries helper function."""

    @pytest.mark.asyncio
    async def test_returns_all_query_names(self) -> None:
        db = await _create_migrated_db()
        try:
            await _seed_requests(db)
            result = await explain_dashboard_queries(db, period="24h")
            names = [q.name for q in result.queries]
            assert names == [
                "fetch_timeseries",
                "fetch_grouped_timeseries",
                "fetch_summary",
                "fetch_account_stats",
                "fetch_model_stats",
                "fetch_bandwidth_timeseries",
            ]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_each_entry_has_plan_lines(self) -> None:
        db = await _create_migrated_db()
        try:
            await _seed_requests(db)
            result = await explain_dashboard_queries(db, period="24h")
            for q in result.queries:
                assert len(q.plan_lines) > 0, f"{q.name} has no plan lines"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_each_entry_has_positive_elapsed(self) -> None:
        db = await _create_migrated_db()
        try:
            await _seed_requests(db)
            result = await explain_dashboard_queries(db, period="24h")
            for q in result.queries:
                assert q.elapsed_ms >= 0.0, f"{q.name} has negative elapsed"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_result_carries_period_bucket_group_by(self) -> None:
        db = await _create_migrated_db()
        try:
            await _seed_requests(db)
            result = await explain_dashboard_queries(
                db, period="7d", bucket="day", group_by="provider"
            )
            assert result.period == "7d"
            assert result.bucket == "day"
            assert result.group_by == "provider"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_different_group_by_variants(self) -> None:
        db = await _create_migrated_db()
        try:
            await _seed_requests(db)
            for gby in ("provider", "model", "provider_model", "account"):
                result = await explain_dashboard_queries(db, period="1h", group_by=gby)
                grouped = next(
                    q for q in result.queries if q.name == "fetch_grouped_timeseries"
                )
                assert len(grouped.plan_lines) > 0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_empty_database_returns_plans(self) -> None:
        db = await _create_migrated_db()
        try:
            result = await explain_dashboard_queries(db, period="24h")
            assert len(result.queries) == 6
            for q in result.queries:
                assert len(q.plan_lines) > 0
        finally:
            await db.disconnect()


class TestExplainResultDataclass:
    def test_default_queries_list(self) -> None:
        r = ExplainResult(period="24h", bucket="hour", group_by="provider_model")
        assert r.queries == []


class TestExplainDashboardCliSmoke:
    def test_text_output(self, tmp_path: Any) -> None:
        from eggpool.cli import cli

        runner = CliRunner()
        db_path = str(tmp_path / "explain_test.sqlite3")

        async def _seed() -> None:
            db = Database(path=db_path)
            await db.connect()
            try:
                await MigrationRunner(db).run()
                await _seed_requests(db)
            finally:
                await db.disconnect()

        asyncio.run(_seed())

        with (
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
            patch("eggpool.config.ensure_config"),
            patch("eggpool.cli_full.AppConfig.from_toml") as mock_config,
        ):
            mock_config_obj = MagicMock()
            mock_config_obj.database.path = db_path
            mock_config_obj.database.busy_timeout_ms = 5000
            mock_config_obj.database.wal = True
            mock_config_obj.database.synchronous = "NORMAL"
            mock_config.return_value = mock_config_obj

            result = runner.invoke(cli, ["stats", "explain-dashboard"])

        assert result.exit_code == 0, result.output
        assert "Period: 24h" in result.output
        assert "fetch_timeseries" in result.output
        assert "fetch_summary" in result.output

    def test_json_output(self, tmp_path: Any) -> None:
        from eggpool.cli import cli

        runner = CliRunner()
        db_path = str(tmp_path / "explain_json_test.sqlite3")

        async def _seed() -> None:
            db = Database(path=db_path)
            await db.connect()
            try:
                await MigrationRunner(db).run()
                await _seed_requests(db)
            finally:
                await db.disconnect()

        asyncio.run(_seed())

        with (
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
            patch("eggpool.config.ensure_config"),
            patch("eggpool.cli_full.AppConfig.from_toml") as mock_config,
        ):
            mock_config_obj = MagicMock()
            mock_config_obj.database.path = db_path
            mock_config_obj.database.busy_timeout_ms = 5000
            mock_config_obj.database.wal = True
            mock_config_obj.database.synchronous = "NORMAL"
            mock_config.return_value = mock_config_obj

            result = runner.invoke(cli, ["stats", "explain-dashboard", "--json"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert "queries" in parsed
        assert len(parsed["queries"]) == 6
        assert parsed["period"] == "24h"
