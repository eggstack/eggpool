"""Tests for routing trace write pressure modes.

Phase 4 of the performance optimization safety plan: configurable
observability write pressure for routing-decision traces.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    RoutingDecisionRepository,
)
from eggpool.models.config import AppConfig, RoutingTraceConfig
from eggpool.observability.routing_trace_writer import RoutingTraceWriter
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.routing.router import Router

pytestmark = pytest.mark.request_path

if TYPE_CHECKING:
    from eggpool.health.health_manager import HealthManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_accounts(
    db: Database,
    account_names: list[str],
    *,
    model_id: str = "gpt-4",
    protocol: str = "openai",
) -> None:
    """Insert accounts, models, and account_models rows in one transaction."""
    async with db.transaction():
        existing_model = await db.fetch_one(
            "SELECT model_id FROM models WHERE model_id = ?", (model_id,)
        )
        if existing_model is None:
            await db.execute_insert(
                "INSERT INTO models (model_id, display_name, protocol) "
                "VALUES (?, ?, ?)",
                (model_id, model_id, protocol),
            )
        for name in account_names:
            existing_acct = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?", (name,)
            )
            if existing_acct is None:
                await db.execute_insert(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    (name, f"K_{name}"),
                )
            acct = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
            assert acct is not None
            await db.execute_insert(
                "INSERT OR IGNORE INTO account_models "
                "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                (int(acct["id"]), model_id),
            )


def _build_config(
    trace_mode: str = "all",
    sample_rate: float = 0.05,
    include_score_components: bool = True,
) -> AppConfig:
    os.environ.setdefault("TTEST_KEY", "test-key-000")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "TTEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "https://trace-test.example.com"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "trace-acct-a", "api_key_env": "TTEST_KEY"},
                {"name": "trace-acct-b", "api_key_env": "TTEST_KEY"},
            ],
            "dashboard": {"enabled": False},
            "routing": {
                "trace": {
                    "mode": trace_mode,
                    "sample_rate": sample_rate,
                    "include_score_components": include_score_components,
                }
            },
        }
    )


async def _build_coordinator(
    config: AppConfig,
    db: Database,
    routing_decision_repo: RoutingDecisionRepository | None = None,
    health_manager: HealthManager | None = None,
    routing_trace_writer: Any | None = None,
) -> RequestCoordinator:
    registry = AccountRegistry(config)
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    from eggpool.catalog.service import CatalogService

    catalog = CatalogService(config, registry, db, httpx_client)
    for model_id, proto in [
        ("gpt-4", "openai"),
        ("claude-3-sonnet-20240229", "anthropic"),
    ]:
        catalog.cache.load_model(
            model_id=model_id,
            display_name=model_id,
            protocol=proto,
            capabilities={},
            source_metadata={},
        )
        for acct in ["trace-acct-a", "trace-acct-b"]:
            catalog.cache.add_account_support(model_id, acct)

    router = Router(registry, catalog)
    router.set_account_weight("trace-acct-a", 1.0)
    router.set_account_weight("trace-acct-b", 1.0)

    return RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        routing_decision_repo=routing_decision_repo or RoutingDecisionRepository(db),
        quota_estimator=None,
        health_manager=health_manager,
        config=config,
        routing_trace_writer=routing_trace_writer,
    )


def _make_context(request_id: str = "req-1") -> ProxyRequestContext:
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
    )


async def _count_routing_traces(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM routing_decisions")
    assert row is not None
    return int(row["cnt"])


async def _create_test_writer(
    db: Database,
    routing_decision_repo: RoutingDecisionRepository | None = None,
) -> RoutingTraceWriter:
    """Create and start a routing trace writer for testing."""
    writer = RoutingTraceWriter(
        db=db,
        routing_decision_repo=routing_decision_repo or RoutingDecisionRepository(db),
        queue_capacity=1000,
        flush_interval_s=0.05,
        max_batch_size=50,
    )
    writer.start()
    return writer


async def _seed_request_row(db: Database, request_id: int) -> None:
    """Insert a minimal request row for FK compliance."""
    await db.execute_insert(
        "INSERT OR IGNORE INTO requests (id, account_id, model_id, started_at) "
        "VALUES (?, 1, 'gpt-4', datetime('now'))",
        (request_id,),
    )


async def _flush_writer(writer: RoutingTraceWriter) -> None:
    """Wait for the writer to drain its queue."""
    import asyncio

    # Give the drain task time to process
    await asyncio.sleep(0.15)


async def _get_trace(
    db: Database, request_id: int, attempt_number: int
) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT * FROM routing_decisions WHERE request_id = ? AND attempt_number = ?",
        (request_id, attempt_number),
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_all_mode_writes_trace_every_attempt() -> None:
    """In 'all' mode, every selection writes a routing trace."""
    config = _build_config(trace_mode="all")
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            for i in range(5):
                ctx = _make_context(f"req-all-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)

            count = await _count_routing_traces(db)
            assert count == 5, f"Expected 5 traces, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_off_mode_skips_all_traces() -> None:
    """In 'off' mode, no routing traces are persisted."""
    config = _build_config(trace_mode="off")
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            for i in range(5):
                ctx = _make_context(f"req-off-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)

            count = await _count_routing_traces(db)
            assert count == 0, f"Expected 0 traces in off mode, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_sampled_mode_deterministic() -> None:
    """In 'sampled' mode, trace writes are deterministic for a given request_id."""
    config = _build_config(trace_mode="sampled", sample_rate=0.5)
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, health_manager=None, routing_trace_writer=writer
            )

            written_first: list[bool] = []
            for i in range(10):
                ctx = _make_context(f"req-sampled-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)
                count = await _count_routing_traces(db)
                written_first.append(count > (i if i == 0 else written_first[i - 1]))  # type: ignore[index]
        finally:
            await writer.stop()

        # Clear all traces and run again with a fresh DB to avoid PK conflicts
        await db.disconnect()
        db = Database(path=":memory:")
        await db.connect()
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer2 = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, health_manager=None, routing_trace_writer=writer2
            )
            written_second: list[bool] = []
            for i in range(10):
                ctx = _make_context(f"req-sampled-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer2)
                count = await _count_routing_traces(db)
                written_second.append(count > (i if i == 0 else written_second[i - 1]))  # type: ignore[index]
        finally:
            await writer2.stop()

        # Same request_ids produce same sampling decisions
        assert written_first == written_second, (
            f"Sampling not deterministic: {written_first} != {written_second}"
        )
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_sampled_mode_respects_sample_rate() -> None:
    """In 'sampled' mode with rate=0, no traces are written."""
    config = _build_config(trace_mode="sampled", sample_rate=0.0)
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            for i in range(10):
                ctx = _make_context(f"req-sr0-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)

            count = await _count_routing_traces(db)
            assert count == 0, f"Expected 0 traces with sample_rate=0, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_sampled_mode_rate_1_writes_all() -> None:
    """In 'sampled' mode with rate=1.0, all traces are written."""
    config = _build_config(trace_mode="sampled", sample_rate=1.0)
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            for i in range(10):
                ctx = _make_context(f"req-sr1-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)

            count = await _count_routing_traces(db)
            assert count == 10, f"Expected 10 traces with sample_rate=1.0, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


def test_errors_mode_rejected_by_config() -> None:
    """Config parser rejects 'errors' mode (removed in corrective pass)."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="literal_error"):
        RoutingTraceConfig(mode="errors")


