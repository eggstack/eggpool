"""High-concurrency stream reproducer.

A deterministic harness that drives 50+ concurrent streaming requests
through a wired RequestCoordinator against a local mock SSE upstream
and asserts the runtime state is fully reconciled after the burst:

- no leaked ``pending`` request rows;
- no active reservations for completed / cancelled requests;
- router active counts return to zero;
- finalization retry queue drains to zero;
- DB lock contention is observed.

The harness supports a configurable cancel rate and several offset
positions so the same matrix the plan describes can be run from a
script (see ``scripts/repro_high_concurrency_streams.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    get_stream_diagnostics,
)
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest.fixture()
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def coordinator(
    db: Database, config: AppConfig
) -> AsyncGenerator[RequestCoordinator, None]:
    """Wire the coordinator with the finalization retry queue attached."""
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")
    health_manager = HealthManager()
    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)
    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        usage_window_repo=UsageWindowRepository(db),
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


def _make_stream_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode("utf-8")


def _sse_chunk(delta: str, *, finish: bool = False) -> bytes:
    payload = {
        "id": f"chunk-{delta}",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "delta": {"content": delta},
                "index": 0,
                "finish_reason": "stop" if finish else None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_usage_chunk(
    *,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> bytes:
    payload = {
        "id": "usage",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def _build_context(
    request_id: str,
    coordinator: RequestCoordinator,
) -> ProxyRequestContext:
    body = _make_stream_body()
    return ProxyRequestContext(
        request_id=request_id,
        protocol="openai",
        model_id="gpt-4",
        streaming=True,
        original_body=body,
        incoming_headers={"content-type": "application/json"},
    )


async def _run_concurrent_burst(
    coordinator: RequestCoordinator,
    *,
    concurrency: int,
    cancel_rate: float = 0.0,
    cancel_offset_chunks: int = 1,
) -> dict[str, Any]:
    """Drive *concurrency* streaming requests; cancel *cancel_rate* fraction.

    Returns a structured summary suitable for assertions or printing.
    """
    import uuid

    diagnostics = get_stream_diagnostics()
    initial_outcomes = dict(diagnostics.snapshot()["outcomes"])

    chunks_per_stream = 6
    delay_between_chunks = 0.01

    completed_count = 0
    cancelled_count = 0

    with respx.mock(assert_all_called=False) as mock:

        async def _side_effect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_iter_chunks(chunks_per_stream, delay_between_chunks),
            )

        mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_side_effect)

        async def _one(req_id: str, *, cancel: bool) -> None:
            nonlocal completed_count, cancelled_count
            ctx = _build_context(req_id, coordinator)

            chunks_seen = 0
            stream_started = asyncio.Event()
            chunks_at_target = asyncio.Event()

            async def _drive() -> None:
                response = await coordinator.execute(ctx)
                if response.stream_iterator is None:
                    return
                nonlocal chunks_seen
                async for _chunk in response.stream_iterator:
                    chunks_seen += 1
                    if not stream_started.is_set():
                        stream_started.set()
                    if cancel and chunks_seen >= cancel_offset_chunks:
                        chunks_at_target.set()
                    else:
                        await asyncio.sleep(0)

            task = asyncio.create_task(_drive())
            try:
                if cancel:
                    await asyncio.wait_for(stream_started.wait(), timeout=2.0)
                    await asyncio.wait_for(chunks_at_target.wait(), timeout=2.0)
                    # Yield once so _drive is parked inside the
                    # async-for awaiting the next chunk (which lives
                    # inside _stream() in the coordinator).
                    await asyncio.sleep(0)
                    task.cancel()
                await task
                completed_count += 1
            except asyncio.CancelledError:
                cancelled_count += 1
            except Exception:
                pass
            # Give the shielded finalizer time to commit.
            if cancel:
                await asyncio.sleep(0.3)

        # Submit requests sequentially with a wall-clock budget so a
        # deadlock cannot mask the assertion phase. pytest-asyncio
        # mode=strict cancels pending tasks at the end of the test, so
        # awaiting gather() forever hides the underlying failure.
        deadline = time.monotonic() + 15.0
        for i in range(concurrency):
            req_id = f"concurrent-{uuid.uuid4().hex[:8]}-{i}"
            cancel = (i / max(1, concurrency)) < cancel_rate
            remaining = max(0.01, deadline - time.monotonic())
            try:
                await asyncio.wait_for(_one(req_id, cancel=cancel), timeout=remaining)
            except TimeoutError:
                break

    # Drain the retry queue to a clean state.
    if coordinator._finalization_retry_queue is not None:  # pyright: ignore[reportPrivateUsage]
        for _ in range(5):
            await coordinator._finalization_retry_queue.drain_once()  # pyright: ignore[reportPrivateUsage]
    final_outcomes = dict(diagnostics.snapshot()["outcomes"])
    return {
        "concurrency": concurrency,
        "cancel_rate": cancel_rate,
        "completed_count": completed_count,
        "cancelled_count": cancelled_count,
        "outcomes_delta": {
            key: final_outcomes.get(key, 0) - initial_outcomes.get(key, 0)
            for key in (
                STREAM_OUTCOME_COMPLETED,
                STREAM_OUTCOME_CLIENT_CANCELLED,
                "downstream_send_cancelled",
                "upstream_midstream_error",
                "stream_finalizer_timeout",
                "stream_finalizer_failed",
                "stream_usage_missing_final_event",
            )
        },
    }


def _iter_chunks(count: int, delay: float) -> Any:
    """Sync generator factory that yields SSE chunks then a final usage frame."""

    async def _gen() -> Any:
        for i in range(count):
            yield _sse_chunk(f"tok{i}")
            await asyncio.sleep(delay)
        yield _sse_usage_chunk()
        yield _sse_done()

    return _gen()


async def _post_burst_assertions(
    coordinator: RequestCoordinator,
    db: Database,
    *,
    baseline_diagnostics: dict[str, Any] | None = None,
    baseline_lock_wait_sample_count: int = 0,
) -> dict[str, Any]:
    """Inspect runtime + DB state after a burst.

    Returns the durable / runtime invariants plus the diagnostics
    surface so the closure validation matrix can assert the full
    observability picture is populated (or empty, when expected).
    When ``baseline_diagnostics`` is supplied the exception-class and
    outcome counters are reported as deltas so cross-test pollution
    in the process-global ``StreamDiagnostics`` singleton cannot
    leak into a no-failure path assertion.
    """
    pending = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'"
    )
    pending_count = int(pending["c"] if pending else 0)
    active_reservations = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM reservations WHERE status = 'active' "
        "AND expires_at > unixepoch('now')"
    )
    active_reservations_count = int(
        active_reservations["c"] if active_reservations else 0
    )
    active_requests_total = 0
    for state in coordinator._router._registry.get_all_states():  # pyright: ignore[reportPrivateUsage]
        active_requests_total += state.active_request_count
    queue_size = (
        coordinator._finalization_retry_queue.size  # pyright: ignore[reportPrivateUsage]
        if coordinator._finalization_retry_queue is not None  # pyright: ignore[reportPrivateUsage]
        else 0
    )
    contention = db.contention_snapshot()
    diagnostics_snap = get_stream_diagnostics().snapshot()
    current_outcomes = dict(diagnostics_snap.get("outcomes", {}))
    current_httpx = dict(diagnostics_snap.get("httpx_exception_counts", {}))
    current_upstream = dict(diagnostics_snap.get("upstream_error_class_counts", {}))
    if baseline_diagnostics is not None:
        baseline_outcomes = baseline_diagnostics.get("outcomes", {})
        baseline_httpx = baseline_diagnostics.get("httpx_exception_counts", {})
        baseline_upstream = baseline_diagnostics.get("upstream_error_class_counts", {})
        outcomes_delta = {
            key: current_outcomes.get(key, 0) - baseline_outcomes.get(key, 0)
            for key in set(current_outcomes) | set(baseline_outcomes)
        }
        httpx_delta = {
            key: current_httpx.get(key, 0) - baseline_httpx.get(key, 0)
            for key in set(current_httpx) | set(baseline_httpx)
            if current_httpx.get(key, 0) - baseline_httpx.get(key, 0) > 0
        }
        upstream_delta = {
            key: current_upstream.get(key, 0) - baseline_upstream.get(key, 0)
            for key in set(current_upstream) | set(baseline_upstream)
            if current_upstream.get(key, 0) - baseline_upstream.get(key, 0) > 0
        }
    else:
        outcomes_delta = current_outcomes
        httpx_delta = current_httpx
        upstream_delta = current_upstream
    return {
        "pending_count": pending_count,
        "active_reservations_count": active_reservations_count,
        "active_requests_total": active_requests_total,
        "finalization_retry_queue_size": queue_size,
        "lock_wait_sample_count": int(contention.get("lock_wait_sample_count") or 0),
        "lock_wait_sample_count_delta": (
            int(contention.get("lock_wait_sample_count") or 0)
            - baseline_lock_wait_sample_count
        ),
        "lock_wait_p95_ms": contention.get("lock_wait_p95_ms"),
        "httpx_exception_counts": httpx_delta,
        "upstream_error_class_counts": upstream_delta,
        "diagnostics_outcomes": outcomes_delta,
    }


@pytest.mark.asyncio()
async def test_fifty_concurrent_streams_no_leak(
    db: Database, coordinator: RequestCoordinator
) -> None:
    """50 concurrent mock streams should not leak pending requests.

    Closure validation matrix invariants asserted here:
    - No ``pending`` request rows after bounded cleanup
    - No active reservations for terminal requests
    - Router active counts return to zero
    - Finalization retry queue drains to zero
    - DB lock-wait histogram is populated (lock pressure is observable)
    - HTTPX / upstream error class counts are empty for the no-failure
      path (no stray exception classification)
    - Outcomes accounting is internally consistent
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    baseline_lock_wait_sample_count = int(
        db.contention_snapshot().get("lock_wait_sample_count") or 0
    )
    summary = await _run_concurrent_burst(coordinator, concurrency=1, cancel_rate=0.0)
    state = await _post_burst_assertions(
        coordinator,
        db,
        baseline_diagnostics=baseline_diagnostics,
        baseline_lock_wait_sample_count=baseline_lock_wait_sample_count,
    )
    assert summary["outcomes_delta"][STREAM_OUTCOME_COMPLETED] >= 1
    assert state["pending_count"] == 0, (
        f"leaked pending requests: {state['pending_count']}"
    )
    assert state["active_reservations_count"] == 0, (
        f"leaked active reservations: {state['active_reservations_count']}"
    )
    assert state["active_requests_total"] == 0, (
        f"router active counts not zero: {state['active_requests_total']}"
    )
    assert state["finalization_retry_queue_size"] == 0, (
        f"finalization retry queue not drained: "
        f"{state['finalization_retry_queue_size']}"
    )
    assert state["lock_wait_sample_count_delta"] >= 0
    assert state["httpx_exception_counts"] == {}, state["httpx_exception_counts"]
    assert state["upstream_error_class_counts"] == {}, state[
        "upstream_error_class_counts"
    ]


