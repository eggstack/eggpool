"""Milestone D — Integration tests for routing trace writer.

Proves acceptance criteria that require wiring between coordinator,
writer, and database under realistic conditions:

1. Trace DB writes never delay upstream send (acceptance #3)
2. Writer survives rehash without duplication (acceptance #5)
3. Writer crash/restart does not fail proxy requests (acceptance #3)
4. Stale parent rows are silently skipped by the writer (D6)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import httpx
import pytest
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    RoutingDecisionRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.observability.routing_trace_writer import (
    RoutingTraceEvent,
    RoutingTraceWriter,
)
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator
from eggpool.routing.router import Router

pytestmark = pytest.mark.request_path

UPSTREAM_BASE = "https://trace-integ-test.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_config(
    *,
    trace_mode: str = "all",
    sample_rate: float = 1.0,
) -> AppConfig:
    import os

    os.environ.setdefault("TINT_TEST_KEY", "test-key-integ-000")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "TINT_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "integ-acct-a", "api_key_env": "TINT_TEST_KEY"},
            ],
            "dashboard": {"enabled": False},
            "routing": {
                "trace": {
                    "mode": trace_mode,
                    "sample_rate": sample_rate,
                    "include_score_components": False,
                }
            },
        }
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


async def _seed_accounts(db: Database) -> None:
    async with db.transaction():
        await db.execute_insert(
            "INSERT OR IGNORE INTO models (model_id, display_name, protocol) "
            "VALUES (?, ?, ?)",
            ("gpt-4", "gpt-4", "openai"),
        )
        await db.execute_insert(
            "INSERT OR IGNORE INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("integ-acct-a", "K_integ_a"),
        )
        acct = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", ("integ-acct-a",)
        )
        assert acct is not None
        await db.execute_insert(
            "INSERT OR IGNORE INTO account_models "
            "(account_id, model_id, enabled) VALUES (?, ?, 1)",
            (int(acct["id"]), "gpt-4"),
        )


async def _build_coordinator(
    config: AppConfig,
    db: Database,
    writer: RoutingTraceWriter | None = None,
) -> RequestCoordinator:
    registry = AccountRegistry(config)
    health_manager = HealthManager()
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    from eggpool.catalog.service import CatalogService

    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="gpt-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "integ-acct-a")

    router = Router(registry, catalog)
    router.set_account_weight("integ-acct-a", 1.0)

    return RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        routing_decision_repo=RoutingDecisionRepository(db),
        quota_estimator=None,
        health_manager=health_manager,
        config=config,
        routing_trace_writer=writer,
    )


async def _count_routing_traces(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM routing_decisions")
    assert row is not None
    return int(row["cnt"])


# ---------------------------------------------------------------------------
# Test 1: Trace DB writes never delay upstream send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_trace_db_delay_does_not_block_dispatch() -> None:
    """Acceptance #3: Even when trace writes are slow, dispatch completes
    without waiting for trace persistence.

    Proof: we intercept the trace writer's create_many to sleep 200ms
    (simulating slow DB), then verify dispatch p95 stays well under that.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    config = _build_config(trace_mode="all")
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db)

        writer = RoutingTraceWriter(
            db=db,
            routing_decision_repo=RoutingDecisionRepository(db),
            queue_capacity=1000,
            flush_interval_s=0.05,
            max_batch_size=50,
        )

        # Slow down the writer's DB write to simulate contention
        original_create_many = writer._repo.create_many

        async def _slow_create_many(rows: Any) -> int:
            await asyncio.sleep(0.2)  # 200ms simulated DB contention
            return await original_create_many(rows)

        writer._repo.create_many = _slow_create_many  # type: ignore[assignment]

        writer.start()
        try:
            coord = await _build_coordinator(config, db, writer=writer)

            # Run 10 dispatches — trace writes go to the slow writer
            timings: list[float] = []
            for i in range(10):
                ctx = _make_context(f"delay-test-{i}")
                start = time.perf_counter()
                await coord._select_and_persist_attempt(ctx, 1)
                elapsed_ms = (time.perf_counter() - start) * 1000
                timings.append(elapsed_ms)

            p95 = sorted(timings)[int(len(timings) * 0.95)]
            # Dispatch must not be delayed by trace writes.
            # If dispatch waited for trace, p95 would be >= 200ms.
            assert p95 < 100.0, (
                f"Dispatch p95 ({p95:.1f}ms) appears blocked by slow trace "
                f"writes (200ms simulated delay)"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Test 2: Writer survives rehash (10 rehashes) without duplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_writer_survives_ten_rehashes_without_duplication() -> None:
    """Acceptance #5: The process-owned writer survives 10 configure()
    calls (simulating rehash) without duplicating or losing events.
    """
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        # Seed enough FK rows for all 30 events (10 reloads x 3 events)
        async with db.transaction():
            for i in range(1, 35):
                await db.execute_insert(
                    "INSERT OR IGNORE INTO accounts "
                    "(name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, 1, 1.0)",
                    (f"rehash-acct-{i}", f"K_RH_{i}"),
                )
            await db.execute_insert(
                "INSERT OR IGNORE INTO models "
                "(model_id, display_name, protocol) "
                "VALUES ('gpt-4', 'gpt-4', 'openai')",
            )
            for i in range(1, 35):
                await db.execute_insert(
                    "INSERT OR IGNORE INTO requests "
                    "(id, account_id, model_id, started_at) "
                    "VALUES (?, 1, 'gpt-4', datetime('now'))",
                    (i,),
                )

        writer = RoutingTraceWriter(
            db=db,
            routing_decision_repo=RoutingDecisionRepository(db),
            queue_capacity=1000,
            flush_interval_s=100.0,  # won't drain naturally during the loop
            max_batch_size=50,
        )
        writer.configure(mode="all")
        writer.start()
        try:
            for reload_num in range(10):
                # Submit 3 events per reload
                for i in range(3):
                    idx = reload_num * 3 + i + 1
                    writer.submit(
                        RoutingTraceEvent(
                            request_id=f"rehash-{reload_num}-{i}",
                            db_request_id=idx,
                            attempt_number=1,
                            model_id="gpt-4",
                            provider_id=None,
                            protocol="openai",
                            selected_account_name="rehash-acct-1",
                            selected_account_id=1,
                            selected_tier=0,
                            selected_score=1.0,
                            eligible_count=2,
                            scored_count=2,
                            attempted_excluded_count=0,
                            top_score=1.0,
                            top_score_account_name="rehash-acct-1",
                            exclude_reasons_json="{}",
                            score_components_json=None,
                            created_at_mono_ns=time.monotonic_ns(),
                            created_at_epoch=time.time(),
                            generation_id=None,
                        )
                    )
                # Simulate rehash: reconfigure writer
                writer.configure(
                    mode="sampled" if reload_num % 2 == 0 else "all",
                    sample_rate=0.5,
                )

            # Now flush everything by stopping with a generous timeout
            await writer.stop(timeout_s=5.0)

            snap = writer.snapshot()
            assert snap["accepted"] == 30, (
                f"Expected 30 accepted, got {snap['accepted']}"
            )
            assert snap["written"] == 30, f"Expected 30 written, got {snap['written']}"

            count = await _count_routing_traces(db)
            assert count == 30, f"Expected 30 rows in DB, got {count}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Test 3: Writer crash/restart does not fail proxy requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_writer_crash_does_not_fail_dispatch() -> None:
    """Acceptance #3: If the writer crashes or is unavailable, dispatch
    still completes and returns a result to the caller.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    config = _build_config(trace_mode="all")
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db)

        writer = RoutingTraceWriter(
            db=db,
            routing_decision_repo=RoutingDecisionRepository(db),
            queue_capacity=1000,
            flush_interval_s=0.05,
            max_batch_size=50,
        )
        writer.configure(mode="all")
        writer.start()

        coord = await _build_coordinator(config, db, writer=writer)

        # Verify normal dispatch works
        ctx = _make_context("pre-crash-1")
        result = await coord._select_and_persist_attempt(ctx, 1)
        assert result is not None

        # Crash the writer (stop it abruptly without drain)
        writer._drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer._drain_task
        writer._state = "stopped"

        # Dispatch should still work with a dead writer
        ctx2 = _make_context("post-crash-1")
        result2 = await coord._select_and_persist_attempt(ctx2, 1)
        assert result2 is not None

        # The trace should be dropped (writer unavailable), not an error
        snap = writer.snapshot()
        assert snap["dropped_writer_unavailable"] >= 1
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Test 4: Stale parent rows are silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stale_parent_row_silently_skipped() -> None:
    """D6 acceptance: When the parent request row has been deleted by
    retention before the trace writer persists, the batch does not fail —
    the stale event is silently skipped.
    """
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        # Seed two request rows: one that persists, one that gets deleted
        async with db.transaction():
            await db.execute_insert(
                "INSERT OR IGNORE INTO models (model_id, display_name, protocol) "
                "VALUES ('gpt-4', 'gpt-4', 'openai')",
            )
            await db.execute_insert(
                "INSERT OR IGNORE INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                ("stale-acct", "K_STALE"),
            )
            # Request id=1 (will be deleted), id=2 (stays)
            await db.execute_insert(
                "INSERT OR IGNORE INTO requests (id, account_id, model_id, started_at) "
                "VALUES (1, 1, 'gpt-4', datetime('now'))",
            )
            await db.execute_insert(
                "INSERT OR IGNORE INTO requests (id, account_id, model_id, started_at) "
                "VALUES (2, 1, 'gpt-4', datetime('now'))",
            )

        writer = RoutingTraceWriter(
            db=db,
            routing_decision_repo=RoutingDecisionRepository(db),
            queue_capacity=1000,
            flush_interval_s=0.05,
            max_batch_size=50,
        )
        writer.configure(mode="all")
        writer.start()
        try:
            # Write an event for request_id=2 (will survive cascade delete)
            writer.submit(
                RoutingTraceEvent(
                    request_id="stale-survivor",
                    db_request_id=2,
                    attempt_number=1,
                    model_id="gpt-4",
                    provider_id=None,
                    protocol="openai",
                    selected_account_name="stale-acct",
                    selected_account_id=1,
                    selected_tier=0,
                    selected_score=1.0,
                    eligible_count=1,
                    scored_count=1,
                    attempted_excluded_count=0,
                    top_score=1.0,
                    top_score_account_name="stale-acct",
                    exclude_reasons_json="{}",
                    score_components_json=None,
                    created_at_mono_ns=time.monotonic_ns(),
                    created_at_epoch=time.time(),
                    generation_id=None,
                )
            )
            await asyncio.sleep(0.3)
            count_ok = await _count_routing_traces(db)
            assert count_ok == 1

            # Delete request id=1 (not the one we wrote, but the other one)
            # This tests that FK violations on INSERT are handled
            async with db.transaction():
                await db.execute_write("DELETE FROM requests WHERE id = 1")

            # Submit an event for the now-deleted request_id=1
            writer.submit(
                RoutingTraceEvent(
                    request_id="stale-orphan",
                    db_request_id=1,
                    attempt_number=1,
                    model_id="gpt-4",
                    provider_id=None,
                    protocol="openai",
                    selected_account_name="stale-acct",
                    selected_account_id=1,
                    selected_tier=0,
                    selected_score=1.0,
                    eligible_count=1,
                    scored_count=1,
                    attempted_excluded_count=0,
                    top_score=1.0,
                    top_score_account_name="stale-acct",
                    exclude_reasons_json="{}",
                    score_components_json=None,
                    created_at_mono_ns=time.monotonic_ns(),
                    created_at_epoch=time.time(),
                    generation_id=None,
                )
            )
            await asyncio.sleep(0.3)

            # The orphan event should be silently skipped (FK violation)
            # and not crash the batch. The surviving event for request_id=2
            # should still be in the DB.
            count_after = await _count_routing_traces(db)
            assert count_after == 1, (
                f"Stale parent should be silently skipped, got {count_after} rows"
            )

            # No flush error — the batch completed successfully
            snap = writer.snapshot()
            assert snap["dropped_flush_error"] == 0
        finally:
            await writer.stop()
    finally:
        await db.disconnect()