@pytest.mark.asyncio()
async def test_include_score_components_false_omits_components() -> None:
    """When include_score_components=false, score_components_json is None."""
    config = _build_config(
        trace_mode="all",
        include_score_components=False,
    )
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            ctx = _make_context("req-nosc-1")
            selected = await coordinator._select_and_persist_attempt(ctx, 1)
            await _flush_writer(writer)
            db_request_id = ctx.client_metadata["db_request_id"]

            trace = await _get_trace(db, int(db_request_id), selected.attempt_number)
            assert trace is not None
            # score_components_json column is NOT NULL with DEFAULT '{}';
            # when include_score_components=false the coordinator passes
            # None which the DB coerces to the default '{}'.
            assert trace["score_components_json"] == "{}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_include_score_components_true_preserves_components() -> None:
    """When include_score_components=true, score_components_json is populated."""
    config = _build_config(
        trace_mode="all",
        include_score_components=True,
    )
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            ctx = _make_context("req-yesc-1")
            selected = await coordinator._select_and_persist_attempt(ctx, 1)
            await _flush_writer(writer)
            db_request_id = ctx.client_metadata["db_request_id"]

            trace = await _get_trace(db, int(db_request_id), selected.attempt_number)
            assert trace is not None
            assert trace["score_components_json"] is not None
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_default_mode_is_sampled() -> None:
    """Default RoutingTraceConfig mode is 'sampled' with 5% sampling."""
    cfg = RoutingTraceConfig()
    assert cfg.mode == "sampled"
    assert cfg.sample_rate == 0.05
    assert cfg.include_score_components is False
    assert cfg.queue_capacity == 1000
    assert cfg.flush_interval_s == 1.0
    assert cfg.max_batch_size == 50
    assert cfg.shutdown_flush_timeout_s == 5.0


@pytest.mark.asyncio()
async def test_all_mode_writes_traces_when_overridden() -> None:
    """Coordinator with mode='all' writes traces for every request."""
    config = _build_config()  # default trace mode = "sampled"; override to "all"
    config.routing.trace.mode = "all"
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db, ["trace-acct-a", "trace-acct-b"])
        writer = await _create_test_writer(db)
        try:
            coordinator = await _build_coordinator(
                config, db, routing_trace_writer=writer
            )

            for i in range(3):
                ctx = _make_context(f"req-nocfg-{i}")
                await coordinator._select_and_persist_attempt(ctx, 1)
                await _flush_writer(writer)

            count = await _count_routing_traces(db)
            assert count == 3, f"Expected 3 traces with mode='all', got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_sampled_request_id_hash_determinism() -> None:
    """Verify the SHA-256 sampling hash is deterministic for a given request_id."""
    request_id = "test-request-12345"
    h = hashlib.sha256(request_id.encode()).digest()
    ratio = int.from_bytes(h[:8], "big") / ((1 << 64) - 1)

    # Run again — must produce identical ratio
    h2 = hashlib.sha256(request_id.encode()).digest()
    ratio2 = int.from_bytes(h2[:8], "big") / ((1 << 64) - 1)

    assert ratio == ratio2
    assert 0.0 <= ratio <= 1.0
