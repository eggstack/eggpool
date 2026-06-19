"""Tests for the statistics query layer and service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import RequestRepository, ReservationRepository
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
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("acct_a", "ENV_A", 1),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("acct_b", "ENV_B", 1),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_x", "openai"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_y", "anthropic"),
        )
    # Insert some completed requests
    async with db.transaction():
        for _i in range(3):
            await db.execute_write(
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
            await db.execute_write(
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
                (
                    "acct_b",
                    "model_y",
                    50,
                    75,
                    300.0,
                    "RateLimitError",
                    "rate_limited",
                ),
            )
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
        assert "total_providers" in result

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
        assert result["total_providers"] >= 1


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
    async def test_returns_provider_id(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        for row in rows:
            assert "provider_id" in row

    @pytest.mark.asyncio()
    async def test_provider_id_value(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        for row in rows:
            assert row["provider_id"] == "opencode-go"

    @pytest.mark.asyncio()
    async def test_aggregates_per_account(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_name = {r["account_name"]: r for r in rows}
        assert by_name["acct_a"]["request_count"] == 3
        assert by_name["acct_b"]["request_count"] == 2
        assert by_name["acct_b"]["error_count"] == 2

    @pytest.mark.asyncio()
    async def test_cancelled_requests_count_in_usage_windows(
        self, seeded_db: Database
    ) -> None:
        async with seeded_db.transaction():
            await seeded_db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, input_tokens, output_tokens, cost_microdollars,
                    upstream_latency_ms
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?,
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'cancelled', ?, ?, ?, ?
                )
                """,
                ("acct_b", "model_y", 1, 2, 250_000, 12.0),
            )

        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_name = {r["account_name"]: r for r in rows}
        assert by_name["acct_b"]["cost_5h"] == 250_000
        assert by_name["acct_b"]["cost_7d"] == 250_000
        assert by_name["acct_b"]["cost_30d"] == 250_000


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
        async with seeded_db.transaction():
            await seeded_db.execute_write(
                """
                INSERT INTO account_events (account_id, event_type, details)
                VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    ?, ?
                )
                """,
                ("acct_a", "cooldown_active", '{"seconds": 60}'),
            )
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
        async with seeded_db.transaction():
            await seeded_db.execute_write(
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
            await seeded_db.execute_write(
                """
                INSERT INTO reservations (
                    request_id, account_id, model_id, reserved_microdollars, status
                ) VALUES (
                    ?, (SELECT id FROM accounts WHERE name = ?), ?, ?, 'active'
                )
                """,
                (req_id, "acct_a", "model_x", 500_000),
            )

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
        assert by_name["acct_a"]["utilization_5h"] == pytest.approx(600_000)
        assert by_name["acct_a"]["utilization_7d"] == pytest.approx(3_000_000 / 168)

    @pytest.mark.asyncio()
    async def test_get_account_stats_includes_live_reservation_path(
        self, seeded_db: Database
    ) -> None:
        request_repo = RequestRepository(seeded_db)
        reservation_repo = ReservationRepository(seeded_db)

        async with seeded_db.transaction():
            request_id = await request_repo.create_pending(
                request_id="live-reservation-req",
                model_id="model_x",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            await reservation_repo.create(
                request_id=request_id,
                account_id=1,
                model_id="model_x",
                estimated_tokens=1_000,
                estimated_microdollars=500_000,
            )

        service = StatsService(seeded_db)
        rows = await service.get_account_stats(
            TimeRange(
                start=datetime.fromisoformat("2000-01-01"),
                end=datetime.fromisoformat("2099-12-31"),
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
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env) VALUES (?, ?)",
                ("only_one", "ENV_ONLY"),
            )
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
        async with seeded_db.transaction():
            await seeded_db.execute_write(
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


class TestBandwidthQueries:
    """Tests for bandwidth tracking in queries."""

    @pytest.mark.asyncio()
    async def test_summary_includes_bandwidth(self, seeded_db: Database) -> None:
        """Summary should include total_bytes_received and total_bytes_emitted."""
        result = await queries.fetch_summary(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert "total_bytes_received" in result
        assert "total_bytes_emitted" in result
        assert result["total_bytes_received"] == 0
        assert result["total_bytes_emitted"] == 0

    @pytest.mark.asyncio()
    async def test_summary_bandwidth_with_data(self, db: Database) -> None:
        """Summary bandwidth should aggregate from requests with bandwidth data."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_bw", "ENV_BW", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_bw", "openai"),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, bytes_received, bytes_emitted
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw'),
                    'model_bw',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed', 5000, 2500
                )
                """
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, bytes_received, bytes_emitted
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw'),
                    'model_bw',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed', 3000, 1500
                )
                """
            )

        result = await queries.fetch_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_bytes_received"] == 8000
        assert result["total_bytes_emitted"] == 4000

    @pytest.mark.asyncio()
    async def test_account_stats_includes_bandwidth(self, seeded_db: Database) -> None:
        """Account stats should include bytes_received and bytes_emitted."""
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        for row in rows:
            assert "bytes_received" in row
            assert "bytes_emitted" in row

    @pytest.mark.asyncio()
    async def test_timeseries_includes_bandwidth(self, seeded_db: Database) -> None:
        """Timeseries should include bytes_received and bytes_emitted."""
        rows = await queries.fetch_timeseries(
            seeded_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            bucket="hour",
        )
        for row in rows:
            assert "bytes_received" in row
            assert "bytes_emitted" in row

    @pytest.mark.asyncio()
    async def test_fetch_bandwidth_timeseries(self, db: Database) -> None:
        """fetch_bandwidth_timeseries should return daily-bucketed bandwidth."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_bw2", "ENV_BW2", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_bw2", "openai"),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, bytes_received, bytes_emitted
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw2'),
                    'model_bw2',
                    datetime('now', '-1 day'),
                    datetime('now', '-1 day'),
                    'completed', 10000, 5000
                )
                """
            )

        rows = await queries.fetch_bandwidth_timeseries(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) >= 1
        assert "day" in rows[0]
        assert "bytes_received" in rows[0]
        assert "bytes_emitted" in rows[0]
        assert "request_count" in rows[0]

    @pytest.mark.asyncio()
    async def test_fetch_bandwidth_timeseries_with_account_filter(
        self, db: Database
    ) -> None:
        """fetch_bandwidth_timeseries should filter by account_id."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_bw3", "ENV_BW3", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_bw3", "openai"),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, bytes_received, bytes_emitted
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw3'),
                    'model_bw3',
                    datetime('now', '-1 day'),
                    datetime('now', '-1 day'),
                    'completed', 10000, 5000
                )
                """
            )

        account_id = await queries.fetch_account_id(db, "acct_bw3")
        assert account_id is not None

        rows = await queries.fetch_bandwidth_timeseries(
            db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            account_id=account_id,
        )
        assert len(rows) >= 1
        assert rows[0]["bytes_received"] == 10000
        assert rows[0]["bytes_emitted"] == 5000


class TestStatsServiceBandwidth:
    """Tests for StatsService.get_bandwidth_timeseries."""

    @pytest.mark.asyncio()
    async def test_get_bandwidth_timeseries(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        daily = await service.get_bandwidth_timeseries(time_range)
        assert isinstance(daily, list)

    @pytest.mark.asyncio()
    async def test_get_bandwidth_timeseries_with_account(
        self, seeded_db: Database
    ) -> None:
        service = StatsService(seeded_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        daily = await service.get_bandwidth_timeseries(
            time_range, account_name="acct_a"
        )
        assert isinstance(daily, list)

    @pytest.mark.asyncio()
    async def test_get_bandwidth_timeseries_unknown_account(
        self, seeded_db: Database
    ) -> None:
        service = StatsService(seeded_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        daily = await service.get_bandwidth_timeseries(
            time_range, account_name="nonexistent"
        )
        assert daily == []


# ===================================================================
# TTFT (Time-to-First-Token) tests
# ===================================================================


@pytest_asyncio.fixture()
async def ttft_db(db: Database) -> Database:
    """Seed the database with TTFT-specific data: streamed and non-streamed requests."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("ttft_acct", "ENV_TTFT", 1),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_ttft", "openai"),
        )
    # Insert streamed requests with known first_byte_ms values
    async with db.transaction():
        for fbt in [100, 200, 300, 400, 500]:
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, streamed, first_byte_ms
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    'model_ttft',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed', 1, ?
                )
                """,
                ("ttft_acct", fbt),
            )
        # Insert non-streamed request with first_byte_ms (should be excluded)
        await db.execute_write(
            """
            INSERT INTO requests (
                account_id, model_id, started_at, completed_at,
                status, streamed, first_byte_ms
            ) VALUES (
                (SELECT id FROM accounts WHERE name = ?),
                'model_ttft',
                datetime('now', '-1 hour'),
                datetime('now', '-1 hour'),
                'completed', 0, 9999
            )
            """,
            ("ttft_acct",),
        )
    return db


class TestTTFTStatsQueries:
    """Tests for TTFT fields in stats queries."""

    @pytest.mark.asyncio()
    async def test_summary_includes_ttft_fields(self, ttft_db: Database) -> None:
        """fetch_summary includes avg_ttft_ms, p50_ttft_ms, p99_ttft_ms."""
        result = await queries.fetch_summary(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert "avg_ttft_ms" in result
        assert "p50_ttft_ms" in result
        assert "p99_ttft_ms" in result

    @pytest.mark.asyncio()
    async def test_summary_ttft_avg_correct(self, ttft_db: Database) -> None:
        """avg_ttft_ms is the average of streamed first_byte_ms values."""
        result = await queries.fetch_summary(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        # Average of [100, 200, 300, 400, 500] = 300.0
        assert float(result["avg_ttft_ms"]) == pytest.approx(300.0)

    @pytest.mark.asyncio()
    async def test_summary_ttft_p50_correct(self, ttft_db: Database) -> None:
        """p50_ttft_ms approximates the median of streamed first_byte_ms."""
        result = await queries.fetch_summary(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        # P50 of [100, 200, 300, 400, 500] should be ~300
        assert float(result["p50_ttft_ms"]) == pytest.approx(300.0, abs=100.0)

    @pytest.mark.asyncio()
    async def test_summary_ttft_p99_correct(self, ttft_db: Database) -> None:
        """p99_ttft_ms approximates the 99th percentile."""
        result = await queries.fetch_summary(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        # P99 of 5 values should be close to the max (500)
        assert float(result["p99_ttft_ms"]) >= 400.0

    @pytest.mark.asyncio()
    async def test_streamed_only_filtering(self, db: Database) -> None:
        """Non-streamed requests are excluded from TTFT calculations."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("stream_acct", "ENV_STREAM", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_s", "openai"),
            )
        async with db.transaction():
            # One streamed request with first_byte_ms=100
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, streamed, first_byte_ms
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    'model_s', datetime('now'), datetime('now'),
                    'completed', 1, 100
                )
                """,
                ("stream_acct",),
            )
            # One non-streamed request with first_byte_ms=9999 (should not count)
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, streamed, first_byte_ms
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    'model_s', datetime('now'), datetime('now'),
                    'completed', 0, 9999
                )
                """,
                ("stream_acct",),
            )

        result = await queries.fetch_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        # Only the streamed request (100ms) should be counted
        assert float(result["avg_ttft_ms"]) == pytest.approx(100.0)

    @pytest.mark.asyncio()
    async def test_ttft_zero_when_no_streamed(self, db: Database) -> None:
        """TTFT fields are 0 when no streamed requests exist."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("no_stream", "ENV_NS", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_ns", "openai"),
            )
        async with db.transaction():
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, streamed
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    'model_ns', datetime('now'), datetime('now'),
                    'completed', 0
                )
                """,
                ("no_stream",),
            )

        result = await queries.fetch_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert float(result["avg_ttft_ms"]) == 0.0
        assert float(result["p50_ttft_ms"]) == 0.0
        assert float(result["p99_ttft_ms"]) == 0.0


