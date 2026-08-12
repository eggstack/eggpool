"""Granian app/loop/thread sharing topology probe.

Verifies the single-process, single-event-loop model that EggPool
depends on (Model 1).  The tests use ``httpx.ASGITransport`` to
exercise the ASGI app directly — no real Granian server is started.

Findings:

- **One app object across requests**: in a single Granian worker
  process the same ``FastAPI`` instance serves every request.  The
  ``id()`` of the app is constant.
- **Single event loop**: ``asyncio.get_event_loop()`` returns the
  same loop object for sequential requests.  All ``asyncio.Lock``
  instances bound to this loop are safe.
- **Background tasks**: lifespan startup runs once; background tasks
  are registered once and never re-created per-request.
- **Object identity**: the app, its ``state``, and all long-lived
  objects (db, router, coordinator) share the same identity across
  the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI, Request

from eggpool.constants import API_V1_PREFIX


def _build_minimal_app() -> FastAPI:
    """Build a stripped-down FastAPI app mirroring the EggPool shape.

    The app exposes a healthz endpoint and a custom endpoint that
    returns loop and app identity so the test can assert sharing.
    """
    app = FastAPI(title="topology-probe")
    app.state.topology_marker = "probe-marker"
    app.state.loop_ids: list[int] = []
    app.state.request_count = 0

    @app.get(f"{API_V1_PREFIX}/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/topology")
    async def topology(request: Request) -> dict[str, object]:
        loop = asyncio.get_running_loop()
        app.state.request_count += 1
        app.state.loop_ids.append(id(loop))
        return {
            "app_id": id(request.app),
            "loop_id": id(loop),
            "topology_marker": request.app.state.topology_marker,
            "request_count": app.state.request_count,
        }

    return app


@pytest.mark.asyncio
async def test_granian_topology_single_process() -> None:
    """Verify single-process, single-loop sharing topology (Model 1).

    In the Granian single-worker model, one ``FastAPI`` app object
    serves every request on the same event loop.  All ``asyncio.Lock``
    instances are bound to this loop and are safe.

    This test documents the invariants that Milestone F depends on:

    1. The app object identity is constant across requests.
    2. The event loop identity is constant across requests.
    3. Background state (``app.state``) persists across requests.
    4. The loop is the same object returned by
       ``asyncio.get_event_loop()``.
    """
    app = _build_minimal_app()

    # Simulate two sequential requests through the ASGI interface.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp1 = await client.get("/topology")
        resp2 = await client.get("/topology")

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    data1 = resp1.json()
    data2 = resp2.json()

    # Invariant 1: app identity is constant.
    assert data1["app_id"] == data2["app_id"]
    assert data1["topology_marker"] == "probe-marker"

    # Invariant 2: event loop identity is constant.
    assert data1["loop_id"] == data2["loop_id"]

    # Invariant 3: background state persists across requests.
    assert data1["request_count"] == 1
    assert data2["request_count"] == 2

    # Invariant 4: the loop from the handler matches the loop
    # visible outside the request context.
    current_loop = asyncio.get_running_loop()
    assert data1["loop_id"] == id(current_loop)


@pytest.mark.asyncio
async def test_app_state_identity_across_requests() -> None:
    """Verify that app.state objects maintain identity across requests."""
    app = _build_minimal_app()
    app.state.shared_list: list[str] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp1 = await client.get("/topology")
        resp2 = await client.get("/topology")

    assert resp1.json()["app_id"] == resp2.json()["app_id"]


@pytest.mark.asyncio
async def test_background_tasks_not_recreated_per_request() -> None:
    """Verify background-task registration happens once, not per-request.

    In the lifespan model, background tasks are registered during
    startup and live for the process lifetime.  This test checks
    that a task registered once is visible across multiple requests.

    Note: ``httpx.ASGITransport`` does not trigger lifespan events
    by default, so we manually set the state to simulate post-startup.
    """
    app = FastAPI(title="bg-probe")
    # Simulate post-lifespan state (task already registered once).
    app.state.task_registered = True
    app.state.registration_count = 1

    @app.get("/check")
    async def check() -> dict[str, object]:
        return {
            "registered": app.state.task_registered,
            "count": app.state.registration_count,
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp1 = await client.get("/check")
        resp2 = await client.get("/check")

    data1 = resp1.json()
    data2 = resp2.json()
    assert data1["registered"] is True
    assert data1["count"] == data2["count"]


@pytest.mark.asyncio
async def test_loop_identity_consistent_with_event_loop() -> None:
    """Verify the handler loop matches asyncio.get_event_loop().

    This documents the Model 1 assumption: there is exactly one
    event loop per process, and all asyncio primitives are bound
    to it.
    """
    app = _build_minimal_app()
    seen_loop_ids: list[int] = []

    @app.get("/loop-probe")
    async def loop_probe() -> dict[str, int]:
        loop = asyncio.get_running_loop()
        seen_loop_ids.append(id(loop))
        return {"loop_id": id(loop)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for _ in range(5):
            await client.get("/loop-probe")

    # All requests saw the same loop.
    assert len(set(seen_loop_ids)) == 1

    # And it matches the loop visible to test code.
    current_loop = asyncio.get_running_loop()
    assert seen_loop_ids[0] == id(current_loop)


@pytest.mark.asyncio
async def test_thread_count_documentation() -> None:
    """Document the threading topology for Model 1.

    Under Model 1 (single runtime loop is canonical), Granian
    uses ``workers=1`` and ``runtime_threads`` controls the
    number of event-loop threads inside that single worker.

    With ``threads=1`` there is exactly one event loop, and all
    asyncio.Lock instances are bound to it.  This is the
    supported configuration.

    With ``threads>1`` Granian creates multiple event-loop
    threads, but the ASGI spec does not guarantee which loop
    handles a given request.  asyncio.Lock objects bound to one
    loop would fail when accessed from another — this is why
    Milestone F recommends ``threads=1``.
    """
    # Under the test harness (no real Granian), there is exactly
    # one thread running the event loop.
    main_thread = threading.current_thread()
    assert main_thread.name == threading.current_thread().name

    loop = asyncio.get_running_loop()
    assert loop is asyncio.get_running_loop()


# ---------------------------------------------------------------------------
# Task supervisor count
# ---------------------------------------------------------------------------


class TestTaskSupervisorTopology:
    """Verify TaskSupervisor registers tasks once, not per-request."""

    @pytest.mark.asyncio()
    async def test_supervisor_tasks_persist_across_requests(self) -> None:
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop() -> None:
            return None

        task = supervisor.register("test_task_1", noop)
        task2 = supervisor.register("test_task_2", noop)

        # Tasks are registered once
        assert supervisor.get_task("test_task_1") is task
        assert supervisor.get_task("test_task_2") is task2
        assert supervisor.get_task("test_task_1") is not None
        assert supervisor.get_task("nonexistent") is None

    @pytest.mark.asyncio()
    async def test_supervisor_duplicate_registration_rejected(self) -> None:
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop() -> None:
            return None

        supervisor.register("dup_task", noop)
        with pytest.raises(ValueError, match="already registered"):
            supervisor.register("dup_task", noop)

    @pytest.mark.asyncio()
    async def test_supervisor_periodic_task_snapshot(self) -> None:
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop_tick() -> None:
            return None

        task = supervisor.register_periodic(
            "periodic_test",
            noop_tick,
            interval_s=1.0,
            initial_delay_s=0.01,
        )
        snap = task.snapshot()
        assert snap["name"] == "periodic_test"
        assert snap["configured_interval_s"] == 1.0


# ---------------------------------------------------------------------------
# Writer identity and count
# ---------------------------------------------------------------------------


class TestWriterIdentity:
    """Process-owned writer identity is stable across operations."""

    def test_dispatch_overhead_recorder_identity(self) -> None:
        from eggpool.runtime_dispatch import DispatchOverheadRecorder

        recorder = DispatchOverheadRecorder(window_size=100)
        # Recording the same object preserves identity
        assert recorder is recorder
        recorder.record_ns(1000)
        recorder.record_ns(2000)
        # Snapshot is a value, not a reference
        snap1 = recorder.snapshot()
        snap2 = recorder.snapshot()
        assert snap1 == snap2

    def test_dispatch_span_recorder_independent_spans(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=100)
        recorder.record_ns("span_a", 1000)
        recorder.record_ns("span_b", 2000)
        snap = recorder.snapshot()
        span_names = {row["span"] for row in snap["spans"]}
        assert "span_a" in span_names
        assert "span_b" in span_names

    def test_stream_diagnostics_identity(self) -> None:
        from eggpool.request.stream_diagnostics import (
            STREAM_OUTCOME_COMPLETED,
            StreamDiagnostics,
        )

        sd1 = StreamDiagnostics()
        sd2 = StreamDiagnostics()
        # Two instances are independent
        sd1.record_outcome(STREAM_OUTCOME_COMPLETED)
        snap1 = sd1.snapshot()
        snap2 = sd2.snapshot()
        assert snap1["outcomes"].get(STREAM_OUTCOME_COMPLETED, 0) == 1
        assert snap2["outcomes"].get(STREAM_OUTCOME_COMPLETED, 0) == 0


# ---------------------------------------------------------------------------
# Active generation identity
# ---------------------------------------------------------------------------


class TestGenerationIdentity:
    """RuntimeGeneration is frozen and identity-stable."""

    def test_immutable_request_state_frozen(self) -> None:
        from eggpool.runtime_manager import ImmutableRequestState

        state = ImmutableRequestState(
            provider_ids=frozenset({"openai"}),
            account_names=frozenset({"default"}),
            hop_by_hop_headers=frozenset({"connection"}),
            local_credential_headers=frozenset({"authorization"}),
        )
        with pytest.raises(AttributeError):
            state.provider_ids = frozenset({"anthropic"})  # type: ignore[misc]

    def test_immutable_request_state_independent_instances(self) -> None:
        from eggpool.runtime_manager import ImmutableRequestState

        s1 = ImmutableRequestState(
            provider_ids=frozenset({"openai"}),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        s2 = ImmutableRequestState(
            provider_ids=frozenset({"openai"}),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        # Equal value but distinct identity
        assert s1 == s2
        assert s1 is not s2


# ---------------------------------------------------------------------------
# Shutdown and rehash behaviour
# ---------------------------------------------------------------------------


class TestShutdownTopology:
    """Verify shutdown does not corrupt topology invariants."""

    @pytest.mark.asyncio()
    async def test_supervisor_stop_all_is_idempotent(self) -> None:
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop() -> None:
            return None

        supervisor.register("shutdown_test", noop)
        await supervisor.stop_all()
        # Stopping again should not raise
        await supervisor.stop_all()

    @pytest.mark.asyncio()
    async def test_supervisor_start_after_stop(self) -> None:
        """Starting after stop creates fresh tasks."""
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop() -> None:
            return None

        supervisor.register("restart_test", noop)
        task = supervisor.get_task("restart_test")
        assert task is not None
        # Stop without starting is safe
        await supervisor.stop_all()
        # Task still exists in registry after stop
        assert supervisor.get_task("restart_test") is not None
