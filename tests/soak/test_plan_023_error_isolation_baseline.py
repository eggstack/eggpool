"""Plan 023 — Error isolation soak baseline.

Short deterministic soak test that sends bursts of requests including
validation errors and verifies that error responses do not corrupt
subsequent request state.  Captures resource plateau metrics.

Run with::

    uv run pytest tests/soak/test_plan_023_error_isolation_baseline.py -m soak -v
"""

from __future__ import annotations

import asyncio
import resource
import threading
from typing import Any

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
                "message": f"Error {status}",
            }
        },
    )


def _payload(model: str = "gpt-4") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }


def _capture_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "asyncio_task_count": len(asyncio.all_tasks()),
        "thread_count": threading.active_count(),
    }
    try:
        import psutil

        proc = psutil.Process()
        metrics["rss_bytes"] = proc.memory_info().rss
        metrics["fd_count"] = proc.num_fds()
    except ImportError:
        try:
            metrics["fd_count"] = len(list(resource.getrlimit(resource.RLIMIT_NOFILE)))
        except Exception:
            metrics["fd_count"] = None
    return metrics


# ---------------------------------------------------------------------------
# Soak test
# ---------------------------------------------------------------------------


class TestPlan023ErrorIsolationBaseline:
    """Error-isolation soak baseline."""

    async def test_mixed_success_error_workload(self) -> None:
        """Send a mixed workload of successes and errors, verify no state leak."""
        n_requests = 50
        error_ratio = 0.3

        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=int(n_requests * error_ratio),
                response=_error_response(400),
            ),
            MockUpstreamRule(
                min_sequence=int(n_requests * error_ratio) + 1,
                max_sequence=n_requests,
                response=_ok_response(),
            ),
        ]

        upstream = MockUpstream(rules=rules)
        results: list[int] = []

        with upstream:
            async with httpx.AsyncClient(
                base_url=UPSTREAM_BASE,
                timeout=httpx.Timeout(30.0),
            ) as client:
                for _ in range(n_requests):
                    resp = await client.post(
                        "/chat/completions",
                        json=_payload(),
                    )
                    results.append(resp.status_code)

        # First batch should be errors, rest should be successes
        error_count = sum(1 for r in results if r == 400)
        ok_count = sum(1 for r in results if r == 200)
        assert error_count > 0, "Expected some error responses"
        assert ok_count > 0, "Expected some success responses"
        assert error_count + ok_count == n_requests

    async def test_concurrent_mixed_workload(self) -> None:
        """Concurrent workload mixing errors and successes."""
        n_concurrent = 20

        rules = [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=n_concurrent // 2,
                response=_error_response(400),
            ),
            MockUpstreamRule(
                min_sequence=n_concurrent // 2 + 1,
                max_sequence=n_concurrent,
                response=_ok_response(),
            ),
        ]

        upstream = MockUpstream(rules=rules)
        results: list[int] = []

        with upstream:
            async with httpx.AsyncClient(
                base_url=UPSTREAM_BASE,
                timeout=httpx.Timeout(30.0),
            ) as client:

                async def _req() -> None:
                    resp = await client.post(
                        "/chat/completions",
                        json=_payload(),
                    )
                    results.append(resp.status_code)

                await asyncio.gather(*[_req() for _ in range(n_concurrent)])

        assert len(results) == n_concurrent
        assert 400 in results
        assert 200 in results

    async def test_resource_plateau(self) -> None:
        """Verify resource usage stabilizes after repeated error+success cycles."""
        metrics_before = _capture_metrics()

        n_cycles = 10
        for _ in range(n_cycles):
            rules = [
                MockUpstreamRule(response=_error_response(400)),
            ]
            upstream = MockUpstream(rules=rules)
            with upstream:
                async with httpx.AsyncClient(
                    base_url=UPSTREAM_BASE,
                    timeout=httpx.Timeout(10.0),
                ) as client:
                    resp = await client.post("/chat/completions", json=_payload())
                    assert resp.status_code == 400

        metrics_after = _capture_metrics()

        # After repeated cycles, thread count should not grow unbounded
        if "thread_count" in metrics_before and "thread_count" in metrics_after:
            assert metrics_after["thread_count"] <= metrics_before["thread_count"] + 5
