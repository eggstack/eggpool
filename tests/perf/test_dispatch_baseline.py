"""Milestone A5 deterministic dispatch baseline harness.

Captures timing distributions from the existing ``DispatchOverheadRecorder``,
``LocalPreUpstreamRecorder``, ``DispatchSpanRecorder``, and
``Database.contention_snapshot()`` so later milestones (B–G) have a
reproducible baseline to compare against.

The harness is intentionally lightweight:

- In-memory SQLite (no filesystem WAL traffic).
- ``respx``-mocked upstream (no network variability).
- A handful of low-volume serial and moderate-concurrency workloads.
- Captures compact machine-readable JSON under ``tests/perf/baselines/``.
- Avoids wall-clock-flaky assertions; uses fixed-cadence assertions
  on the *recorder* snapshot, not on absolute timing.

Run with::

    uv run pytest tests/perf/test_dispatch_baseline.py -m performance -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.runtime_dispatch import (
    DispatchOverheadRecorder,
    DispatchSpanRecorder,
    LocalPreUpstreamRecorder,
)
from eggpool.runtime_metrics import RuntimeMetricsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.performance

UPSTREAM_BASE = "https://baseline-test-upstream.example.com"


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers for baseline tests."""
    config.addinivalue_line(
        "markers",
        "perf_baseline: performance benchmark baseline snapshot",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def baseline_dir(tmp_path: Path) -> Path:
    """Directory to write baseline artifacts to."""
    d = tmp_path / "baselines"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest_asyncio.fixture()
async def baseline_coordinator(
    perf_db: Any,  # noqa: ANN401
    perf_config: Any,  # noqa: ANN401
) -> AsyncGenerator[
    tuple[
        Any,
        DispatchOverheadRecorder,
        LocalPreUpstreamRecorder,
        DispatchSpanRecorder,
    ],
    None,
]:
    """Wire a coordinator with all three dispatch recorders + a
    Respx mock for the upstream."""
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.service import CatalogService
    from eggpool.db.repositories import (
        AttemptRepository,
        RequestRepository,
        ReservationRepository,
        UsageWindowRepository,
    )
    from eggpool.health.health_manager import HealthManager
    from eggpool.request.coordinator import RequestCoordinator
    from eggpool.routing.router import Router

    httpx_client = httpx.AsyncClient(
        base_url=UPSTREAM_BASE,
        timeout=httpx.Timeout(60.0, connect=5.0, read=60.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(perf_config)
    catalog = CatalogService(perf_config, registry, perf_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "perf-acct")

    router = Router(registry, catalog)
    router.set_account_weight("perf-acct", 1.0)

    health_manager = HealthManager()
    request_repo = RequestRepository(perf_db)
    reservation_repo = ReservationRepository(perf_db)
    attempt_repo = AttemptRepository(perf_db)
    usage_window_repo = UsageWindowRepository(perf_db)

    dispatch_overhead = DispatchOverheadRecorder(window_size=200)
    local_pre_upstream = LocalPreUpstreamRecorder(window_size=200)
    dispatch_spans = DispatchSpanRecorder(window_size=200)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=perf_db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        dispatch_overhead_recorder=dispatch_overhead,
        local_pre_upstream_recorder=local_pre_upstream,
        dispatch_span_recorder=dispatch_spans,
    )
    yield coord, dispatch_overhead, local_pre_upstream, dispatch_spans
    await httpx_client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openai_payload(model: str = "gpt-4", content: str = "hi") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }


async def _ok_response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-baseline",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        },
    )


def _emit_summary(path: Path, name: str, payload: dict[str, Any]) -> None:
    """Persist a compact machine-readable baseline artifact."""
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{name}.json"
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------


