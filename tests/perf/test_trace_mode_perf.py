"""Performance comparison tests for routing trace modes.

Validates acceptance criterion #11: Under slow trace flush, dispatch
p95/p99 remain near trace-off behavior within benchmark tolerance.

Compares dispatch overhead across trace modes: all, sampled, off.
Uses in-memory SQLite and respx-mocked upstream for deterministic timing.

Run with::

    uv run pytest tests/perf/test_trace_mode_perf.py -m performance -v
"""

from __future__ import annotations

import asyncio
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
from eggpool.observability.routing_trace_writer import (
    RoutingTraceEvent,
    RoutingTraceWriter,
)
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

pytestmark = pytest.mark.performance

UPSTREAM_BASE = "https://trace-perf-test.example.com"


def _build_config(
    *,
    trace_mode: str = "off",
    sample_rate: float = 0.05,
) -> Any:
    import os

    from eggpool.models.config import AppConfig

    os.environ.setdefault("TTEST_KEY", "test-key-000")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "TTEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "perf-acct-a", "api_key_env": "TTEST_KEY"},
                {"name": "perf-acct-b", "api_key_env": "TTEST_KEY"},
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
        for name in ["perf-acct-a", "perf-acct-b"]:
            await db.execute_insert(
                "INSERT OR IGNORE INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                (name, f"K_{name}"),
            )
            acct = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", (name,))
            assert acct is not None
            await db.execute_insert(
                "INSERT OR IGNORE INTO account_models "
                "(account_id, model_id, enabled) VALUES (?, ?, 1)",
                (int(acct["id"]), "gpt-4"),
            )


async def _build_coordinator(
    config: Any,
    db: Database,
    writer: RoutingTraceWriter | None = None,
) -> RequestCoordinator:
    from eggpool.health.health_manager import HealthManager
    from eggpool.routing.router import Router

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
    for acct in ["perf-acct-a", "perf-acct-b"]:
        catalog.cache.add_account_support("gpt-4", acct)

    router = Router(registry, catalog)
    router.set_account_weight("perf-acct-a", 1.0)
    router.set_account_weight("perf-acct-b", 1.0)

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


async def _run_dispatches(
    coordinator: RequestCoordinator,
    count: int,
) -> list[float]:
    """Run *count* dispatches and return per-dispatch elapsed times in ms."""
    timings: list[float] = []
    for i in range(count):
        ctx = _make_context(f"perf-trace-{i}")
        start = time.perf_counter()
        await coordinator._select_and_persist_attempt(ctx, 1)
        elapsed_ms = (time.perf_counter() - start) * 1000
        timings.append(elapsed_ms)
    return timings


def _percentile(data: list[float], pct: float) -> float:
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100.0)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_trace_mode_all_baseline() -> None:
    """Baseline: trace mode 'all' records every dispatch."""
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
        writer.start()
        try:
            coord = await _build_coordinator(config, db, writer=writer)
            timings = await _run_dispatches(coord, count=20)
            await asyncio.sleep(0.2)  # let writer drain

            p50 = _percentile(timings, 50)
            p95 = _percentile(timings, 95)
            # Sanity: timings should be reasonable (in-memory, no real upstream)
            assert p95 < 500.0, f"p95={p95:.1f}ms too high for in-memory dispatch"
            assert p50 < 200.0, f"p50={p50:.1f}ms too high for in-memory dispatch"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
@respx.mock
async def test_trace_mode_off_baseline() -> None:
    """Baseline: trace mode 'off' — no trace writes at all."""
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    config = _build_config(trace_mode="off")
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
        writer.start()
        try:
            coord = await _build_coordinator(config, db, writer=writer)
            timings = await _run_dispatches(coord, count=20)
            await asyncio.sleep(0.2)

            p50 = _percentile(timings, 50)
            p95 = _percentile(timings, 95)
            assert p95 < 500.0, f"p95={p95:.1f}ms too high"
            assert p50 < 200.0, f"p50={p50:.1f}ms too high"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
