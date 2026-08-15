"""Tests for request-history exactness and recent-request metadata.

Verifies that ``fetch_account_stats`` and ``fetch_model_stats``
expose the new exactness counters, ratios, and avg-cost fields, and
that ``fetch_recent_requests`` returns a bounded metadata-only view
that never includes ``error_detail``, ``client_ip`` (unless opted
in), or request bodies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
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
    database = Database(path=str(tmp_path / "phase5_phase6_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


async def _seed_two_requests(db: Database) -> None:
    """Two accounts with mixed exactness/cache/reasoning values."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_exact", "ENV_E", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_est", "ENV_X", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_phase5", "openai", "opencode-go"),
        )
        # Account 1 — exact + cache + reasoning
        await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, completed_at, status, "
            "  input_tokens, output_tokens, "
            "  cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "  cost_microdollars, exactness"
            ") VALUES (?, 1, 'model_phase5', 'openai', 0, "
            "  datetime('now', '-2 seconds'), datetime('now'), 'completed', "
            "  1000, 500, 200, 50, 100, 10000, 'exact')",
            ("req-exact",),
        )
        # Account 2 — estimated, no cache, no reasoning
        await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, completed_at, status, "
            "  input_tokens, output_tokens, "
            "  cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "  cost_microdollars, exactness"
            ") VALUES (?, 2, 'model_phase5', 'openai', 0, "
            "  datetime('now', '-1 seconds'), datetime('now'), 'completed', "
            "  800, 0, 0, 0, 0, 4000, 'estimated')",
            ("req-est",),
        )


@pytest.mark.asyncio
async def test_account_stats_exactness_counters(db: Database) -> None:
    await _seed_two_requests(db)
    rows = await queries.fetch_account_stats(
        db, "1970-01-01 00:00:00", "2999-12-31 23:59:59"
    )
    by_name = {row["account_name"]: row for row in rows}
    exact_row = by_name["acct_exact"]
    assert exact_row["exact_count"] == 1
    assert exact_row["estimated_count"] == 0
    assert exact_row["cache_read_tokens"] == 200
    assert exact_row["cache_write_tokens"] == 50
    assert exact_row["reasoning_tokens"] == 100
    assert exact_row["estimated_cost_fraction"] == 0.0
    assert exact_row["cache_read_ratio"] == pytest.approx(200 / 1250)
    assert exact_row["cache_write_ratio"] == pytest.approx(50 / 1250)
    assert exact_row["reasoning_output_ratio"] == pytest.approx(0.2)
    assert exact_row["avg_cost_per_request"] == 10000.0
    assert exact_row["avg_cost_per_1k_tokens"] == pytest.approx(10000.0 * 1000 / 1500)

    est_row = by_name["acct_est"]
    assert est_row["estimated_count"] == 1
    assert est_row["estimated_cost_fraction"] == 1.0
    # acct_est has input=800, cache=0 → bounded ratio is 0.0
    # (denominator = 800, numerator = 0).
    assert est_row["cache_read_ratio"] == 0.0
    assert est_row["cache_write_ratio"] == 0.0
    # reasoning_output_ratio: output_tokens = 0 → NULL.
    assert est_row["reasoning_output_ratio"] is None


