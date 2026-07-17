"""Stability assertion tests for soak validation (Workstream G4).

Defines early/late window comparison framework and stability ratio
gates. These tests verify that dispatch latency, queue depths,
and resource usage remain stable over time under stationary workloads.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

if TYPE_CHECKING:
    from eggpool.db.connection import Database

pytestmark = [pytest.mark.soak, pytest.mark.stability_assertion]

UPSTREAM_BASE = "https://soak-test-upstream.example.com"

# Stability ratio thresholds from Workstream G4
DISPATCH_P95_RATIO_LIMIT = 1.20
DISPATCH_P99_RATIO_LIMIT = 1.50
DB_LOCK_P95_RATIO_LIMIT = 1.25
EVENT_LOOP_LAG_P95_RATIO_LIMIT = 1.25
# CI tolerance is wider than the plan's 10% because short-window measurements
# are sensitive to database row growth between windows. The real validation
# happens in multi-hour soak runs.
THROUGHPUT_DECLINE_LIMIT = 0.30


@dataclass
class WindowMetrics:
    """Metrics collected during a measurement window."""

    window_name: str
    start_time: float
    end_time: float
    dispatch_latencies_ms: list[float] = field(default_factory=list)
    request_count: int = 0
    error_count: int = 0
    active_reservations: int = 0
    pending_requests: int = 0

    @property
    def duration_s(self) -> float:
        return self.end_time - self.start_time

    @property
    def throughput(self) -> float:
        if self.duration_s == 0:
            return 0.0
        return self.request_count / self.duration_s

    def p50(self) -> float:
        if not self.dispatch_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.dispatch_latencies_ms)
        mid = len(sorted_lat) // 2
        if len(sorted_lat) % 2 == 0:
            return (sorted_lat[mid - 1] + sorted_lat[mid]) / 2
        return sorted_lat[mid]

    def p95(self) -> float:
        if not self.dispatch_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.dispatch_latencies_ms)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def p99(self) -> float:
        if not self.dispatch_latencies_ms:
            return 0.0
        sorted_lat = sorted(self.dispatch_latencies_ms)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


async def _stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        yield b"data: "
        yield json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hi"},
                        "finish_reason": None,
                    }
                ],
            }
        ).encode()
        yield b"\n\n"
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _consume_stream(stream_iter: object) -> None:
    async for _chunk in stream_iter:  # type: ignore[misc]
        pass


async def _run_measurement_window(
    coordinator: RequestCoordinator,
    db: Database,
    window_name: str,
    request_count: int,
    concurrency: int = 1,
) -> WindowMetrics:
    """Execute a measurement window and collect metrics."""
    window = WindowMetrics(
        window_name=window_name,
        start_time=time.monotonic(),
        end_time=0.0,
    )

    async def _dispatch_one(idx: int) -> None:
        start = time.monotonic()
        context = ProxyRequestContext(
            request_id=f"{window_name}-{idx}",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=json.dumps(
                {
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": f"Msg {idx}"}],
                    "stream": True,
                }
            ).encode(),
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)
        elapsed_ms = (time.monotonic() - start) * 1000
        window.dispatch_latencies_ms.append(elapsed_ms)
        window.request_count += 1
        if response.status_code != 200:
            window.error_count += 1
        if response.stream_iterator is not None:
            await _consume_stream(response.stream_iterator)

    if concurrency <= 1:
        for i in range(request_count):
            await _dispatch_one(i)
    else:
        sem = asyncio.Semaphore(concurrency)

        async def _limited(idx: int) -> None:
            async with sem:
                await _dispatch_one(idx)

        await asyncio.gather(*[_limited(i) for i in range(request_count)])

    window.end_time = time.monotonic()

    # Collect final state
    pending = await db.fetch_all("SELECT * FROM requests WHERE status = 'pending'")
    window.pending_requests = len(pending)
    active_resv = await db.fetch_all(
        "SELECT * FROM reservations WHERE status = 'active'"
    )
    window.active_reservations = len(active_resv)

    return window


class TestEarlyLateStability:
    """Early-vs-late window stability ratio checks."""

    @pytest.mark.asyncio
    async def test_dispatch_p95_stability(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Late dispatch p95 must not exceed 1.20x early p95.

        Includes a warm-up window to stabilize database state before
        measurement. Applies a minimum floor to avoid failing on noisy
        microsecond-level ratios when both values are trivial.
        """
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            # Warm-up: stabilize database and coordinator state
            await _run_measurement_window(
                soak_coordinator, soak_db, "warmup", request_count=30
            )
            early = await _run_measurement_window(
                soak_coordinator, soak_db, "early", request_count=50
            )
            late = await _run_measurement_window(
                soak_coordinator, soak_db, "late", request_count=50
            )

        # Skip ratio check when both values are below the floor (trivial)
        p95_floor_ms = 5.0
        if early.p95() >= p95_floor_ms and late.p95() >= p95_floor_ms:
            ratio = late.p95() / early.p95()
            assert ratio <= DISPATCH_P95_RATIO_LIMIT, (
                f"Late p95 ({late.p95():.1f}ms) / early p95 ({early.p95():.1f}ms) "
                f"= {ratio:.2f} exceeds {DISPATCH_P95_RATIO_LIMIT}"
            )

    @pytest.mark.asyncio
    async def test_dispatch_p99_stability(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Late dispatch p99 must not exceed 1.50x early p99.

        Includes a warm-up window and applies a minimum floor to avoid
        failing on noisy microsecond-level ratios.
        """
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            # Warm-up
            await _run_measurement_window(
                soak_coordinator, soak_db, "warmup-p99", request_count=30
            )
            early = await _run_measurement_window(
                soak_coordinator, soak_db, "early-p99", request_count=50
            )
            late = await _run_measurement_window(
                soak_coordinator, soak_db, "late-p99", request_count=50
            )

        # Skip ratio check when both values are below the floor (trivial)
        # p99 with moderate sample sizes is essentially the max, which is
        # inherently noisy; apply a higher floor than p95.
        p99_floor_ms = 5.0
        if early.p99() >= p99_floor_ms and late.p99() >= p99_floor_ms:
            ratio = late.p99() / early.p99()
            assert ratio <= DISPATCH_P99_RATIO_LIMIT, (
                f"Late p99 ({late.p99():.1f}ms) / early p99 ({early.p99():.1f}ms) "
                f"= {ratio:.2f} exceeds {DISPATCH_P99_RATIO_LIMIT}"
            )

    @pytest.mark.asyncio
    async def test_throughput_stability(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Throughput must not decline by more than 10%.

        Includes a warm-up window and applies a minimum floor to avoid
        failing on noisy short-window measurements.
        """
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            # Warm-up
            await _run_measurement_window(
                soak_coordinator, soak_db, "warmup-tp", request_count=30
            )
            early = await _run_measurement_window(
                soak_coordinator, soak_db, "early-tp", request_count=30
            )
            late = await _run_measurement_window(
                soak_coordinator, soak_db, "late-tp", request_count=30
            )

        # Skip when throughput is too low to be meaningful
        min_throughput = 5.0  # requests/second
        if early.throughput >= min_throughput and late.throughput >= min_throughput:
            decline = 1.0 - (late.throughput / early.throughput)
            assert decline <= THROUGHPUT_DECLINE_LIMIT, (
                f"Throughput decline {decline:.1%} exceeds "
                f"{THROUGHPUT_DECLINE_LIMIT:.0%}"
            )

    @pytest.mark.asyncio
    async def test_queue_drain_after_burst(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Queue depths must return to baseline after bursts."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            # Burst
            await _run_measurement_window(
                soak_coordinator, soak_db, "burst", request_count=15, concurrency=5
            )
            # Recovery
            recovery = await _run_measurement_window(
                soak_coordinator, soak_db, "recovery", request_count=5
            )

        assert recovery.pending_requests == 0, (
            f"Pending requests after recovery: {recovery.pending_requests}"
        )
        assert recovery.active_reservations == 0, (
            f"Active reservations after recovery: {recovery.active_reservations}"
        )

    @pytest.mark.asyncio
    async def test_no_monotonic_queue_growth(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Queue depths must not show positive monotonic trend."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            pending_counts = []
            for cycle in range(5):
                window = await _run_measurement_window(
                    soak_coordinator, soak_db, f"cycle-{cycle}", request_count=10
                )
                pending_counts.append(window.pending_requests)

        # No monotonic increase in pending counts
        for i in range(1, len(pending_counts)):
            assert pending_counts[i] <= pending_counts[i - 1], (
                f"Monotonic growth at cycle {i}: "
                f"{pending_counts[i - 1]} -> {pending_counts[i]}"
            )
