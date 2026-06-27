"""Tests for the UsageRollupRepository."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.rollup_repository import UsageRollupRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "rollup_repo_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest.fixture()
def repo(db: Database) -> UsageRollupRepository:
    return UsageRollupRepository(db)


def _row(
    *,
    bucket_start: str = "2025-06-15T12:00:00Z",
    bucket_size_s: int = 60,
    provider_id: str = "prov_a",
    model_id: str = "model_a",
    account_id: int = 1,
    protocol: str = "openai",
    streamed: int = 0,
    status: str = "completed",
    request_count: int = 1,
    error_count: int = 0,
    retry_count: int = 0,
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    thinking_characters: int = 0,
    cost_microdollars: int = 100,
    bytes_received: int = 500,
    bytes_emitted: int = 250,
    latency_ms_sum: int = 100,
    latency_ms_min: int | None = 100,
    latency_ms_max: int | None = 100,
    first_byte_ms_sum: int = 0,
    first_byte_ms_count: int = 0,
) -> dict[str, object]:
    return {
        "bucket_start": bucket_start,
        "bucket_size_s": bucket_size_s,
        "provider_id": provider_id,
        "model_id": model_id,
        "account_id": account_id,
        "protocol": protocol,
        "streamed": streamed,
        "status": status,
        "request_count": request_count,
        "error_count": error_count,
        "retry_count": retry_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "thinking_characters": thinking_characters,
        "cost_microdollars": cost_microdollars,
        "bytes_received": bytes_received,
        "bytes_emitted": bytes_emitted,
        "latency_ms_sum": latency_ms_sum,
        "latency_ms_min": latency_ms_min,
        "latency_ms_max": latency_ms_max,
        "first_byte_ms_sum": first_byte_ms_sum,
        "first_byte_ms_count": first_byte_ms_count,
    }


class TestUpsertManyEmpty:
    @pytest.mark.asyncio()
    async def test_returns_zero(self, repo: UsageRollupRepository) -> None:
        result = await repo.upsert_many([])
        assert result == 0


class TestUpsertManyCreatesRows:
    @pytest.mark.asyncio()
    async def test_creates_new_row(self, repo: UsageRollupRepository) -> None:
        count = await repo.upsert_many([_row()])
        assert count == 1

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 1


class TestUpsertManyIncrementsCounters:
    @pytest.mark.asyncio()
    async def test_increments_on_conflict(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many([_row(input_tokens=10, output_tokens=20)])
        await repo.upsert_many([_row(input_tokens=30, output_tokens=40)])

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 40
        assert rows[0]["output_tokens"] == 60
        assert rows[0]["request_count"] == 2


class TestUpsertManyLatencyMinMax:
    @pytest.mark.asyncio()
    async def test_min_decreases_max_increases(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many([_row(latency_ms_min=100, latency_ms_max=100)])
        await repo.upsert_many([_row(latency_ms_min=50, latency_ms_max=200)])
        await repo.upsert_many([_row(latency_ms_min=75, latency_ms_max=150)])

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["latency_ms_min"] == 50
        assert rows[0]["latency_ms_max"] == 200


class TestUpsertManyLatencyMinMaxNull:
    @pytest.mark.asyncio()
    async def test_null_existing_replaced_by_new(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many([_row(latency_ms_min=None, latency_ms_max=None)])
        await repo.upsert_many([_row(latency_ms_min=42, latency_ms_max=99)])

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["latency_ms_min"] == 42
        assert rows[0]["latency_ms_max"] == 99

    @pytest.mark.asyncio()
    async def test_new_null_keeps_existing(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many([_row(latency_ms_min=10, latency_ms_max=90)])
        await repo.upsert_many([_row(latency_ms_min=None, latency_ms_max=None)])

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["latency_ms_min"] == 10
        assert rows[0]["latency_ms_max"] == 90


class TestQueryTimeseriesBasic:
    @pytest.mark.asyncio()
    async def test_returns_grouped_data(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(bucket_start="2025-06-15T12:00:00Z"),
                _row(bucket_start="2025-06-15T12:01:00Z"),
            ]
        )

        rows = await repo.query_timeseries(
            start="2025-06-15T12:00:00Z",
            end="2025-06-15T12:02:00Z",
            bucket_size_s=60,
        )
        assert len(rows) == 2
        assert rows[0]["bucket"] == "2025-06-15T12:00:00Z"
        assert rows[1]["bucket"] == "2025-06-15T12:01:00Z"
        assert "series_key" in rows[0]

    @pytest.mark.asyncio()
    async def test_streamed_counts_use_request_count(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many(
            [
                _row(streamed=1, request_count=4),
                _row(streamed=0, request_count=2),
            ]
        )

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 6
        assert rows[0]["streamed"] == 4


class TestQueryTimeseriesProviderFilter:
    @pytest.mark.asyncio()
    async def test_filters_by_provider(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(provider_id="prov_a", model_id="m1"),
                _row(provider_id="prov_b", model_id="m1"),
            ]
        )

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
            provider_id="prov_a",
        )
        assert len(rows) == 1
        assert rows[0]["series_key"] == "prov_a/m1"


class TestQueryTimeseriesModelFilter:
    @pytest.mark.asyncio()
    async def test_filters_by_model(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(provider_id="prov_a", model_id="m1"),
                _row(provider_id="prov_a", model_id="m2"),
            ]
        )

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
            model_id="m1",
        )
        assert len(rows) == 1
        assert rows[0]["series_key"] == "prov_a/m1"


class TestQueryTimeseriesAccountFilter:
    @pytest.mark.asyncio()
    async def test_filters_by_account(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(account_id=1),
                _row(account_id=2),
            ]
        )

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
            group_by="account",
            account_id=1,
        )
        assert len(rows) == 1
        assert rows[0]["series_key"] == "1"


class TestQueryTimeseriesEmpty:
    @pytest.mark.asyncio()
    async def test_returns_empty_list(self, repo: UsageRollupRepository) -> None:
        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert rows == []


class TestQueryTimeseriesInvalidGroupBy:
    @pytest.mark.asyncio()
    async def test_raises_on_invalid_group_by(
        self, repo: UsageRollupRepository
    ) -> None:
        with pytest.raises(ValueError, match="Invalid group_by"):
            await repo.query_timeseries(
                start="2000-01-01T00:00:00Z",
                end="2099-12-31T23:59:59Z",
                bucket_size_s=60,
                group_by="bogus",
            )


class TestQueryFlatTimeseries:
    @pytest.mark.asyncio()
    async def test_returns_one_row_per_bucket(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many(
            [
                _row(
                    bucket_start="2025-06-15T12:00:00Z",
                    model_id="m1",
                    request_count=3,
                    input_tokens=30,
                ),
                _row(
                    bucket_start="2025-06-15T12:00:00Z",
                    model_id="m2",
                    request_count=2,
                    input_tokens=20,
                ),
                _row(
                    bucket_start="2025-06-15T12:01:00Z",
                    model_id="m1",
                    request_count=1,
                    input_tokens=10,
                ),
            ]
        )

        rows = await repo.query_flat_timeseries(
            start="2025-06-15T12:00:00Z",
            end="2025-06-15T12:02:00Z",
            bucket_size_s=60,
        )
        assert len(rows) == 2
        assert rows[0]["bucket"] == "2025-06-15T12:00:00Z"
        assert rows[0]["request_count"] == 5
        assert rows[0]["input_tokens"] == 50
        assert rows[1]["bucket"] == "2025-06-15T12:01:00Z"
        assert rows[1]["request_count"] == 1


class TestQuerySummaryBasic:
    @pytest.mark.asyncio()
    async def test_returns_correct_totals(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(
                    request_count=3,
                    input_tokens=300,
                    output_tokens=600,
                    cost_microdollars=3000,
                ),
                _row(
                    request_count=2,
                    input_tokens=200,
                    output_tokens=400,
                    cost_microdollars=2000,
                ),
            ]
        )

        summary = await repo.query_summary(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
        )
        assert summary["total_requests"] == 5
        assert summary["total_input_tokens"] == 500
        assert summary["total_output_tokens"] == 1000
        assert summary["total_cost_microdollars"] == 5000

    @pytest.mark.asyncio()
    async def test_streamed_totals_use_request_count(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many(
            [
                _row(streamed=1, request_count=3),
                _row(streamed=0, request_count=2),
            ]
        )

        summary = await repo.query_summary(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
        )
        assert summary["total_requests"] == 5
        assert summary["streamed_requests"] == 3
        assert summary["non_streamed_requests"] == 2


class TestQuerySummaryEmpty:
    @pytest.mark.asyncio()
    async def test_returns_zero_valued_dict(self, repo: UsageRollupRepository) -> None:
        summary = await repo.query_summary(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
        )
        assert summary["total_requests"] == 0
        assert summary["error_requests"] == 0
        assert summary["total_input_tokens"] == 0
        assert summary["total_output_tokens"] == 0
        assert summary["total_cost_microdollars"] == 0
        assert summary["avg_latency_ms"] == 0.0


class TestCleanupOldRollups:
    @pytest.mark.asyncio()
    async def test_deletes_old_rows(self, repo: UsageRollupRepository) -> None:
        await repo.upsert_many(
            [
                _row(
                    bucket_start="2020-01-01T00:00:00Z",
                    model_id="old_model_a",
                ),
                _row(
                    bucket_start="2020-01-02T00:00:00Z",
                    model_id="old_model_b",
                ),
                _row(bucket_start="2026-06-20T12:00:00Z"),
            ]
        )

        deleted = await repo.cleanup_old_rollups(retain_days=30)
        assert deleted == 2

        rows = await repo.query_timeseries(
            start="2000-01-01T00:00:00Z",
            end="2099-12-31T23:59:59Z",
            bucket_size_s=60,
        )
        assert len(rows) == 1
        assert rows[0]["bucket"] == "2026-06-20T12:00:00Z"


class TestCleanupOldRollupsNoOldData:
    @pytest.mark.asyncio()
    async def test_returns_zero_when_no_old_data(
        self, repo: UsageRollupRepository
    ) -> None:
        await repo.upsert_many([_row(bucket_start="2026-06-20T12:00:00Z")])

        deleted = await repo.cleanup_old_rollups(retain_days=30)
        assert deleted == 0