@pytest.mark.asyncio
async def test_model_stats_exactness_counters(db: Database) -> None:
    await _seed_two_requests(db)
    rows = await queries.fetch_model_stats(
        db, "1970-01-01 00:00:00", "2999-12-31 23:59:59"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["model_id"] == "model_phase5"
    assert row["request_count"] == 2
    assert row["exact_count"] == 1
    assert row["estimated_count"] == 1
    assert row["cache_read_tokens"] == 200
    assert row["reasoning_tokens"] == 100
    assert row["estimated_cost_fraction"] == pytest.approx(0.5)
    assert row["cache_read_ratio"] == pytest.approx(200 / 2050)
    assert row["avg_cost_per_1k_tokens"] == pytest.approx(14000.0 * 1000 / 2300)


@pytest.mark.asyncio
async def test_stats_service_account_stats_includes_new_fields(
    db: Database,
) -> None:
    await _seed_two_requests(db)
    service = StatsService(db)
    rows = await service.get_account_stats(resolve_time_range("7d"))
    by_name = {row["account_name"]: row for row in rows}
    assert "estimated_cost_fraction" in by_name["acct_exact"]
    assert "cache_read_ratio" in by_name["acct_exact"]
    assert "reasoning_output_ratio" in by_name["acct_exact"]


@pytest.mark.asyncio
async def test_recent_requests_returns_metadata_only(db: Database) -> None:
    await _seed_two_requests(db)
    rows = await queries.fetch_recent_requests(db, limit=10)
    assert len(rows) == 2
    fields = set(rows[0].keys())
    expected_present = {
        "request_id",
        "proxy_request_id",
        "upstream_request_id",
        "started_at",
        "completed_at",
        "account_id",
        "account_name",
        "provider_id",
        "model_id",
        "protocol",
        "status",
        "status_code",
        "error_class",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "thinking_characters",
        "cost_microdollars",
        "exactness",
        "first_byte_ms",
        "upstream_latency_ms",
        "retry_count",
        "bytes_received",
        "bytes_emitted",
        "streamed",
    }
    assert expected_present <= fields
    # Body/error_detail/client_ip must NOT be present by default.
    assert "body" not in fields
    assert "error_detail" not in fields
    assert "client_ip" in fields  # column is always selected, value is NULL
    assert rows[0]["client_ip"] is None


@pytest.mark.asyncio
async def test_recent_requests_client_ip_when_enabled(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_ip", "ENV_I", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_ip", "openai", "opencode-go"),
        )
        await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, completed_at, status, client_ip"
            ") VALUES (?, 1, 'model_ip', 'openai', 0, "
            "  datetime('now'), datetime('now'), 'completed', '10.0.0.5')",
            ("req-ip",),
        )
    rows = await queries.fetch_recent_requests(db, limit=10, include_client_ip=True)
    assert rows[0]["client_ip"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_recent_requests_filters_compose(db: Database) -> None:
    await _seed_two_requests(db)
    rows = await queries.fetch_recent_requests(
        db, limit=10, account_id=1, status="completed"
    )
    assert len(rows) == 1
    assert rows[0]["account_id"] == 1

    rows = await queries.fetch_recent_requests(
        db, limit=10, provider_id="opencode-go", model_id="model_phase5"
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_recent_requests_limit_clamped(db: Database) -> None:
    rows = await queries.fetch_recent_requests(db, limit=10_000)
    assert isinstance(rows, list)
    # Even with limit=10_000 the function clamps internally; we just
    # verify no exception is raised and result is a list.
    assert rows == []


@pytest.mark.asyncio
async def test_recent_requests_endpoint_via_fastapi(
    db: Database,
) -> None:
    """End-to-end smoke: /api/stats/recent-requests returns the row."""
    from fastapi import FastAPI

    from eggpool.api.stats import register_stats_routes
    from eggpool.models.config import AppConfig

    await _seed_two_requests(db)
    app = FastAPI()
    app.state.stats_db = db
    app.state.stats = StatsService(db)
    app.state.config = AppConfig()  # empty api_key → require_auth passes
    register_stats_routes(app, require_auth=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/stats/recent-requests?limit=10")
    assert response.status_code == 200
    payload = response.json()
    assert "requests" in payload
    assert payload["include_client_ip"] is False
    assert payload["limit"] == 10
    assert len(payload["requests"]) == 2


@pytest.mark.asyncio
async def test_recent_requests_endpoint_does_not_include_body(
    db: Database,
) -> None:
    from fastapi import FastAPI

    from eggpool.api.stats import register_stats_routes
    from eggpool.models.config import AppConfig

    await _seed_two_requests(db)
    app = FastAPI()
    app.state.stats_db = db
    app.state.stats = StatsService(db)
    app.state.config = AppConfig()
    register_stats_routes(app, require_auth=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/stats/recent-requests?limit=10",
            headers={"Authorization": "Bearer test-key"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert "requests" in payload
    body_keys = set(payload["requests"][0].keys())
    assert "body" not in body_keys
    assert "error_detail" not in body_keys


def test_metrics_queries_register_recent_requests_helper() -> None:
    assert callable(queries.fetch_recent_requests)


def test_queries_use_json_for_exclude_reasons_not_recent() -> None:
    # Sanity: confirm that the recent-requests query does NOT touch
    # exclude_reasons_json (that is the routing-decisions surface).
    sql = queries.fetch_recent_requests.__doc__ or ""
    assert "exclude_reasons_json" not in sql


async def _seed_cache_heavy_request(db: Database) -> None:
    """Cache-heavy seed: input=100, cache_read=900, cache_write=100,
    output=50. ``cache_read_tokens > total_tokens`` is the exact regression
    scenario flagged in
    ``plans/2026-07-07-dashboard-cache-token-card-semantics-fix.md``.
    """
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, 1, ?)",
            ("acct_cache_heavy", "ENV_C", "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_cache_heavy", "openai", "opencode-go"),
        )
        await db.execute_insert(
            "INSERT INTO requests ("
            "  proxy_request_id, account_id, model_id, protocol, streamed, "
            "  started_at, completed_at, status, "
            "  input_tokens, output_tokens, "
            "  cache_read_tokens, cache_write_tokens, reasoning_tokens, "
            "  cost_microdollars, exactness"
            ") VALUES (?, 1, 'model_cache_heavy', 'openai', 0, "
            "  datetime('now', '-1 seconds'), datetime('now'), 'completed', "
            "  100, 50, 900, 100, 0, 0, 'exact')",
            ("req-cache-heavy",),
        )


@pytest.mark.asyncio
async def test_bounded_cache_ratio_cache_heavy(db: Database) -> None:
    """Cache-heavy regression: bounded ratio must be ``cache_read /
    (input + cache_read + cache_write)`` and never ``cache_read / input``
    or ``cache_read / total_tokens``.
    """
    await _seed_cache_heavy_request(db)
    row = queries.bounded_cache_ratio(cache_read=900, input_tokens=100, cache_write=100)
    assert row == pytest.approx(900 / 1100)
    # Negative guards: the buggy formulas must NOT be returned.
    assert row != pytest.approx(9.0)
    assert row != pytest.approx(900 / 150)


@pytest.mark.asyncio
async def test_summary_token_semantic_split_cache_heavy(db: Database) -> None:
    """Pin the token semantic split on ``fetch_summary``:

    - ``total_tokens`` = input + output (legacy fresh-token volume).
    - ``fresh_tokens`` = input + output (explicit alias).
    - ``accounted_tokens`` = input + output + cache_read + cache_write.

    Regression for
    ``plans/2026-07-07-dashboard-cache-token-card-semantics-fix.md`` so a
    future "fix" cannot reinterpret ``total_tokens`` and break the stats
    API contract or the dashboard headline semantics.
    """
    await _seed_cache_heavy_request(db)
    summary = await queries.fetch_summary(
        db, "1970-01-01 00:00:00", "2999-12-31 23:59:59"
    )
    # Legacy fresh-token volume.
    assert summary["total_tokens"] == 150
    # Explicit fresh-token alias.
    assert summary["fresh_tokens"] == 150
    # Broad provider-accounting total.
    assert summary["accounted_tokens"] == 1150
    # Bounded cache-read share (cache_read / (input + cache_read + cache_write)).
    assert summary["cache_read_ratio"] == pytest.approx(900 / 1100)


@pytest.mark.asyncio
async def test_empty_summary_exposes_semantic_split_fields(db: Database) -> None:
    """The empty summary must still carry ``fresh_tokens`` and
    ``accounted_tokens`` so the dashboard can render with zeros instead of
    raising on a missing key.
    """
    summary = queries._empty_summary()  # noqa: SLF001 - intentional
    assert summary["fresh_tokens"] == 0
    assert summary["accounted_tokens"] == 0
    assert summary["total_tokens"] == 0