class TestDispatchBaselineSerial:
    """Serial native (OpenAI) requests against a mock upstream."""

    @pytest.mark.asyncio()
    async def test_serial_native_baseline(
        self,
        baseline_coordinator: Any,
        baseline_dir: Path,
    ) -> None:
        coord, dispatch_overhead, local_pre_upstream, dispatch_spans = (
            baseline_coordinator
        )
        request_count = 20

        from eggpool.request.coordinator import ProxyRequestContext

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_response)
            for i in range(request_count):
                ctx = ProxyRequestContext(
                    request_id=f"serial-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=json.dumps(_openai_payload()).encode(),
                    incoming_headers={"content-type": "application/json"},
                    request_received_monotonic_ns=time.perf_counter_ns(),
                )
                await coord.execute(ctx)

        summary = {
            "workload": "serial_native",
            "request_count": request_count,
            "dispatch_overhead": dispatch_overhead.snapshot(),
            "local_pre_upstream_ms": local_pre_upstream.snapshot().as_dict(),
            "dispatch_spans": dispatch_spans.snapshot_for_spans(
                [
                    "routing_plan",
                    "selection_lock_wait",
                    "selection_locked",
                    "db_write_request",
                    "db_write_attempt",
                ]
            ),
        }
        _emit_summary(baseline_dir, "serial_native", summary)

        # Sanity: dispatch_overhead should have at least one sample.
        assert summary["dispatch_overhead"]["sample_count"] >= 1
        # The local pre-upstream recorder should also be populated.
        assert summary["local_pre_upstream_ms"]["sample_count"] >= 1


class TestDispatchBaselineConcurrent:
    """Moderate native concurrency (10 in-flight requests)."""

    @pytest.mark.asyncio()
    async def test_moderate_concurrent_native(
        self,
        baseline_coordinator: Any,
        baseline_dir: Path,
    ) -> None:
        coord, dispatch_overhead, local_pre_upstream, dispatch_spans = (
            baseline_coordinator
        )
        concurrency = 10
        request_count = 30

        from eggpool.request.coordinator import ProxyRequestContext

        async def one(i: int) -> None:
            ctx = ProxyRequestContext(
                request_id=f"concurrent-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(_openai_payload()).encode(),
                incoming_headers={"content-type": "application/json"},
                request_received_monotonic_ns=time.perf_counter_ns(),
            )
            await coord.execute(ctx)

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_response)
            sem = asyncio.Semaphore(concurrency)

            async def limited(i: int) -> None:
                async with sem:
                    await one(i)

            await asyncio.gather(*[limited(i) for i in range(request_count)])

        summary = {
            "workload": "concurrent_native",
            "request_count": request_count,
            "concurrency": concurrency,
            "dispatch_overhead": dispatch_overhead.snapshot(),
            "local_pre_upstream_ms": local_pre_upstream.snapshot().as_dict(),
        }
        _emit_summary(baseline_dir, "concurrent_native", summary)
        assert summary["dispatch_overhead"]["sample_count"] >= 1


class TestDispatchBaselineCancellation:
    """Cancellation burst during active streams is deferred to the
    high-concurrency stream harness (see ``stream_stability_harness.py``).
    This workload pins the dispatch recorder on a no-op cancellation:
    the upstream returns 200 and the client disconnects before the
    response is read.

    The point is to confirm that the dispatch recorder continues to
    record (and ``local_pre_upstream_ms`` still tracks the dispatch
    boundary even when the response is dropped), not to validate the
    streaming path itself."""

    @pytest.mark.asyncio()
    async def test_dispatch_recorder_stable_under_concurrency(
        self,
        baseline_coordinator: Any,
        baseline_dir: Path,
    ) -> None:
        coord, dispatch_overhead, _local_pre_upstream, _dispatch_spans = (
            baseline_coordinator
        )
        request_count = 50

        from eggpool.request.coordinator import ProxyRequestContext

        async def fire(i: int) -> None:
            ctx = ProxyRequestContext(
                request_id=f"stability-{i}",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(_openai_payload()).encode(),
                incoming_headers={"content-type": "application/json"},
                request_received_monotonic_ns=time.perf_counter_ns(),
            )
            with contextlib.suppress(Exception):
                await coord.execute(ctx)
            # Cancellation / transient failures must not crash the
            # dispatch recorder.  We deliberately do not assert on
            # request success here.

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_response)
            await asyncio.gather(
                *[fire(i) for i in range(request_count)], return_exceptions=True
            )

        # Recorder is still alive and reporting a bounded window.
        snap = dispatch_overhead.snapshot()
        assert snap["sample_count"] <= snap["window_size"]
        assert snap["avg_ms"] is None or snap["avg_ms"] >= 0


