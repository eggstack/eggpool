"""Tests for the statistics query layer and service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository, ReservationRepository
from eggpool.stats import queries
from eggpool.stats.service import (
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

    @pytest.mark.asyncio()
    async def test_summary_total_tokens_aggregate(self, seeded_db: Database) -> None:
        result = await queries.fetch_summary(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        expected = 3 * (100 + 200) + 2 * (50 + 75)
        assert result["total_tokens"] == expected

    @pytest.mark.asyncio()
    async def test_summary_tokens_per_second_aggregate(
        self, seeded_db: Database
    ) -> None:
        result = await queries.fetch_summary(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        total_output_tokens = 3 * 200 + 2 * 75
        total_latency_ms = 3 * 150.0 + 2 * 300.0
        expected = total_output_tokens * 1000.0 / total_latency_ms
        assert result["tokens_per_second"] == pytest.approx(expected)

    @pytest.mark.asyncio()
    async def test_summary_tokens_per_second_zero_when_no_traffic(
        self, db: Database
    ) -> None:
        result = await queries.fetch_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["tokens_per_second"] == 0.0


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
    async def test_account_total_tokens_and_tps(self, seeded_db: Database) -> None:
        rows = await queries.fetch_account_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_name = {r["account_name"]: r for r in rows}
        # acct_a: 3 completed, 100 in / 200 out, 150ms latency each
        a_total = 3 * (100 + 200)
        a_output = 3 * 200
        a_latency_ms = 3 * 150.0
        assert by_name["acct_a"]["total_tokens"] == a_total
        assert by_name["acct_a"]["tokens_per_second"] == pytest.approx(
            a_output * 1000.0 / a_latency_ms
        )
        # acct_b: 2 error, 50 in / 75 out, 300ms latency each
        b_total = 2 * (50 + 75)
        b_output = 2 * 75
        b_latency_ms = 2 * 300.0
        assert by_name["acct_b"]["total_tokens"] == b_total
        assert by_name["acct_b"]["tokens_per_second"] == pytest.approx(
            b_output * 1000.0 / b_latency_ms
        )

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

    @pytest.mark.asyncio()
    async def test_model_total_tokens_and_tps(self, seeded_db: Database) -> None:
        rows = await queries.fetch_model_stats(
            seeded_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_model = {r["model_id"]: r for r in rows}
        x_total = 3 * (100 + 200)
        x_output = 3 * 200
        x_latency = 3 * 150.0
        assert by_model["model_x"]["total_tokens"] == x_total
        assert by_model["model_x"]["tokens_per_second"] == pytest.approx(
            x_output * 1000.0 / x_latency
        )
        y_total = 2 * (50 + 75)
        y_output = 2 * 75
        y_latency = 2 * 300.0
        assert by_model["model_y"]["total_tokens"] == y_total
        assert by_model["model_y"]["tokens_per_second"] == pytest.approx(
            y_output * 1000.0 / y_latency
        )


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

    @pytest.mark.asyncio()
    async def test_buckets_include_total_tokens(self, seeded_db: Database) -> None:
        rows = await queries.fetch_timeseries(
            seeded_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            bucket="hour",
        )
        # All requests fall into a single hour bucket in the seeded data
        # (inserted via datetime('now', '-1 hour')), so the bucket total
        # equals the global total_tokens for the seeded traffic.
        total = sum(r["total_tokens"] for r in rows)
        expected = 3 * (100 + 200) + 2 * (50 + 75)
        assert total == expected


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

    @pytest.mark.parametrize(
        "period",
        ["not-a-period", "invalid..2024-01-02", "2024-01-02..2024-01-01"],
    )
    def test_invalid_period_defaults_to_24h(self, period: str) -> None:
        start, end, label = resolve_period(period)

        assert label == "24h"
        assert (end - start).total_seconds() == pytest.approx(86400.0)

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
    async def test_get_summary_filters_by_account(self, seeded_db: Database) -> None:
        service = StatsService(seeded_db)
        summary = await service.get_summary(
            TimeRange(
                start=datetime.fromisoformat("2000-01-01"),
                end=datetime.fromisoformat("2099-12-31"),
                label="custom",
            ),
            account_name="acct_a",
        )
        assert summary["total_requests"] == 3
        assert summary["successful_requests"] == 3
        assert summary["error_requests"] == 0

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
                    status, bytes_received, bytes_emitted,
                    input_tokens, output_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw2'),
                    'model_bw2',
                    datetime('now', '-1 day'),
                    datetime('now', '-1 day'),
                    'completed', 10000, 5000,
                    100, 200
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
        assert "total_tokens" in rows[0]
        assert "request_count" in rows[0]
        assert rows[0]["total_tokens"] == 300

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
                    status, bytes_received, bytes_emitted,
                    input_tokens, output_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_bw3'),
                    'model_bw3',
                    datetime('now', '-1 day'),
                    datetime('now', '-1 day'),
                    'completed', 10000, 5000,
                    7, 11
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
        assert rows[0]["total_tokens"] == 18


class TestFetchIpStats:
    """Tests for fetch_ip_stats."""

    @pytest.mark.asyncio()
    async def test_ip_total_tokens(self, db: Database) -> None:
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_ip", "ENV_IP", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_ip", "openai"),
            )
            await db.execute_write(
                """
                INSERT INTO requests (
                    account_id, model_id, started_at, completed_at,
                    status, client_ip, input_tokens, output_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = 'acct_ip'),
                    'model_ip',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed', '10.0.0.1', 11, 22
                )
                """
            )

        rows = await queries.fetch_ip_stats(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        assert rows[0]["client_ip"] == "10.0.0.1"
        assert rows[0]["input_tokens"] == 11
        assert rows[0]["output_tokens"] == 22
        assert rows[0]["total_tokens"] == 33


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
        # P50 of [100, 200, 300, 400, 500] is 300. The P99 row must not
        # contaminate the median average.
        assert float(result["p50_ttft_ms"]) == pytest.approx(300.0)

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

    @pytest.mark.asyncio()
    async def test_provider_model_percentiles_use_one_query(self) -> None:
        """Grouped percentile enrichment must not issue N+1 queries."""
        db = AsyncMock(spec=Database)
        db.fetch_all.return_value = [
            {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "request_count": 10,
                "avg_ttft_ms": 20.0,
                "p50_ttft_ms": 15.0,
                "p99_ttft_ms": 40.0,
            }
        ]

        rows = await queries.fetch_provider_model_ttft(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )

        assert rows[0]["p99_ttft_ms"] == 40.0
        db.fetch_all.assert_awaited_once()


class TestDashboardStatsCache:
    @pytest.mark.asyncio()
    async def test_summary_cache_is_bounded_and_reuses_reference(
        self, db: Database
    ) -> None:
        """Cache returns the same reference on hits; callers must not mutate.

        The dashboard cache intentionally avoids a ``deepcopy`` on every
        hit to keep page rendering cheap; cached data is read-only by
        contract and any consumer that mutates the returned value will
        observe the mutation on subsequent calls within the TTL window.
        """
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )
        expected = {"total_requests": 10}

        with patch(
            "eggpool.stats.service.fetch_summary",
            new=AsyncMock(return_value=expected),
        ) as fetch:
            first = await service.get_summary(time_range, use_cache=True)
            second = await service.get_summary(time_range, use_cache=True)

        assert second is first
        fetch.assert_awaited_once()
        assert len(service._dashboard_cache) <= 32

    @pytest.mark.asyncio()
    async def test_bandwidth_cache_serves_repeated_lookups(self, db: Database) -> None:
        """The 90-day heatmap query is served from the dashboard cache."""
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="90d",
        )

        with patch(
            "eggpool.stats.service.fetch_bandwidth_timeseries",
            new=AsyncMock(return_value=[{"day": "2025-01-01"}]),
        ) as fetch:
            first = await service.get_bandwidth_timeseries(time_range, use_cache=True)
            second = await service.get_bandwidth_timeseries(time_range, use_cache=True)

        assert second is first
        fetch.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_timeseries_cache_serves_repeated_lookups(self, db: Database) -> None:
        """``get_timeseries`` participates in the dashboard cache when opted in."""
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )

        with patch(
            "eggpool.stats.service.fetch_timeseries",
            new=AsyncMock(return_value=[{"bucket": "2025-01-01 00:00"}]),
        ) as fetch:
            first = await service.get_timeseries(time_range, use_cache=True)
            second = await service.get_timeseries(time_range, use_cache=True)

        assert second is first
        fetch.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_timeseries_cache_bypassed_when_disabled(self, db: Database) -> None:
        """``get_timeseries`` hits SQLite on every call when ``use_cache`` is off."""
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )

        with patch(
            "eggpool.stats.service.fetch_timeseries",
            new=AsyncMock(return_value=[{"bucket": "2025-01-01 00:00"}]),
        ) as fetch:
            await service.get_timeseries(time_range)
            await service.get_timeseries(time_range)

        assert fetch.await_count == 2

    @pytest.mark.asyncio()
    async def test_ping_summary_cache(self, db: Database) -> None:
        """Ping summary hits the dashboard cache on repeat lookups."""
        from unittest.mock import MagicMock

        repo = MagicMock()
        repo.get_provider_ping_summary = AsyncMock(
            return_value=[{"provider_id": "opencode-go"}]
        )
        service = StatsService(db, ping_repo=repo)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )

        first = await service.get_ping_summary(time_range, use_cache=True)
        second = await service.get_ping_summary(time_range, use_cache=True)

        assert second is first
        repo.get_provider_ping_summary.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_ip_stats_cache(self, db: Database) -> None:
        """IP stats hits the dashboard cache on repeat lookups."""
        service = StatsService(db)
        time_range = TimeRange(
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
            label="24h",
        )

        with patch(
            "eggpool.stats.service.fetch_ip_stats",
            new=AsyncMock(return_value=[{"client_ip": "127.0.0.1"}]),
        ) as fetch:
            first = await service.get_ip_stats(time_range, use_cache=True)
            second = await service.get_ip_stats(time_range, use_cache=True)

        assert second is first
        fetch.assert_awaited_once()


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


# ===================================================================
# Grouped timeseries dashboard tests
# ===================================================================


async def _seed_request(
    db: Database,
    *,
    account_id: int,
    model_id: str,
    provider_id: str,
    status: str = "completed",
    started_at: str = "2024-01-01 12:00:00",
    input_tokens: int = 100,
    output_tokens: int = 200,
    cost_microdollars: int = 1000,
    original_model_id: str | None = None,
    error_class: str | None = None,
    error_detail: str | None = None,
    bytes_received: int = 0,
    bytes_emitted: int = 0,
    upstream_latency_ms: float = 100.0,
    first_byte_ms: int | None = None,
    streamed: int = 0,
) -> None:
    """Insert a single request row with explicit provider/original_model_id.

    Mirrors the shape the production coordinator writes so the query layer
    sees the same column surface used in real traffic.
    """
    async with db.transaction():
        await db.execute_write(
            """
            INSERT INTO requests (
                account_id, model_id, provider_id, started_at, completed_at,
                status, input_tokens, output_tokens, cost_microdollars,
                upstream_latency_ms, bytes_received, bytes_emitted,
                streamed, first_byte_ms, error_class, error_detail,
                original_model_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                model_id,
                provider_id,
                started_at,
                started_at,
                status,
                input_tokens,
                output_tokens,
                cost_microdollars,
                upstream_latency_ms,
                bytes_received,
                bytes_emitted,
                streamed,
                first_byte_ms,
                error_class,
                error_detail,
                original_model_id,
            ),
        )


