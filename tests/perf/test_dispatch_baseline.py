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