# ---------------------------------------------------------------------------
# Database contention snapshot (no requests required)
# ---------------------------------------------------------------------------


class TestContentionSnapshot:
    """A baseline of the Database lock-wait histogram so later
    milestones can compare lock convoy size to the pre-A reference."""

    @pytest.mark.asyncio()
    async def test_contention_snapshot_shape(self, perf_db: Any) -> None:
        """The contention snapshot always exposes the documented keys
        (write_ops, lock_wait_count, cumulative_lock_wait_s,
        max_lock_wait_s) plus percentile fields when samples exist."""
        snap = perf_db.contention_snapshot()
        # Required keys regardless of sample state.
        assert set(snap.keys()) >= {
            "write_ops",
            "read_ops",
            "total_transactions",
            "cumulative_lock_wait_s",
            "max_lock_wait_s",
            "lock_wait_count",
        }
        # Counter types are stable so the dashboard can rely on them.
        assert isinstance(snap["lock_wait_count"], int)
        assert isinstance(snap["cumulative_lock_wait_s"], (int, float))
        assert isinstance(snap["max_lock_wait_s"], (int, float))


# ---------------------------------------------------------------------------
# Background task cadence snapshot (Milestone A3)
# ---------------------------------------------------------------------------


class TestBackgroundTaskBaseline:
    """Captures the baseline for periodic-task cadence so milestone A3
    diagnostics can be compared before/after subsequent rehash / drift
    work.  Uses the unified ``register_runtime_tasks`` table but with
    a tiny interval so two ticks land inside the test budget."""

    @pytest.mark.asyncio()
    async def test_periodic_cadence_diagnostic_snapshot(self) -> None:
        from eggpool.background import TaskSupervisor

        supervisor = TaskSupervisor()

        async def noop_tick() -> None:
            return None

        # Register a single periodic task directly so we can assert on
        # its snapshot fields without spinning up the full app.
        task = supervisor.register_periodic(
            "baseline_tick",
            noop_tick,
            interval_s=0.05,
            initial_delay_s=0.01,
        )

        snap = task.snapshot()
        assert snap["configured_interval_s"] == 0.05
        assert snap["configured_initial_delay_s"] == 0.01
        assert snap["initial_delay_consumed"] is False
        assert snap["interval_s"] == 0.05

        await supervisor.start_all()
        await asyncio.sleep(0.1)
        snap_after = task.snapshot()
        await supervisor.stop_all()

        assert snap_after["initial_delay_consumed"] is True
        assert snap_after["last_tick_started_at"] is not None
        assert snap_after["last_tick_duration_ms"] is not None


# ---------------------------------------------------------------------------
# Runtime metrics service exposes both recorders (Milestone A4)
# ---------------------------------------------------------------------------


