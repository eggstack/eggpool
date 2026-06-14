"""Tests for the statistics query layer and service."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.stats import queries
from go_aggregator.stats.service import (
    PERIOD_PRESETS,
    StatsService,
    TimeRange,
    resolve_period,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "stats_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_db(db: Database) -> Database:
    """Seed the database with two accounts, two models, and several requests."""
    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
        ("acct_a", "ENV_A", 1),
    )
    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
        ("acct_b", "ENV_B", 1),
    )
    await db.execute(
        "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
        ("model_x", "openai"),
    )
    await db.execute(
        "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
        ("model_y", "anthropic"),
    )
    await db.connection.commit()
    # Insert some completed requests
    for _i in range(3):
        await db.execute(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, completed_at,
                status, input_tokens, output_tokens, cost_microdollars,
                upstream_latency_ms
            ) VALUES (
                (SELECT id FROM accounts WHERE name = ?),
                ?,
                datetime('now', '-1 hour'),
                datetime('now', '-1 hour'),
                'completed', ?, ?, ?, ?
            )
            """,
            ("acct_a", "model_x", 100, 200, 1_000_000, 150.0),
        )
    for _i in range(2):
        await db.execute(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, completed_at,
                status, input_tokens, output_tokens, cost_microdollars,
                upstream_latency_ms, error_class, error_detail
            ) VALUES (
                (SELECT id FROM accounts WHERE name = ?),
                ?,
                datetime('now', '-2 hours'),
                datetime('now', '-2 hours'),
                'error', ?, ?, 0, ?, ?, ?
            )
            """,
            ("acct_b", "model_y", 50, 75, 300.0, "RateLimitError", "rate_limited"),
        )
    await db.connection.commit()
    return db


class TestFetchSummary:
    """Tests for fetch_summary."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_zeros(self, db: Database) -> None:
        result = await queries.fetch_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_requests"] == 0
        assert result["error_rate"] == 0.0
        assert result["total_cost_microdollars"] == 0

    @pytest.mark.asyncio()
    async def test_summary_aggregates(self, seeded_db: Database) -> None:
        result = await queries.fetch_summary(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_requests"] == 5
        assert result["successful_requests"] == 3
        assert result["error_requests"] == 2
        assert result["total_input_tokens"] == 3 * 100 + 2 * 50
        assert result["total_output_tokens"] == 3 * 200 + 2 * 75
        assert result["total_cost_microdollars"] == 3 * 1_000_000
        assert result["error_rate"] == pytest.approx(0.4)


class TestFetchAccountStats:
    """Tests for fetch_account_stats."""

    @pytest.mark.asyncio()
    async def test_returns_all_accounts(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        names = {r["account_name"] for r in rows}
        assert "acct_a" in names
        assert "acct_b" in names

    @pytest.mark.asyncio()
    async def test_aggregates_per_account(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_name = {r["account_name"]: r for r in rows}
        assert by_name["acct_a"]["request_count"] == 3
        assert by_name["acct_b"]["request_count"] == 2
        assert by_name["acct_b"]["error_count"] == 2


class TestFetchModelStats:
    """Tests for fetch_model_stats."""

    @pytest.mark.asyncio()
    async def test_returns_per_model_rows(self, seeded_db: Database) -> None:
        rows = await queries.fetch_model_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_model = {r["model_id"]: r for r in rows}
        assert "model_x" in by_model
        assert "model_y" in by_model
        assert by_model["model_x"]["request_count"] == 3

    @pytest.mark.asyncio()
    async def test_account_filter(self, seeded_db: Database) -> None:
        account_id = await queries.fetch_account_id(seeded_db, "acct_a")
        assert account_id is not None
        rows = await queries.fetch_model_stats(
            seeded_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            account_id=account_id,
        )
        for r in rows:
            assert r["model_id"] == "model_x"


class TestFetchTimeseries:
    """Tests for fetch_timeseries."""

    @pytest.mark.asyncio()
    async def test_returns_buckets(self, seeded_db: Database) -> None:
        rows = await queries.fetch_timeseries(
            seeded_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            bucket="hour",
        )
        assert len(rows) > 0
        for r in rows:
            assert "bucket" in r
            assert "request_count" in r

    @pytest.mark.asyncio()
    async def test_invalid_bucket_defaults_to_hour(self, seeded_db: Database) -> None:
        rows = await queries.fetch_timeseries(
            seeded_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            bucket="bogus",
        )
        assert len(rows) > 0


class TestFetchErrorBreakdown:
    """Tests for fetch_error_breakdown."""

    @pytest.mark.asyncio()
    async def test_groups_by_error(self, seeded_db: Database) -> None:
        rows = await queries.fetch_error_breakdown(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) >= 1


class TestFetchRecentEvents:
    """Tests for fetch_recent_events."""

    @pytest.mark.asyncio()
    async def test_returns_empty(self, db: Database) -> None:
        rows = await queries.fetch_recent_events(db, 10)
        assert rows == []

    @pytest.mark.asyncio()
    async def test_filters_by_type(self, seeded_db: Database) -> None:
        await seeded_db.execute(
            """
            INSERT INTO account_events (account_id, event_type, details)
            VALUES (
                (SELECT id FROM accounts WHERE name = ?),
                ?, ?
            )
            """,
            ("acct_a", "cooldown_active", '{"seconds": 60}'),
        )
        await seeded_db.connection.commit()
        rows = await queries.fetch_recent_events(
            seeded_db, 10, event_type="cooldown_active"
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "cooldown_active"


class TestFetchActiveReservations:
    """Tests for fetch_active_reservations."""

    @pytest.mark.asyncio()
    async def test_empty_when_no_reservations(self, db: Database) -> None:
        rows = await queries.fetch_active_reservations(db)
        assert rows == []


class TestResolvePeriod:
    """Tests for resolve_period."""

    def test_preset_24h(self) -> None:
        start, end, label = resolve_period("24h")
        assert label == "24h"
        assert end > start
        assert (end - start).total_seconds() == pytest.approx(86400.0, abs=1.0)

    def test_preset_1h(self) -> None:
        start, end, label = resolve_period("1h")
        assert label == "1h"
        assert (end - start).total_seconds() == pytest.approx(3600.0, abs=1.0)

    def test_preset_7d(self) -> None:
        start, end, label = resolve_period("7d")
        assert label == "7d"
        assert (end - start).total_seconds() == pytest.approx(7 * 86400.0, abs=1.0)

    def test_unknown_defaults_to_24h(self) -> None:
        start, end, label = resolve_period(None)
        assert label == "24h"

    def test_custom_range(self) -> None:
        start, end, label = resolve_period("2024-01-01 00:00:00..2024-01-02 00:00:00")
        assert label == "custom"
        assert start.year == 2024
        assert end.year == 2024
        assert (end - start).total_seconds() == 86400.0

    def test_period_presets_complete(self) -> None:
        assert "1h" in PERIOD_PRESETS
        assert "24h" in PERIOD_PRESETS
        assert "7d" in PERIOD_PRESETS
        assert "30d" in PERIOD_PRESETS


class TestTimeRange:
    """Tests for the TimeRange dataclass."""

    def test_str_format(self) -> None:
        from datetime import datetime

        tr = TimeRange(
            start=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC),
            label="custom",
        )
        assert tr.start_str() == "2024-01-01 00:00:00"
        assert tr.end_str() == "2024-01-02 00:00:00"


class TestStatsService:
    """Tests for the high-level StatsService."""

    @pytest.mark.asyncio()
    async def test_get_summary(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        summary = await service.get_summary(time_range)
        assert summary["total_requests"] == 5

    @pytest.mark.asyncio()
    async def test_get_account_stats_includes_reservations(
        self, seeded_db: Database
    ) -> None:
        # Insert a pending request and a reservation
        await seeded_db.execute(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, status
            ) VALUES (
                (SELECT id FROM accounts WHERE name = ?), ?, datetime('now'),
                'pending'
            )
            """,
            ("acct_a", "model_x"),
        )
        req_id_row = await seeded_db.fetch_one("SELECT last_insert_rowid() as id")
        assert req_id_row is not None
        req_id = int(req_id_row["id"])
        await seeded_db.execute(
            """
            INSERT INTO reservations (
                request_id, account_id, model_id, reserved_microdollars, status
            ) VALUES (
                ?, (SELECT id FROM accounts WHERE name = ?), ?, ?, 'active'
            )
            """,
            (req_id, "acct_a", "model_x", 500_000),
        )
        await seeded_db.connection.commit()

        service = StatsService(seeded_db)
        rows = await service.get_account_stats(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        by_name = {r["account_name"]: r for r in rows}
        assert by_name["acct_a"]["reserved_microdollars"] == 500_000

    @pytest.mark.asyncio()
    async def test_get_model_stats_with_filter(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        rows = await service.get_model_stats(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            ),
            account_name="acct_b",
        )
        assert all(r["model_id"] == "model_y" for r in rows)

    @pytest.mark.asyncio()
    async def test_get_timeseries(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        rows = await service.get_timeseries(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            ),
            bucket="hour",
        )
        assert len(rows) > 0

    @pytest.mark.asyncio()
    async def test_get_utilization_imbalance_no_active(
        self, seeded_db: Database
    ) -> None:
        service = StatsService(seeded_db)
        imb = await service.get_utilization_imbalance(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        assert "imbalance_ratio" in imb
        assert imb["active_accounts"] == 2

    @pytest.mark.asyncio()
    async def test_get_utilization_imbalance_single_active(self, db: Database) -> None:
        await db.execute(
            "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
            ("only_one", "ENV_ONLY"),
        )
        await db.connection.commit()
        service = StatsService(db)
        imb = await service.get_utilization_imbalance(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        assert imb["active_accounts"] == 0
        assert imb["imbalance_ratio"] == 0.0

    @pytest.mark.asyncio()
    async def test_get_error_breakdown(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        errors = await service.get_error_breakdown(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        assert isinstance(errors, list)

    @pytest.mark.asyncio()
    async def test_error_breakdown_uses_error_class(self, seeded_db: Database) -> None:
        """Error breakdown should query error_class, not error_message."""
        # Insert a request with error_class and error_detail
        await seeded_db.execute(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, completed_at,
                status, error_class, error_detail
            ) VALUES (
                (SELECT id FROM accounts WHERE name = 'acct_a'),
                'model_x', datetime('now', '-1 hour'),
                datetime('now', '-1 hour'),
                'error', 'AuthenticationError', 'Invalid API key'
            )
            """
        )
        await seeded_db.connection.commit()

        service = StatsService(seeded_db)
        errors = await service.get_error_breakdown(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        assert len(errors) >= 1
        # Find the AuthenticationError row
        auth_errors = [e for e in errors if e["error_class"] == "AuthenticationError"]
        assert len(auth_errors) == 1
        assert auth_errors[0]["error_detail"] == "Invalid API key"

    @pytest.mark.asyncio()
    async def test_get_recent_events(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        events = await service.get_recent_events(10)
        assert isinstance(events, list)

    @pytest.mark.asyncio()
    async def test_get_dashboard_overview(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        overview = await service.get_dashboard_overview(
            TimeRange(
                start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
                end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
                label="custom",
            )
        )
        assert "summary" in overview
        assert "imbalance" in overview
        assert overview["summary"]["total_requests"] == 5

    @pytest.mark.asyncio()
    async def test_dashboard_totals_match_db_aggregates(
        self, seeded_db: Database
    ) -> None:
        """Dashboard summary should match direct SQL aggregates."""
        service = StatsService(seeded_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        overview = await service.get_dashboard_overview(time_range)
        summary = overview["summary"]

        # Direct SQL query for comparison
        row = await seeded_db.fetch_one(
            "SELECT COUNT(*) as cnt, "
            "COALESCE(SUM(input_tokens), 0) as in_tok, "
            "COALESCE(SUM(output_tokens), 0) as out_tok, "
            "COALESCE(SUM(cost_microdollars), 0) as cost "
            "FROM requests"
        )
        assert row is not None
        assert summary["total_requests"] == int(row["cnt"])
        assert summary["total_input_tokens"] == int(row["in_tok"])
        assert summary["total_output_tokens"] == int(row["out_tok"])
        assert summary["total_cost_microdollars"] == int(row["cost"])
