"""Tests for attempt observability queries and service methods.

Covers Phase 1 of the metrics-core-api plan: per-attempt aggregates,
retry-category distribution, request trace endpoint, and the
attempt-finalizer side-effects that record provider/model/protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import AttemptRepository, RequestRepository
from eggpool.request.attempt_finalizer import AttemptFinalizationData, AttemptFinalizer
from eggpool.retry.classification import RetryCategory
from eggpool.stats import queries
from eggpool.stats.service import StatsService, TimeRange, resolve_time_range

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "attempt_stats_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_attempts_db(db: Database) -> Database:
    """Seed an account, a model, a request, and a chain of attempts.

    Three attempts: attempt 1 (transient 502, retryable),
    attempt 2 (rate-limited 429, retryable),
    attempt 3 (200 success, is_retry_outcome=0).
    """
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_x", "ENV_X", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_z", "openai", "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO requests ("
            "account_id, model_id, provider_id, status, started_at, "
            "completed_at, input_tokens, output_tokens"
            ") VALUES ("
            "(SELECT id FROM accounts WHERE name = ?), ?, ?, ?, "
            "datetime('now', '-1 hour'), datetime('now', '-1 hour'), "
            "100, 200"
            ")",
            ("acct_x", "model_z", "opencode-go", "completed"),
        )
    rows = await db.fetch_all("SELECT id FROM requests")
    request_id = int(rows[0]["id"])
    attempts_data = [
        (1, "transient", 502, "ConnectError", 100, 0, 0, "attempt_retryable"),
        (2, "quota_exceeded", 429, "RateLimitError", 100, 0, 250, "attempt_retryable"),
        (3, None, 200, None, 100, 150, 400, "attempt_completed"),
    ]
    async with db.transaction():
        for (
            attempt_num,
            category,
            status_code,
            error_class,
            bytes_recv,
            bytes_emit,
            latency,
            release_reason,
        ) in attempts_data:
            await db.execute_write(
                "INSERT INTO request_attempts ("
                "request_id, attempt_number, account_id, provider_id, "
                "model_id, protocol, status_code, error_class, "
                "retry_category, release_reason, "
                "bytes_received, bytes_emitted, latency_ms, "
                "is_retry_outcome, started_at, completed_at, streamed"
                ") VALUES ("
                "?, ?, (SELECT id FROM accounts WHERE name = ?), ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "datetime('now', '-1 hour'), datetime('now', '-1 hour'), 0"
                ")",
                (
                    request_id,
                    attempt_num,
                    "acct_x",
                    "opencode-go",
                    "model_z",
                    "openai",
                    status_code,
                    error_class,
                    category,
                    release_reason,
                    bytes_recv,
                    bytes_emit,
                    latency,
                    1 if category is not None else 0,
                ),
            )
    return db


class TestFetchAttemptStats:
    """Tests for fetch_attempt_stats."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_zeros(self, db: Database) -> None:
        result = await queries.fetch_attempt_stats(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_attempts"] == 0
        assert result["retry_attempts"] == 0
        assert result["retry_rate"] == 0.0
        assert result["avg_attempt_latency_ms"] == 0.0

    @pytest.mark.asyncio()
    async def test_aggregates_three_attempts(
        self, seeded_attempts_db: Database
    ) -> None:
        result = await queries.fetch_attempt_stats(
            seeded_attempts_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_attempts"] == 3
        # Two attempts had is_retry_outcome=1
        assert result["retry_attempts"] == 2
        # One success (status 200)
        assert result["success_attempts"] == 1
        # Two failures (502 and 429)
        assert result["failed_attempts"] == 2
        assert result["retry_rate"] == pytest.approx(2 / 3)
        # Latency aggregate (0, 250, 400) = 650/3
        assert result["avg_attempt_latency_ms"] == pytest.approx(650 / 3)

    @pytest.mark.asyncio()
    async def test_filter_by_account(self, seeded_attempts_db: Database) -> None:
        result = await queries.fetch_attempt_stats(
            seeded_attempts_db,
            "2000-01-01 00:00:00",
            "2099-12-31 23:59:59",
            account_id=99999,  # nonexistent
        )
        assert result["total_attempts"] == 0


class TestFetchRetryDistribution:
    """Tests for fetch_retry_distribution."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_no_rows(self, db: Database) -> None:
        rows = await queries.fetch_retry_distribution(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert rows == []

    @pytest.mark.asyncio()
    async def test_distribution_groups_by_category(
        self, seeded_attempts_db: Database
    ) -> None:
        rows = await queries.fetch_retry_distribution(
            seeded_attempts_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        by_category = {r["retry_category"]: r for r in rows}
        assert "transient" in by_category
        assert "quota_exceeded" in by_category
        assert "unclassified" in by_category  # the success attempt
        assert by_category["transient"]["attempt_count"] == 1
        assert by_category["quota_exceeded"]["attempt_count"] == 1
        assert by_category["unclassified"]["attempt_count"] == 1
        assert by_category["unclassified"]["success_count"] == 1


class TestFetchRequestTrace:
    """Tests for fetch_request_trace."""

    @pytest.mark.asyncio()
    async def test_missing_request_returns_none(self, db: Database) -> None:
        result = await queries.fetch_request_trace(db, 99999)
        assert result is None

    @pytest.mark.asyncio()
    async def test_returns_request_and_attempts(
        self, seeded_attempts_db: Database
    ) -> None:
        rows = await seeded_attempts_db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        trace = await queries.fetch_request_trace(seeded_attempts_db, request_id)
        assert trace is not None
        assert trace["request"]["id"] == request_id
        assert trace["request"]["resolved_model_id"] == "model_z"
        assert len(trace["attempts"]) == 3
        assert trace["attempts"][0]["attempt_number"] == 1
        assert trace["attempts"][0]["retry_category"] == "transient"
        assert trace["attempts"][0]["is_retry_outcome"] == 1
        assert trace["attempts"][2]["attempt_number"] == 3
        assert trace["attempts"][2]["status_code"] == 200
        assert trace["attempts"][2]["is_retry_outcome"] == 0


class TestStatsServiceAttemptMethods:
    """Tests for the high-level StatsService methods."""

    @pytest.mark.asyncio()
    async def test_get_attempt_stats_with_account_filter(
        self, seeded_attempts_db: Database
    ) -> None:
        service = StatsService(seeded_attempts_db)
        time_range = resolve_time_range("24h")
        result = await service.get_attempt_stats(time_range, account_name="acct_x")
        assert result["total_attempts"] == 3
        assert result["retry_attempts"] == 2

    @pytest.mark.asyncio()
    async def test_get_retry_distribution(self, seeded_attempts_db: Database) -> None:
        service = StatsService(seeded_attempts_db)
        time_range = resolve_time_range("24h")
        rows = await service.get_retry_distribution(time_range)
        categories = {r["retry_category"] for r in rows}
        assert "transient" in categories
        assert "quota_exceeded" in categories

    @pytest.mark.asyncio()
    async def test_get_request_trace(self, seeded_attempts_db: Database) -> None:
        service = StatsService(seeded_attempts_db)
        rows = await seeded_attempts_db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        trace = await service.get_request_trace(request_id)
        assert trace is not None
        assert trace["request"]["id"] == request_id
        assert len(trace["attempts"]) == 3


class TestAttemptFinalizerNewFields:
    """Verify the finalizer persists the new observability columns."""

    @pytest.mark.asyncio()
    async def test_finalize_persists_retry_category(self, db: Database) -> None:
        attempt_repo = AttemptRepository(db)
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_y", "ENV_Y", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_y", "openai"),
            )
            await db.execute_write(
                "INSERT INTO requests ("
                "account_id, model_id, started_at, status, protocol, "
                "streamed, proxy_request_id, provider_id"
                ") VALUES ("
                "(SELECT id FROM accounts WHERE name = ?), ?, "
                "datetime('now'), 'pending', 'openai', 0, "
                "'test-proxy-id', 'opencode-go'"
                ")",
                ("acct_y", "model_y"),
            )
        request_rows = await db.fetch_all("SELECT id FROM requests")
        request_id = int(request_rows[0]["id"])
        async with db.transaction():
            attempt_id = await attempt_repo.create(
                request_id=request_id,
                attempt_number=1,
                account_id=1,
                provider_id="opencode-go",
                model_id="model_y",
                protocol="openai",
                streamed=False,
            )
        async with db.transaction():
            await AttemptFinalizer(
                db, attempt_repo, None, persist_error_detail=False
            ).finalize_failed_attempt(
                attempt_id=attempt_id,
                reservation_id="",
                data=AttemptFinalizationData(
                    status_code=429,
                    error_class="RateLimitError",
                    retry_category=RetryCategory.QUOTA_EXCEEDED.value,
                    release_reason="attempt_retryable",
                    bytes_received=128,
                    latency_ms=350,
                    is_retry_outcome=True,
                ),
            )
        rows = await db.fetch_all(
            "SELECT * FROM request_attempts WHERE id = ?", (attempt_id,)
        )
        row = dict(rows[0])
        assert row["retry_category"] == "quota_exceeded"
        assert row["release_reason"] == "attempt_retryable"
        assert row["bytes_received"] == 128
        assert row["latency_ms"] == 350
        assert row["is_retry_outcome"] == 1

    @pytest.mark.asyncio()
    async def test_first_attempt_stamps_first_attempt_at(self, db: Database) -> None:
        attempt_repo = AttemptRepository(db)
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_z", "ENV_Z", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_q", "openai"),
            )
            await RequestRepository(db).create_pending(
                request_id="trace-test-1",
                model_id="model_q",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
        rows = await db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        async with db.transaction():
            await attempt_repo.create(
                request_id=request_id,
                attempt_number=1,
                account_id=1,
                provider_id="opencode-go",
                model_id="model_q",
                protocol="openai",
            )
        request_rows = await db.fetch_all(
            "SELECT first_attempt_at FROM requests WHERE id = ?",
            (request_id,),
        )
        assert request_rows[0]["first_attempt_at"] is not None

    @pytest.mark.asyncio()
    async def test_finalize_stamps_last_attempt_id(self, db: Database) -> None:
        attempt_repo = AttemptRepository(db)
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_q", "ENV_Q", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_p", "openai"),
            )
            await RequestRepository(db).create_pending(
                request_id="trace-test-2",
                model_id="model_p",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
        rows = await db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        async with db.transaction():
            attempt_id = await attempt_repo.create(
                request_id=request_id,
                attempt_number=1,
                account_id=1,
                provider_id="opencode-go",
                model_id="model_p",
                protocol="openai",
            )
        await attempt_repo.finalize_if_incomplete(
            attempt_id=attempt_id,
            status_code=200,
            bytes_emitted=512,
            release_reason="attempt_completed",
        )
        rows = await db.fetch_all(
            "SELECT last_attempt_id FROM requests WHERE id = ?",
            (request_id,),
        )
        assert int(rows[0]["last_attempt_id"]) == attempt_id


def test_period_label_includes_iso_range() -> None:
    """Sanity check that resolve_time_range normalizes input."""
    tr = resolve_time_range("24h")
    assert tr.label == "24h"
    end = datetime.now(UTC).timestamp()
    start = tr.start.timestamp()
    assert (end - start) >= 86000


def test_time_range_default_label() -> None:
    tr = TimeRange(
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 1, 2, tzinfo=UTC),
        label="custom",
    )
    assert tr.start_str() == "2024-01-01 00:00:00"
    assert tr.end_str() == "2024-01-02 00:00:00"