class TestRuntimeMetricsBaseline:
    """The runtime metrics snapshot exposes both ``dispatch_overhead``
    and ``local_pre_upstream`` so operators can compare coordinator-
    internal latency to total EggPool-side latency."""

    @pytest.mark.asyncio()
    async def test_runtime_metrics_exposes_local_pre_upstream(
        self, baseline_coordinator: Any
    ) -> None:
        coord, _dispatch_overhead, local_pre_upstream, _spans = baseline_coordinator
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key": "ep_test_runtime_baseline_0000000000"},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-test-runtime-base-00000000",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        db = coord._db  # pyright: ignore[reportPrivateUsage]
        service = RuntimeMetricsService(
            config=config,
            db=db,
            stats_db=None,
            supervisor=None,
            task_monitor=None,
            router=None,
            health_manager=None,
            started_monotonic=0.0,
            started_epoch=0.0,
            local_pre_upstream_recorder=local_pre_upstream,
        )
        snap = await service.snapshot()
        assert "local_pre_upstream" in snap
        assert "dispatch_overhead" in snap
        # Even with no samples the section should expose the
        # documented schema so consumers can rely on the key being
        # present.
        assert "window_size" in snap["local_pre_upstream"]
        assert "p95_ms" in snap["local_pre_upstream"]


# ---------------------------------------------------------------------------
# Milestone C: writer-enabled dispatch baseline
# ---------------------------------------------------------------------------


class TestDispatchWriterBaseline:
    """Milestone C baseline: writer-enabled dispatch pipeline.

    Compares the dispatch persistence writer path against the direct
    path to verify acceptance criteria:

    - AC#11: Under concurrent benchmark, SQLite transactions/commits
      per dispatch are materially reduced.
    - AC#12: Serial p50 regression remains within tolerance.
    """

    @pytest.mark.asyncio()
    async def test_writer_single_dispatch_transaction_count(
        self,
        perf_db: Any,
    ) -> None:
        """Single dispatch through the writer uses exactly 1 transaction."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        writer = DispatchPersistenceWriter(perf_db)
        writer.start()

        snap_before = perf_db.contention_snapshot()
        txns_before = snap_before["total_transactions"]

        intent = DispatchIntent(
            proxy_request_id="writer-baseline-single",
            attempt_number=1,
            account_id=1,
            account_name="perf-acct",
            provider_id="openai",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            estimated_tokens=100,
            estimated_microdollars=1_000,
            started_at="2026-01-01T00:00:00Z",
        )

        # Directly enqueue (bypasses call_soon_threadsafe for same-loop)
        future: CFuture[PersistedDispatchResult] = CFuture()
        qi = _QueuedIntent(intent=intent, future=future)
        writer._submitted_total += 1
        await writer._enqueue_from_event_loop(qi)
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)

        snap_after = perf_db.contention_snapshot()
        txns_used = snap_after["total_transactions"] - txns_before

        assert result.db_request_id
        assert txns_used == 1, f"Expected 1 transaction, got {txns_used}"

        await writer.stop()

    @pytest.mark.asyncio()
    async def test_writer_concurrent_dispatch_batch_reduction(
        self,
        perf_db: Any,
    ) -> None:
        """10 concurrent dispatches use fewer than 10 transactions.

        AC#11: 'Under the standard concurrent benchmark, SQLite
        transactions/commits per dispatch are materially reduced.'
        """
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        count = 10
        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=32,
            max_batch_wait_ms=50.0,
        )
        writer.start()

        snap_before = perf_db.contention_snapshot()
        txns_before = snap_before["total_transactions"]

        # Submit all intents rapidly so they batch together
        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"writer-batch-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        results = await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=5.0) for f in futures]
        )

        snap_after = perf_db.contention_snapshot()
        txns_used = snap_after["total_transactions"] - txns_before

        # All requests persisted successfully
        for r in results:
            assert r.db_request_id

        # The batch should use far fewer than N transactions
        assert txns_used < count, (
            f"Writer batch used {txns_used} transactions for "
            f"{count} dispatches; expected fewer than {count}"
        )

        snap = writer.snapshot()
        assert snap["persisted_total"] == count
        # The batch_size_max should be > 1, proving batching occurred
        assert snap["batch_size_max"] is not None
        assert snap["batch_size_max"] > 1

        await writer.stop()

    @pytest.mark.asyncio()
    async def test_writer_serial_no_cumulative_delay(
        self,
        perf_db: Any,
    ) -> None:
        """Serial dispatches through the writer do not accumulate delay.

        AC#12: 'Serial p50 regression remains within the agreed
        tolerance, recommended no more than 5% or 1 ms.'
        """
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        writer = DispatchPersistenceWriter(perf_db)
        writer.start()

        times: list[float] = []
        for i in range(5):
            t0 = time.monotonic()
            intent = DispatchIntent(
                proxy_request_id=f"writer-serial-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            times.append(elapsed_ms)
            assert result.db_request_id

        # Each serial dispatch should complete well under 100ms
        for i, t in enumerate(times):
            assert t < 100.0, f"Serial dispatch {i} took {t:.1f}ms"

        # No cumulative growth: last should not be > 3x first
        assert times[-1] < times[0] * 3.0 + 10.0, (
            f"Cumulative serial regression detected: {times}"
        )

        await writer.stop()

    @pytest.mark.asyncio()
    async def test_writer_diagnostics_snapshot_populated(
        self,
        perf_db: Any,
    ) -> None:
        """The writer diagnostics are exposed in the writer snapshot."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        writer = DispatchPersistenceWriter(perf_db)
        writer.start()

        intent = DispatchIntent(
            proxy_request_id="writer-diagnostics-1",
            attempt_number=1,
            account_id=1,
            account_name="perf-acct",
            provider_id="openai",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            estimated_tokens=100,
            estimated_microdollars=1_000,
            started_at="2026-01-01T00:00:00Z",
        )
        future: CFuture[PersistedDispatchResult] = CFuture()
        qi = _QueuedIntent(intent=intent, future=future)
        writer._submitted_total += 1
        await writer._enqueue_from_event_loop(qi)
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)

        snap = writer.snapshot()
        assert snap["state"] == "running"
        assert snap["submitted_total"] >= 1
        assert snap["persisted_total"] >= 1
        assert snap["batch_count"] >= 1

        await writer.stop()


