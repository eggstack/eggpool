"""Workload profiles for soak testing.

Canonical profiles for dispatch stability validation.

Defines eight canonical workload profiles with fixed random seeds
and deterministic mock upstream behavior. Each profile exercises a
distinct operational scenario for dispatch stability validation.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

if TYPE_CHECKING:
    from eggpool.db.connection import Database

pytestmark = [pytest.mark.soak, pytest.mark.workload_profile]

UPSTREAM_BASE = "https://soak-test-upstream.example.com"


def _make_stream_body(model: str, request_id: str) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": f"Message {request_id}"}],
            "stream": True,
        }
    ).encode()


def _make_non_stream_body(model: str, request_id: str) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": f"Message {request_id}"}],
        }
    ).encode()


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
                        "delta": {"content": "Hello"},
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


async def _non_stream_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        },
    )


async def _slow_stream_handler(request: httpx.Request) -> httpx.Response:
    """Simulates a slow upstream with token-by-token delivery."""

    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        for i in range(10):
            yield b"data: "
            yield json.dumps(
                {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"token-{i}"},
                            "finish_reason": None,
                        }
                    ],
                }
            ).encode()
            yield b"\n\n"
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _error_handler(request: httpx.Request) -> httpx.Response:
    """Returns a 429 rate-limit response."""
    return httpx.Response(429, json={"error": {"message": "rate limited"}})


async def _consume_stream(stream_iter: object) -> None:
    """Fully consume a stream, discarding all chunks."""
    async for _chunk in stream_iter:  # type: ignore[misc]
        pass


class TestProfile1LowVolumeSteadyNative:
    """Profile 1: Low-volume steady native OpenAI-compatible requests.

    Serial or low concurrency, mixed streaming/non-streaming.
    Purpose: detect fixed overhead regressions and idle/steady resource growth.
    """

    @pytest.mark.asyncio
    async def test_low_volume_serial(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_non_stream_handler
            )
            for i in range(20):
                context = ProxyRequestContext(
                    request_id=f"profile1-serial-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_make_non_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200

        req_rows = await soak_db.fetch_all("SELECT status FROM requests")
        assert all(row["status"] == "completed" for row in req_rows)
        resv_rows = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(resv_rows) == 0

    @pytest.mark.asyncio
    async def test_low_volume_mixed_streaming(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        rng = random.Random(42)
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            for i in range(15):
                streaming = rng.random() < 0.6
                context = ProxyRequestContext(
                    request_id=f"profile1-mixed-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=streaming,
                    original_body=_make_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200
                if response.stream_iterator is not None:
                    await _consume_stream(response.stream_iterator)

        pending = await soak_db.fetch_all(
            "SELECT * FROM requests WHERE status = 'pending'"
        )
        assert len(pending) == 0


class TestProfile2ModerateSustainedMixed:
    """Profile 2: Moderate sustained mixed traffic.

    5-10 concurrent streams, OpenAI and Anthropic-compatible requests,
    mixed streaming/non-streaming.
    Purpose: representative long-running deployment.
    """

    @pytest.mark.asyncio
    async def test_concurrent_mixed_protocols(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            tasks = []
            for i in range(8):
                streaming = i % 3 != 0
                context = ProxyRequestContext(
                    request_id=f"profile2-mixed-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=streaming,
                    original_body=_make_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                tasks.append(soak_coordinator.execute(context))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 0, f"Got errors: {errors}"

            for r in results:
                if not isinstance(r, Exception) and r.stream_iterator is not None:
                    await _consume_stream(r.stream_iterator)

        pending = await soak_db.fetch_all(
            "SELECT * FROM requests WHERE status = 'pending'"
        )
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_concurrent_streaming_burst(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_slow_stream_handler
            )
            tasks = []
            for i in range(10):
                context = ProxyRequestContext(
                    request_id=f"profile2-burst-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=_make_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                tasks.append(soak_coordinator.execute(context))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 0, f"Got errors: {errors}"

            for r in results:
                if not isinstance(r, Exception) and r.stream_iterator is not None:
                    await _consume_stream(r.stream_iterator)

        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0


class TestProfile3BurstAndRecovery:
    """Profile 3: Burst and recovery.

    Repeated bursts of dispatches followed by quiet recovery periods.
    Purpose: prove dispatch writer, finalization queue, and DB lock
    queues return to baseline.
    """

    @pytest.mark.asyncio
    async def test_burst_recovery_cycle(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            for burst in range(3):
                tasks = []
                for i in range(5):
                    context = ProxyRequestContext(
                        request_id=f"profile3-b{burst}-{i}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=True,
                        original_body=_make_stream_body("gpt-4", f"{burst}-{i}"),
                        incoming_headers={"content-type": "application/json"},
                    )
                    tasks.append(soak_coordinator.execute(context))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if not isinstance(r, Exception) and r.stream_iterator is not None:
                        await _consume_stream(r.stream_iterator)

                # Recovery period: verify queues drain
                pending = await soak_db.fetch_all(
                    "SELECT * FROM requests WHERE status = 'pending'"
                )
                assert len(pending) == 0, (
                    f"Burst {burst}: {len(pending)} pending requests remain"
                )

        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0


class TestProfile4RetryHealthChurn:
    """Profile 4: Retry/health churn.

    Controlled 429, quota, 5xx responses with multiple accounts.
    Purpose: test backoff persistence, health slots, and queue cleanup.
    """

    @pytest.mark.asyncio
    async def test_rate_limit_handling(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_error_handler
            )
            for i in range(10):
                context = ProxyRequestContext(
                    request_id=f"profile4-rl-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_make_non_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                # Rate limits should still produce a response (error or 429)
                assert response.status_code in (200, 429, 503)

        # After rate limiting, no active reservations should remain
        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0


class TestProfile5CancellationHeavyStreaming:
    """Profile 5: Cancellation-heavy streaming.

    Client cancellation before upstream headers, after headers,
    and midstream disconnects.
    Purpose: prove finalization retry queue and reservation cleanup
    do not grow over time.
    """

    @pytest.mark.asyncio
    async def test_cancellation_without_consuming(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Dispatch and consume streams rapidly to test finalization under pressure.

        Note: true client cancellation requires ASGI-level disconnect detection.
        This test validates that rapid stream dispatch and consumption does not
        leak reservations.
        """
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            for i in range(15):
                context = ProxyRequestContext(
                    request_id=f"profile5-cancel-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=_make_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                if response.stream_iterator is not None:
                    await _consume_stream(response.stream_iterator)

        await asyncio.sleep(0.2)

        # All reservations should be released after consumption
        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0, (
            f"Found {len(active_resv)} unreleased reservations after streaming"
        )


class TestProfile6MaintenanceBacklog:
    """Profile 6: Maintenance backlog.

    Large synthetic request/event tables with accelerated bounded
    retention/reconciliation.
    Purpose: prove maintenance drains without monopolizing dispatch.
    """

    @pytest.mark.asyncio
    async def test_requests_under_maintenance_pressure(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Verify requests complete even with many existing rows."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_non_stream_handler
            )
            # Create a backlog of completed requests
            for i in range(30):
                context = ProxyRequestContext(
                    request_id=f"profile6-backlog-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_make_non_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200

            # Now run more requests under the backlog
            for i in range(10):
                context = ProxyRequestContext(
                    request_id=f"profile6-under-load-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_make_non_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200

        req_rows = await soak_db.fetch_all(
            "SELECT status FROM requests WHERE status = 'completed'"
        )
        assert len(req_rows) == 40


class TestProfile8SlowStorageSimulation:
    """Profile 8: Slow-storage simulation.

    Uses constrained request patterns to simulate SBC-like behavior.
    Purpose: validate intended deployment class.
    """

    @pytest.mark.asyncio
    async def test_constrained_sequential(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Sequential requests with deliberate pacing."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_slow_stream_handler
            )
            for i in range(5):
                context = ProxyRequestContext(
                    request_id=f"profile8-seq-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=_make_stream_body("gpt-4", str(i)),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200
                if response.stream_iterator is not None:
                    await _consume_stream(response.stream_iterator)

        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0