@pytest_asyncio.fixture()
async def grouped_db(db: Database) -> Database:
    """Seed a database with two providers, two models, and two accounts.

    Each account is bound to a distinct provider_id so the grouped
    timeseries query can exercise the provider_model dimension.
    """
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, 1, ?)",
            ("acct_a", "ENV_A", "prov_one"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, 1, ?)",
            ("acct_b", "ENV_B", "prov_two"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_x", "openai"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
            ("model_y", "anthropic"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("__deprecated__", "openai"),
        )
    return db


async def _account_id(db: Database, name: str) -> int:
    """Resolve an account name to its primary key id."""
    row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
    assert row is not None
    return int(row["id"])


class TestFetchGroupedTimeseries:
    """Tests for ``queries.fetch_grouped_timeseries``."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_stable_payload(self, db: Database) -> None:
        """No data yields the documented empty payload shape."""
        result = await queries.fetch_grouped_timeseries(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        expected_keys = {
            "bucket",
            "group_by",
            "metric",
            "limit",
            "series",
            "buckets",
            "bucket_totals",
            "points",
        }
        assert set(result.keys()) == expected_keys
        assert result["series"] == []
        assert result["buckets"] == []
        assert result["bucket_totals"] == []
        assert result["points"] == []
        assert result["group_by"] == "provider_model"
        assert result["metric"] == "requests"
        assert result["bucket"] == "hour"

    @pytest.mark.asyncio()
    async def test_distinct_series_per_provider_model(
        self, grouped_db: Database
    ) -> None:
        """Each (provider_id, model_id) pair becomes a distinct series."""
        a_id = await _account_id(grouped_db, "acct_a")
        b_id = await _account_id(grouped_db, "acct_b")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_x",
            provider_id="prov_two",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_y",
            provider_id="prov_two",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider_model",
        )
        keys = {s["key"] for s in result["series"]}
        labels = {s["label"] for s in result["series"]}
        assert keys == {
            "prov_one:model_x",
            "prov_one:model_y",
            "prov_two:model_x",
            "prov_two:model_y",
        }
        assert labels == {
            "prov_one / model_x",
            "prov_one / model_y",
            "prov_two / model_x",
            "prov_two / model_y",
        }
        # Each series should carry the right provider/model pair.
        for series in result["series"]:
            assert series["provider_id"] is not None
            assert series["model_id"] is not None

    @pytest.mark.asyncio()
    async def test_two_buckets_one_point_each(self, grouped_db: Database) -> None:
        """Requests across two distinct hours surface as two buckets."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
            started_at="2024-01-01 12:15:00",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
            started_at="2024-01-01 13:45:00",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db, "2024-01-01 00:00:00", "2024-01-02 00:00:00"
        )
        assert result["buckets"] == ["2024-01-01 12:00:00", "2024-01-01 13:00:00"]
        assert len(result["bucket_totals"]) == 2
        # Each bucket holds exactly one request.
        totals_by_bucket = {
            t["bucket"]: int(t["request_count"]) for t in result["bucket_totals"]
        }
        assert totals_by_bucket == {
            "2024-01-01 12:00:00": 1,
            "2024-01-01 13:00:00": 1,
        }
        # And the per-(bucket, series) points sum to two entries.
        assert sum(int(p["request_count"]) for p in result["points"]) == 2

    @pytest.mark.asyncio()
    async def test_top_n_folding_creates_other_series(
        self, grouped_db: Database
    ) -> None:
        """Series beyond ``limit`` collapse into a single ``__other__``."""
        a_id = await _account_id(grouped_db, "acct_a")
        # Five distinct (provider, model) series with a small number of
        # requests each so a low limit triggers the fold.
        seeds = [
            ("prov_one", "model_x"),
            ("prov_one", "model_y"),
            ("prov_two", "model_x"),
            ("prov_two", "model_y"),
            ("prov_three", "model_x"),
        ]
        for prov, model in seeds:
            await _seed_request(
                grouped_db,
                account_id=a_id,
                model_id=model,
                provider_id=prov,
            )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider_model",
            limit=2,
        )
        # Exactly one ``Other`` series at the tail.
        other_series = [s for s in result["series"] if s["is_other"]]
        assert len(other_series) == 1
        assert other_series[0]["key"] == "__other__"
        assert other_series[0]["label"] == "Other"
        assert other_series[0]["provider_id"] is None
        assert other_series[0]["model_id"] is None
        # Only the top-2 ranked series (plus Other) appear in ``series``.
        non_other = [s for s in result["series"] if not s["is_other"]]
        assert len(non_other) == 2
        # Bucket totals equal the sum of folded points (including Other).
        for total in result["bucket_totals"]:
            assert int(total["request_count"]) == 5

    @pytest.mark.asyncio()
    async def test_original_model_id_relinks_series_key(
        self, grouped_db: Database
    ) -> None:
        """Rows with ``original_model_id`` group under the original id."""
        a_id = await _account_id(grouped_db, "acct_a")
        # A row whose live model_id is the deprecated placeholder but whose
        # ``original_model_id`` is preserved must appear under the
        # original name in the series key.
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="__deprecated__",
            provider_id="prov_one",
            original_model_id="old-model",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider_model",
        )
        keys = {s["key"] for s in result["series"]}
        assert "prov_one:old-model" in keys
        assert "prov_one:__deprecated__" not in keys

    @pytest.mark.asyncio()
    async def test_account_id_filter(self, grouped_db: Database) -> None:
        """``account_id`` filter narrows results to one account."""
        a_id = await _account_id(grouped_db, "acct_a")
        b_id = await _account_id(grouped_db, "acct_b")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_x",
            provider_id="prov_two",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider_model",
            account_id=a_id,
        )
        # Only the prov_one series survive.
        assert {s["provider_id"] for s in result["series"]} == {"prov_one"}
        # And every point should carry an ``acct_a`` account name.
        for point in result["points"]:
            assert point["account_name"] == "acct_a"

    @pytest.mark.asyncio()
    async def test_model_id_filter_matches_current_and_original(
        self, grouped_db: Database
    ) -> None:
        """``model_id`` filter accepts relinked (original_model_id) rows."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="__deprecated__",
            provider_id="prov_one",
            original_model_id="model_x",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider_model",
            model_id="model_x",
        )
        # Only prov_one:model_x survives; the relinked row counts.
        assert sum(int(p["request_count"]) for p in result["points"]) == 2
        for series in result["series"]:
            assert series["model_id"] == "model_x"

    @pytest.mark.asyncio()
    async def test_invalid_bucket_falls_back_to_hour(
        self, grouped_db: Database
    ) -> None:
        """Unknown bucket values are silently coerced to ``"hour"``."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            bucket="bogus",
        )
        assert result["bucket"] == "hour"
        # Hour-shaped bucket label, not day-shaped.
        assert all(
            ":" in b and len(b) == len("2024-01-01 12:00:00") for b in result["buckets"]
        )

    @pytest.mark.asyncio()
    async def test_invalid_group_by_falls_back_to_provider_model(
        self, grouped_db: Database
    ) -> None:
        """Unknown ``group_by`` values default to provider_model semantics."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="garbage",
        )
        assert result["group_by"] == "provider_model"
        assert len(result["series"]) == 2
        # Keys must combine provider and model.
        for series in result["series"]:
            assert ":" in series["key"]

    @pytest.mark.asyncio()
    async def test_group_by_provider_only(self, grouped_db: Database) -> None:
        """``provider`` grouping collapses across models on the same provider."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_two",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="provider",
        )
        keys = {s["key"] for s in result["series"]}
        assert keys == {"prov_one", "prov_two"}
        # Series keys must be just provider ids (no model suffix).
        for series in result["series"]:
            assert ":" not in series["key"]

    @pytest.mark.asyncio()
    async def test_group_by_model_collapses_across_providers(
        self, grouped_db: Database
    ) -> None:
        """``model`` grouping merges traffic for the same model across providers."""
        a_id = await _account_id(grouped_db, "acct_a")
        b_id = await _account_id(grouped_db, "acct_b")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_x",
            provider_id="prov_two",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="model",
        )
        keys = {s["key"] for s in result["series"]}
        assert keys == {"model_x", "model_y"}
        # Total requests for model_x collapses across both providers.
        by_key = {s["key"]: s for s in result["series"]}
        assert by_key["model_x"]["total_requests"] == 2

    @pytest.mark.asyncio()
    async def test_group_by_account_uses_account_name(
        self, grouped_db: Database
    ) -> None:
        """``account`` grouping keys by account name, not id."""
        a_id = await _account_id(grouped_db, "acct_a")
        b_id = await _account_id(grouped_db, "acct_b")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_y",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_x",
            provider_id="prov_two",
        )

        result = await queries.fetch_grouped_timeseries(
            grouped_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            group_by="account",
        )
        keys = {s["key"] for s in result["series"]}
        labels = {s["label"] for s in result["series"]}
        assert keys == {"acct_a", "acct_b"}
        assert labels == {"acct_a", "acct_b"}
        # And the per-series account_name projection matches.
        by_key = {s["key"]: s for s in result["series"]}
        assert by_key["acct_a"]["account_name"] == "acct_a"
        assert by_key["acct_b"]["account_name"] == "acct_b"