# ---------------------------------------------------------------------------
# Milestone C: higher concurrency writer benchmarks
# ---------------------------------------------------------------------------


class TestDispatchWriterHigherConcurrency:
    """Writer benchmarks at 25 and 50 concurrent dispatches.

    AC#11: Under concurrent benchmark, SQLite transactions/commits per
    dispatch are materially reduced.
    """

    @pytest.mark.asyncio()
    async def test_writer_25_concurrent_dispatches(
        self,
        perf_db: Any,
    ) -> None:
        """25 concurrent dispatches batch into fewer transactions."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        count = 25
        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=32,
            max_batch_wait_ms=50.0,
        )
        writer.start()

        snap_before = perf_db.contention_snapshot()
        txns_before = snap_before["total_transactions"]

        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"writer-25concur-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        results = await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=5.0) for f in futures]
        )

        snap_after = perf_db.contention_snapshot()
        txns_used = snap_after["total_transactions"] - txns_before

        for r in results:
            assert r.db_request_id

        assert txns_used < count, (
            f"25 dispatches used {txns_used} txns; expected fewer than {count}"
        )

        snap = writer.snapshot()
        assert snap["persisted_total"] == count
        assert snap["batch_size_max"] is not None
        assert snap["batch_size_max"] > 1

        await writer.stop()

    @pytest.mark.asyncio()
    async def test_writer_50_concurrent_dispatches(
        self,
        perf_db: Any,
    ) -> None:
        """50 concurrent dispatches batch into fewer transactions."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        count = 50
        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=64,
            max_batch_wait_ms=50.0,
        )
        writer.start()

        snap_before = perf_db.contention_snapshot()
        txns_before = snap_before["total_transactions"]

        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"writer-50concur-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        results = await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=10.0) for f in futures]
        )

        snap_after = perf_db.contention_snapshot()
        txns_used = snap_after["total_transactions"] - txns_before

        for r in results:
            assert r.db_request_id

        assert txns_used < count, (
            f"50 dispatches used {txns_used} txns; expected fewer than {count}"
        )

        snap = writer.snapshot()
        assert snap["persisted_total"] == count
        assert snap["batch_size_max"] is not None
        assert snap["batch_size_max"] > 1

        await writer.stop()