@respx.mock
async def test_trace_mode_off_vs_all_no_regressions() -> None:
    """Dispatch p95 in trace-all mode must not regress significantly vs trace-off.

    This is the key acceptance criterion: trace writes must not add
    measurable overhead to the synchronous dispatch path.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    async def _collect_timings(mode: str) -> list[float]:
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
            writer.start()
            try:
                config = _build_config(trace_mode=mode)
                coord = await _build_coordinator(config, db, writer=writer)
                return await _run_dispatches(coord, count=30)
            finally:
                await writer.stop()
        finally:
            await db.disconnect()

    timings_off = await _collect_timings("off")
    timings_all = await _collect_timings("all")

    p95_off = _percentile(timings_off, 95)
    p95_all = _percentile(timings_all, 95)

    # Allow up to 2x regression — this is a generous bound for in-memory tests.
    # In production, the async writer means zero synchronous overhead.
    regression_ratio = p95_all / max(p95_off, 0.01)
    assert regression_ratio < 2.0, (
        f"trace-all p95 ({p95_all:.1f}ms) regressed >2x vs trace-off p95 "
        f"({p95_off:.1f}ms), ratio={regression_ratio:.2f}"
    )


# ---------------------------------------------------------------------------
# Score components on/off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_score_components_on_off_no_regression() -> None:
    """Dispatch p95 with score_components=True must not regress vs False.

    Score components add JSON serialization overhead per trace event;
    this test ensures the overhead is bounded.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    async def _collect_timings(include_sc: bool) -> list[float]:
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
            writer.start()
            try:
                config = _build_config(trace_mode="all")
                config.routing.trace.include_score_components = include_sc
                coord = await _build_coordinator(config, db, writer=writer)
                return await _run_dispatches(coord, count=30)
            finally:
                await writer.stop()
        finally:
            await db.disconnect()

    timings_off = await _collect_timings(include_sc=False)
    timings_on = await _collect_timings(include_sc=True)

    p95_off = _percentile(timings_off, 95)
    p95_on = _percentile(timings_on, 95)

    # Allow up to 1.5x — score components add modest JSON serialization
    regression_ratio = p95_on / max(p95_off, 0.01)
    assert regression_ratio < 1.5, (
        f"score-components-on p95 ({p95_on:.1f}ms) regressed >1.5x vs off "
        f"({p95_off:.1f}ms), ratio={regression_ratio:.2f}"
    )


# ---------------------------------------------------------------------------
# Queue near capacity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_queue_near_capacity_no_dispatch_regression() -> None:
    """Dispatch p95 remains stable when the trace writer queue is near capacity.

    When the queue is nearly full, submit() must not block dispatch.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_accounts(db)
        # Small queue, slow drain — fills up quickly
        writer = RoutingTraceWriter(
            db=db,
            routing_decision_repo=RoutingDecisionRepository(db),
            queue_capacity=10,
            flush_interval_s=100.0,  # won't drain during test
            max_batch_size=5,
        )
        writer.start()
        try:
            config = _build_config(trace_mode="all")
            coord = await _build_coordinator(config, db, writer=writer)

            # Fill the queue to capacity
            for i in range(10):
                writer.submit(
                    RoutingTraceEvent(
                        request_id=f"fill-{i}",
                        db_request_id=i + 1,
                        attempt_number=1,
                        model_id="gpt-4",
                        provider_id=None,
                        protocol="openai",
                        selected_account_name="perf-acct-a",
                        selected_account_id=1,
                        selected_tier=0,
                        selected_score=1.0,
                        eligible_count=2,
                        scored_count=2,
                        attempted_excluded_count=0,
                        top_score=1.0,
                        top_score_account_name="perf-acct-a",
                        exclude_reasons_json="{}",
                        score_components_json=None,
                        created_at_mono_ns=0,
                        created_at_epoch=0.0,
                        generation_id=None,
                    )
                )

            snap = writer.snapshot()
            assert snap["queue_depth"] >= 8, (
                f"Queue should be near capacity, got depth={snap['queue_depth']}"
            )

            # Now run dispatches — submit() will get dropped_queue_full
            # but dispatch must not be delayed
            timings = await _run_dispatches(coord, count=20)

            p95 = _percentile(timings, 95)
            assert p95 < 500.0, (
                f"Dispatch p95 ({p95:.1f}ms) too high with queue near capacity"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


# ---------------------------------------------------------------------------
# Slow DB flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@respx.mock
async def test_slow_db_flush_no_dispatch_regression() -> None:
    """Dispatch p95 remains stable when the trace writer's DB flush is slow.

    This is the key acceptance criterion #11: under slow trace flush,
    dispatch p95/p99 remain near trace-off behavior.
    """
    respx.post(f"{UPSTREAM_BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

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
        # Inject 50ms delay into every DB write
        original_create_many = writer._repo.create_many

        async def _slow_create_many(rows: Any) -> int:
            await asyncio.sleep(0.05)
            return await original_create_many(rows)

        writer._repo.create_many = _slow_create_many  # type: ignore[assignment]
        writer.start()
        try:
            config = _build_config(trace_mode="all")
            coord = await _build_coordinator(config, db, writer=writer)
            timings = await _run_dispatches(coord, count=30)
            await asyncio.sleep(0.3)

            p95 = _percentile(timings, 95)
            # If dispatch waited for the slow flush, p95 would be >= 50ms per dispatch.
            assert p95 < 100.0, (
                f"Dispatch p95 ({p95:.1f}ms) appears blocked by slow DB flush "
                f"(50ms injected delay)"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()