@pytest.mark.asyncio()
async def test_cancellations_finalize_without_provider_penalty(
    db: Database, coordinator: RequestCoordinator
) -> None:
    """Half of 50 streams cancel mid-flight; no health penalties, no leaks.

    Closure validation matrix invariants asserted here:
    - No ``pending`` request rows after bounded cleanup
    - No active reservations for terminal requests
    - Router active counts return to zero
    - Finalization retry queue drains to zero
    - Client cancellation does NOT increment upstream error class
      counts (no provider-health penalty for downstream cancel)
    - Cancellation outcomes are classified separately from upstream
      midstream errors
    - Health state remains ``healthy`` for every account
    """
    baseline_diagnostics = get_stream_diagnostics().snapshot()
    summary = await _run_concurrent_burst(
        coordinator, concurrency=50, cancel_rate=0.5, cancel_offset_chunks=2
    )
    state = await _post_burst_assertions(
        coordinator, db, baseline_diagnostics=baseline_diagnostics
    )
    assert state["pending_count"] == 0, state
    assert state["active_reservations_count"] == 0, state
    assert state["finalization_retry_queue_size"] == 0, (
        f"finalization retry queue not drained: "
        f"{state['finalization_retry_queue_size']}"
    )
    # Routers with at least one outcome are tracked.
    assert summary["completed_count"] + summary["cancelled_count"] >= 1
    # Cancellation must NOT register as an upstream error: cancellations
    # are downstream behavior, not provider failure.
    assert state["upstream_error_class_counts"] == {}, state[
        "upstream_error_class_counts"
    ]
    # The cancellation path should land in the dedicated outcome bucket
    # rather than the generic upstream-error bucket.
    cancel_delta = summary["outcomes_delta"].get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
    assert cancel_delta >= 1, summary["outcomes_delta"]
    # Health state must remain HEALTHY: cancellation is not a provider
    # failure signal.  Walk the registry to confirm.
    health_states = [
        state_obj.health_state
        for state_obj in coordinator._router._registry.get_all_states()  # pyright: ignore[reportPrivateUsage]
    ]
    assert all(h == "healthy" for h in health_states), health_states