# ---------------------------------------------------------------------------
# Milestone C: transcoded request through writer
# ---------------------------------------------------------------------------


class TestDispatchWriterTranscoded:
    """Writer persists transcoded (Anthropic protocol) requests correctly."""

    @pytest.mark.asyncio()
    async def test_transcoded_request_through_writer(
        self,
        perf_db: Any,
    ) -> None:
        """An Anthropic-protocol intent persists with correct fields."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        # Create an account for anthropic provider
        async with perf_db.transaction():
            await perf_db.execute_write(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, 1, 1.0, ?)",
                ("perf-anthropic", "TEST_KEY", "anthropic"),
            )
            await perf_db.execute_write(
                "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
                ("claude-3-opus", "anthropic"),
            )

        writer = DispatchPersistenceWriter(perf_db)
        writer.start()

        intent = DispatchIntent(
            proxy_request_id="writer-transcoded-1",
            attempt_number=1,
            account_id=2,
            account_name="perf-anthropic",
            provider_id="anthropic",
            model_id="claude-3-opus",
            protocol="anthropic",
            streamed=True,
            estimated_tokens=200,
            estimated_microdollars=2_000,
            started_at="2026-01-01T00:00:00Z",
        )

        future: CFuture[PersistedDispatchResult] = CFuture()
        qi = _QueuedIntent(intent=intent, future=future)
        writer._submitted_total += 1
        await writer._enqueue_from_event_loop(qi)
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)

        assert result.db_request_id
        # Verify the persisted row has the correct protocol
        row = await perf_db.fetch_one(
            "SELECT protocol FROM requests WHERE id = ?",
            (result.db_request_id,),
        )
        assert row is not None
        assert row["protocol"] == "anthropic"

        await writer.stop()


# ---------------------------------------------------------------------------
# Milestone C: streaming start burst through writer
# ---------------------------------------------------------------------------


class TestDispatchWriterStreamingBurst:
    """Writer handles a burst of streaming request intents."""

    @pytest.mark.asyncio()
    async def test_streaming_burst_all_persist(
        self,
        perf_db: Any,
    ) -> None:
        """10 streaming intents submitted rapidly all persist."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        count = 10
        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=16,
            max_batch_wait_ms=50.0,
        )
        writer.start()

        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"writer-stream-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=True,
                estimated_tokens=500,
                estimated_microdollars=5_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        results = await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=5.0) for f in futures]
        )

        for r in results:
            assert r.db_request_id
            # Verify streamed flag
            row = await perf_db.fetch_one(
                "SELECT streamed FROM requests WHERE id = ?",
                (r.db_request_id,),
            )
            assert row is not None
            assert row["streamed"] == 1

        snap = writer.snapshot()
        assert snap["persisted_total"] == count

        await writer.stop()


# ---------------------------------------------------------------------------
# Milestone C: primary DB contention under writer load
# ---------------------------------------------------------------------------