class TestStatsServiceGroupedTimeseries:
    """Tests for ``StatsService.get_grouped_timeseries``."""

    @pytest.mark.asyncio()
    async def test_unknown_account_returns_empty_payload(
        self, grouped_db: Database
    ) -> None:
        """Unknown account must not raise; the payload stays stable."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        service = StatsService(grouped_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        result = await service.get_grouped_timeseries(
            time_range, account_name="nonexistent"
        )
        assert result is not None
        assert result["series"] == []
        assert result["buckets"] == []
        assert result["bucket_totals"] == []
        assert result["points"] == []
        assert result["group_by"] == "provider_model"

    @pytest.mark.asyncio()
    async def test_invalid_arguments_normalize_safely(
        self, grouped_db: Database
    ) -> None:
        """Garbage bucket/group_by/limit values are coerced without raising."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        service = StatsService(grouped_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        result = await service.get_grouped_timeseries(
            time_range,
            bucket="garbage",
            group_by="nope",
            limit=9999,
        )
        # Bucket, group_by, and limit were all normalized.
        assert result["bucket"] == "hour"
        assert result["group_by"] == "provider_model"
        assert result["limit"] == 25

    @pytest.mark.asyncio()
    async def test_cache_reuses_same_reference(self, grouped_db: Database) -> None:
        """``use_cache=True`` returns the same dict object on a hit."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        service = StatsService(grouped_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        first = await service.get_grouped_timeseries(time_range, use_cache=True)
        second = await service.get_grouped_timeseries(time_range, use_cache=True)
        assert second is first

    @pytest.mark.asyncio()
    async def test_cache_keys_differ_by_group_by(self, grouped_db: Database) -> None:
        """Different ``group_by`` values must not share cache entries."""
        a_id = await _account_id(grouped_db, "acct_a")
        b_id = await _account_id(grouped_db, "acct_b")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )
        await _seed_request(
            grouped_db,
            account_id=b_id,
            model_id="model_x",
            provider_id="prov_two",
        )

        service = StatsService(grouped_db)
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2000-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-12-31"),
            label="custom",
        )
        by_provider = await service.get_grouped_timeseries(
            time_range, group_by="provider", use_cache=True
        )
        by_model = await service.get_grouped_timeseries(
            time_range, group_by="model", use_cache=True
        )
        assert by_provider is not by_model
        # The shape should reflect the different grouping dimensions.
        assert {s["key"] for s in by_provider["series"]} == {"prov_one", "prov_two"}
        assert {s["key"] for s in by_model["series"]} == {"model_x"}

    @pytest.mark.asyncio()
    async def test_empty_time_range_returns_empty_payload(
        self, grouped_db: Database
    ) -> None:
        """A window with no traffic yields the stable empty payload."""
        a_id = await _account_id(grouped_db, "acct_a")
        await _seed_request(
            grouped_db,
            account_id=a_id,
            model_id="model_x",
            provider_id="prov_one",
        )

        service = StatsService(grouped_db)
        # Narrow window that doesn't include the seeded request.
        time_range = TimeRange(
            start=__import__("datetime").datetime.fromisoformat("2099-01-01"),
            end=__import__("datetime").datetime.fromisoformat("2099-01-02"),
            label="custom",
        )
        result = await service.get_grouped_timeseries(time_range)
        assert result is not None
        assert result["series"] == []
        assert result["buckets"] == []
        assert result["bucket_totals"] == []
        assert result["points"] == []