class TestTTFTProviderModelQueries:
    """Tests for per-provider/model TTFT breakdown queries."""

    @pytest.mark.asyncio()
    async def test_fetch_provider_model_ttft(self, ttft_db: Database) -> None:
        """fetch_provider_model_ttft returns per-provider/model breakdown."""
        rows = await queries.fetch_provider_model_ttft(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["provider_id"] == "opencode-go"
        assert row["model_id"] == "model_ttft"
        assert row["request_count"] == 5
        assert float(row["avg_ttft_ms"]) == pytest.approx(300.0)
        assert "p50_ttft_ms" in row
        assert "p99_ttft_ms" in row

    @pytest.mark.asyncio()
    async def test_fetch_provider_ttft_summary(self, ttft_db: Database) -> None:
        """fetch_provider_ttft_summary returns per-provider aggregate."""
        rows = await queries.fetch_provider_ttft_summary(
            ttft_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["provider_id"] == "opencode-go"
        assert row["request_count"] == 5
        assert float(row["avg_ttft_ms"]) == pytest.approx(300.0)
        assert "p50_ttft_ms" in row
        assert "p99_ttft_ms" in row

    @pytest.mark.asyncio()
    async def test_provider_model_ttft_empty_when_no_data(self, db: Database) -> None:
        """Returns empty list when no TTFT data exists."""
        rows = await queries.fetch_provider_model_ttft(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert rows == []

    @pytest.mark.asyncio()
    async def test_provider_ttft_summary_empty_when_no_data(self, db: Database) -> None:
        """Returns empty list when no TTFT data exists."""
        rows = await queries.fetch_provider_ttft_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert rows == []


class TestTTFTStatsService:
    """Tests for StatsService TTFT methods."""

    @pytest.mark.asyncio()
    async def test_get_provider_ttft_summary(self, ttft_db: Database) -> None:
        """StatsService.get_provider_ttft_summary returns TTFT data."""
        service = StatsService(ttft_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        rows = await service.get_provider_ttft_summary(time_range)
        assert len(rows) == 1
        assert rows[0]["provider_id"] == "opencode-go"
        assert float(rows[0]["avg_ttft_ms"]) == pytest.approx(300.0)

    @pytest.mark.asyncio()
    async def test_get_provider_model_ttft(self, ttft_db: Database) -> None:
        """StatsService.get_provider_model_ttft returns per-model TTFT."""
        service = StatsService(ttft_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        rows = await service.get_provider_model_ttft(time_range)
        assert len(rows) == 1
        assert rows[0]["model_id"] == "model_ttft"
        assert float(rows[0]["avg_ttft_ms"]) == pytest.approx(300.0)

    @pytest.mark.asyncio()
    async def test_ttft_in_dashboard_overview(self, ttft_db: Database) -> None:
        """Dashboard overview includes TTFT summary fields."""
        service = StatsService(ttft_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        overview = await service.get_dashboard_overview(time_range)
        summary = overview["summary"]
        assert "avg_ttft_ms" in summary
        assert "p50_ttft_ms" in summary
        assert "p99_ttft_ms" in summary
        assert float(summary["avg_ttft_ms"]) == pytest.approx(300.0)
