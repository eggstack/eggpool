"""Tests for latency-phase observability.

Covers Phase 4 of the metrics-core-api plan: phase-decomposed latency
queries that split ``first_byte_ms`` and ``upstream_latency_ms`` into
``upstream_connect_ms``, ``upstream_read_ms``, and
``coordinator_overhead_ms`` components.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.stats import queries
from eggpool.stats.service import StatsService, resolve_time_range

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "latency_phases_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


_ACCOUNT_COUNTER = 0
_MODEL_COUNTER = 0


async def _insert_request(
    db: Database,
    *,
    connect: int,
    read: int,
    overhead: int,
    first_byte: int,
    latency: int,
) -> int:
    global _ACCOUNT_COUNTER, _MODEL_COUNTER
    _ACCOUNT_COUNTER += 1
    _MODEL_COUNTER += 1
    account_name = f"acct-{_ACCOUNT_COUNTER}"
    model_id = f"model-l-{_MODEL_COUNTER}"
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            (account_name, "ENV_L", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            (model_id, "openai", "opencode-go"),
        )
        new_id = await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, completed_at, status, "
            "  first_byte_ms, upstream_latency_ms, "
            "  upstream_connect_ms, upstream_read_ms, "
            "  coordinator_overhead_ms"
            ") VALUES (?, 1, ?, 'openai', 1, "
            "  datetime('now', ? || ' seconds'), datetime('now'), "
            "  'completed', ?, ?, ?, ?, ?)",
            (
                f"r-{connect}-{read}-{overhead}",
                model_id,
                "-1",
                first_byte,
                latency,
                connect,
                read,
                overhead,
            ),
        )
    return int(new_id)


@pytest.mark.asyncio
async def test_fetch_latency_phase_breakdown_aggregates_each_phase(
    db: Database,
) -> None:
    await _insert_request(
        db, connect=20, read=30, overhead=10, first_byte=50, latency=60
    )
    await _insert_request(
        db, connect=40, read=70, overhead=20, first_byte=110, latency=130
    )

    result = await queries.fetch_latency_phase_breakdown(
        db, "1970-01-01 00:00:00", "2999-12-31 23:59:59"
    )
    assert result["request_count"] == 2
    phases = result["phases"]
    assert phases["upstream_connect_ms"]["sample_count"] == 2
    assert phases["upstream_connect_ms"]["avg_ms"] == pytest.approx(30.0)
    assert phases["upstream_read_ms"]["sample_count"] == 2
    assert phases["upstream_read_ms"]["avg_ms"] == pytest.approx(50.0)
    assert phases["coordinator_overhead_ms"]["sample_count"] == 2
    assert phases["coordinator_overhead_ms"]["avg_ms"] == pytest.approx(15.0)
    assert phases["first_byte_ms"]["sample_count"] == 2
    assert phases["first_byte_ms"]["avg_ms"] == pytest.approx(80.0)
    assert phases["upstream_latency_ms"]["sample_count"] == 2
    assert phases["upstream_latency_ms"]["avg_ms"] == pytest.approx(95.0)


@pytest.mark.asyncio
async def test_fetch_latency_phase_breakdown_handles_null_phases(
    db: Database,
) -> None:
    # Pre-migration rows: phase columns NULL, but TTFB + latency populated.
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_n", "ENV_N", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_n", "openai", "opencode-go"),
        )
        await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, status, first_byte_ms, upstream_latency_ms"
            ") VALUES (?, 1, 'model_n', 'openai', 1, "
            "  datetime('now'), 'completed', 100, 200)",
            ("r-null",),
        )

    result = await queries.fetch_latency_phase_breakdown(
        db, "1970-01-01 00:00:00", "2999-12-31 23:59:59"
    )
    assert result["request_count"] == 1
    assert result["phases"]["upstream_connect_ms"]["sample_count"] == 0
    assert result["phases"]["upstream_connect_ms"]["avg_ms"] == 0.0
    assert result["phases"]["first_byte_ms"]["sample_count"] == 1
    assert result["phases"]["first_byte_ms"]["avg_ms"] == 100.0


@pytest.mark.asyncio
async def test_fetch_latency_phase_breakdown_empty_window(
    db: Database,
) -> None:
    result = await queries.fetch_latency_phase_breakdown(
        db, "2998-01-01 00:00:00", "2998-01-02 00:00:00"
    )
    assert result["request_count"] == 0
    assert all(phase["sample_count"] == 0 for phase in result["phases"].values())


@pytest.mark.asyncio
async def test_stats_service_exposes_latency_phases(
    db: Database,
) -> None:
    await _insert_request(
        db, connect=10, read=15, overhead=5, first_byte=25, latency=30
    )
    service = StatsService(db)
    result = await service.get_latency_phase_breakdown(resolve_time_range("7d"))
    assert result["phases"]["upstream_connect_ms"]["avg_ms"] == 10.0
    assert result["phases"]["coordinator_overhead_ms"]["avg_ms"] == 5.0


@pytest.mark.asyncio
async def test_latency_phase_endpoint_via_fastapi(
    db: Database,
) -> None:
    """End-to-end smoke: /api/stats/latency must include 'phases' key."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from eggpool.api.stats import register_stats_routes

    await _insert_request(db, connect=5, read=7, overhead=3, first_byte=12, latency=15)
    app = FastAPI()
    app.state.stats_db = db
    app.state.stats = StatsService(db)
    register_stats_routes(app, require_auth=False)
    client = TestClient(app)
    response = client.get("/api/stats/latency?period=7d")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "phases" in payload
    assert payload["phases"]["phases"]["upstream_connect_ms"]["sample_count"] == 1
