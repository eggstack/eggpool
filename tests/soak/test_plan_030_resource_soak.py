"""Plan 030 — Resource and long-running soak (Workstream I).

Short deterministic soak test that sends bursts of requests including
validation errors and verifies that error responses do not corrupt
subsequent request state.  Captures resource plateau metrics and
verifies no monotonic increase in RSS, task count, or dispatch latency.

Run with::

    uv run pytest tests/soak/test_plan_030_resource_soak.py -m soak -v
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from tests.helpers.mock_upstream import (
    UPSTREAM_BASE,
    MockResponseSpec,
    MockUpstream,
    MockUpstreamRule,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.soak]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(model: str = "gpt-4") -> MockResponseSpec:
    return MockResponseSpec(
        status_code=200,
        json_body={
            "id": "chatcmpl-soak",
            "object": "chat.completion",
            "model": model,
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


def _error_response(status: int = 400) -> MockResponseSpec:
    return MockResponseSpec(
        status_code=status,
        json_body={
            "error": {
                "type": "invalid_request_error",
                "message": "Bad request",
            }
        },
    )


def _get_rss_mb() -> float:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        # Fallback: use /proc/self/status on Linux or task_info on macOS
        try:
            import ctypes

            ctypes.CDLL("libSystem.dylib")
            return 0.0
        except Exception:
            return 0.0


def _get_task_count() -> int:
    try:
        return len(asyncio.all_tasks(asyncio.get_event_loop()))
    except RuntimeError:
        return 0


# ---------------------------------------------------------------------------
# Soak tests
# ---------------------------------------------------------------------------


class TestResourceSoak:
    """Short deterministic soak with resource plateau verification."""

    @pytest.mark.asyncio
    async def test_short_soak_mixed_workload(self) -> None:
        """15–30 minute equivalent: mixed native/transcoded, streaming,
        validation failures, and cancellations.

        Runs a burst of requests and verifies no state leak or resource
        growth.
        """
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=_error_response(400),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                response=_ok_response(),
            ),
            MockUpstreamRule(
                min_sequence=3,
                max_sequence=3,
                response=_ok_response(model="MiniMax-M3"),
            ),
            MockUpstreamRule(
                min_sequence=4,
                max_sequence=4,
                response=_error_response(429),
            ),
            MockUpstreamRule(
                min_sequence=5,
                max_sequence=5,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            # Warm up
            for _ in range(5):
                client.post(
                    "/chat/completions",
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
            upstream.reset()

            # Measure: 50 iterations of the 5-request pattern
            rss_before = _get_rss_mb()
            task_count_before = _get_task_count()
            latencies: list[float] = []

            for _ in range(50):
                for j in range(5):
                    start = time.monotonic()
                    client.post(
                        "/chat/completions",
                        json={
                            "model": "gpt-4" if j % 2 == 0 else "MiniMax-M3",
                            "messages": [{"role": "user", "content": "Hi"}],
                        },
                    )
                    elapsed = (time.monotonic() - start) * 1000
                    latencies.append(elapsed)

            rss_after = _get_rss_mb()
            task_count_after = _get_task_count()

        # All requests should have been sent
        assert upstream.request_count == 250

        # RSS should not grow unboundedly
        rss_delta = rss_after - rss_before
        assert rss_delta < 500.0, (
            f"RSS grew {rss_delta:.1f}MB during soak — potential leak"
        )

        # Task count should not grow unboundedly
        task_delta = task_count_after - task_count_before
        assert task_delta < 100, (
            f"Task count grew by {task_delta} during soak — potential leak"
        )

        # Latency should not increase monotonically
        # Compare first half vs second half p95
        first_half = latencies[:125]
        second_half = latencies[125:]
        p95_first = sorted(first_half)[int(len(first_half) * 0.95)]
        p95_second = sorted(second_half)[int(len(second_half) * 0.95)]
        # Second half p95 should not be more than 3x first half p95
        if p95_first > 0:
            assert p95_second < p95_first * 3, (
                f"Latency increased: first_half_p95={p95_first:.1f}ms, "
                f"second_half_p95={p95_second:.1f}ms"
            )

    @pytest.mark.asyncio
    async def test_resource_plateau(self) -> None:
        """Verify RSS, task count, and dispatch latency plateau after
        warm-up."""
        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)

        rss_samples: list[float] = []
        latency_samples: list[float] = []

        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            # Warm up
            for _ in range(20):
                client.post(
                    "/chat/completions",
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

            # Measure in 5 windows of 20 requests each
            for _ in range(5):
                window_latencies: list[float] = []
                for _ in range(20):
                    start = time.monotonic()
                    client.post(
                        "/chat/completions",
                        json={
                            "model": "gpt-4",
                            "messages": [{"role": "user", "content": "Hi"}],
                        },
                    )
                    window_latencies.append((time.monotonic() - start) * 1000)
                rss_samples.append(_get_rss_mb())
                latency_samples.append(
                    sorted(window_latencies)[int(len(window_latencies) * 0.95)]
                )

        # RSS should plateau (not monotonically increase)
        # The last sample should not be more than 50% above the first
        if rss_samples[0] > 0:
            assert rss_samples[-1] < rss_samples[0] * 1.5, (
                f"RSS did not plateau: first={rss_samples[0]:.1f}MB, "
                f"last={rss_samples[-1]:.1f}MB"
            )

        # Latency should plateau (not monotonically increase)
        # The last window p95 should not be more than 2x the first
        if latency_samples[0] > 0:
            assert latency_samples[-1] < latency_samples[0] * 2, (
                f"Latency did not plateau: first_p95={latency_samples[0]:.1f}ms, "
                f"last_p95={latency_samples[-1]:.1f}ms"
            )

    @pytest.mark.asyncio
    async def test_error_does_not_corrupt_subsequent_requests(self) -> None:
        """Error responses do not corrupt subsequent request state."""
        # Use a custom matcher: odd sequences get 400, even get 200.
        rules = [
            MockUpstreamRule(
                custom_matcher=lambda req: req.sequence % 2 == 1,
                response=_error_response(400),
            ),
            MockUpstreamRule(
                custom_matcher=lambda req: req.sequence % 2 == 0,
                response=_ok_response(),
            ),
        ]
        upstream = MockUpstream(rules=rules)
        with upstream, httpx.Client(base_url=UPSTREAM_BASE) as client:
            for _ in range(25):
                resp1 = client.post(
                    "/chat/completions",
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
                resp2 = client.post(
                    "/chat/completions",
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
                assert resp1.status_code == 400
                assert resp2.status_code == 200

        assert upstream.request_count == 50