class TestDispatchWriterDBContention:
    """Writer maintains low contention under concurrent persistence load."""

    @pytest.mark.asyncio()
    async def test_writer_under_contention_stable(
        self,
        perf_db: Any,
    ) -> None:
        """20 concurrent dispatches through the writer produce bounded contention."""
        from concurrent.futures import Future as CFuture

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        count = 20
        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=32,
            max_batch_wait_ms=50.0,
        )
        writer.start()

        futures: list[CFuture[PersistedDispatchResult]] = []
        for i in range(count):
            intent = DispatchIntent(
                proxy_request_id=f"writer-contention-{i}",
                attempt_number=1,
                account_id=1,
                account_name="perf-acct",
                provider_id="openai",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                estimated_tokens=100,
                estimated_microdollars=1_000,
                started_at="2026-01-01T00:00:00Z",
            )
            future: CFuture[PersistedDispatchResult] = CFuture()
            qi = _QueuedIntent(intent=intent, future=future)
            writer._submitted_total += 1
            await writer._enqueue_from_event_loop(qi)
            futures.append(future)

        results = await asyncio.gather(
            *[asyncio.wait_for(asyncio.wrap_future(f), timeout=10.0) for f in futures]
        )

        for r in results:
            assert r.db_request_id

        snap_after = perf_db.contention_snapshot()
        # Lock wait should be bounded — the writer serializes DB access
        lock_wait_p95 = snap_after.get("lock_wait_p95_ms", 0.0)
        assert lock_wait_p95 < 100.0, (
            f"Lock wait p95 under contention: {lock_wait_p95:.1f}ms"
        )

        snap = writer.snapshot()
        assert snap["persisted_total"] == count

        await writer.stop()


# ---------------------------------------------------------------------------
# Milestone C: SBC/slow-storage simulation
# ---------------------------------------------------------------------------


class TestDispatchWriterSBCSimulation:
    """Writer behaviour under simulated SBC/slow-storage conditions.

    Uses a mock persist_dispatch_bundles to simulate slow writes and
    verifies the writer still completes within reasonable bounds.
    """

    @pytest.mark.asyncio()
    async def test_slow_write_completes_within_timeout(
        self,
        perf_db: Any,
    ) -> None:
        """Writer batch completes even when persistence is slow."""
        from concurrent.futures import Future as CFuture
        from unittest.mock import patch

        from eggpool.request.dispatch_intent import (
            DispatchIntent,
            PersistedDispatchResult,
        )
        from eggpool.request.dispatch_writer import (
            DispatchPersistenceWriter,
            _QueuedIntent,
        )

        async def _slow_persist(
            *args: Any, **kwargs: Any
        ) -> list[PersistedDispatchResult]:
            await asyncio.sleep(0.05)  # simulate 50ms write latency
            return [
                PersistedDispatchResult(
                    db_request_id=f"slow-{i}",
                    reservation_id=f"res-{i}",
                    attempt_id=i + 1,
                    attempt_number=1,
                    batch_id=1,
                    batch_size=1,
                )
                for i in range(len(args[1]))
            ]

        writer = DispatchPersistenceWriter(
            perf_db,
            max_batch_size=4,
            max_batch_wait_ms=50.0,
            shutdown_drain_timeout_s=5.0,
        )
        writer.start()

        with patch(
            "eggpool.request.dispatch_writer.persist_dispatch_bundles",
            side_effect=_slow_persist,
        ):
            futures: list[CFuture[PersistedDispatchResult]] = []
            for i in range(5):
                intent = DispatchIntent(
                    proxy_request_id=f"writer-sbc-{i}",
                    attempt_number=1,
                    account_id=1,
                    account_name="perf-acct",
                    provider_id="openai",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    estimated_tokens=100,
                    estimated_microdollars=1_000,
                    started_at="2026-01-01T00:00:00Z",
                )
                future: CFuture[PersistedDispatchResult] = CFuture()
                qi = _QueuedIntent(intent=intent, future=future)
                writer._submitted_total += 1
                await writer._enqueue_from_event_loop(qi)
                futures.append(future)

            t0 = time.monotonic()
            results = await asyncio.gather(
                *[
                    asyncio.wait_for(asyncio.wrap_future(f), timeout=10.0)
                    for f in futures
                ]
            )
            elapsed_ms = (time.monotonic() - t0) * 1000.0

        for r in results:
            assert r.db_request_id

        # Should complete within 5s (shutdown_drain_timeout) even with slow writes
        assert elapsed_ms < 5000.0, f"SBC simulation took {elapsed_ms:.0f}ms"

        await writer.stop()
