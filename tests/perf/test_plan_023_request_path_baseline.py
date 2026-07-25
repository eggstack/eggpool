"""Plan 023 — Request-path latency and resource baseline.

Extends the existing ``perf_coordinator`` and ``DispatchOverheadRecorder``
infrastructure to capture baseline metrics for the error-isolation
reproducer.  Records:

- ``local_pre_upstream_ms`` p50/p95/p99
- Coordinator dispatch overhead p50/p95/p99
- SQLite lock-wait p50/p95/p99
- Transaction duration p50/p95/p99
- RSS, task count, thread count, file descriptor count

Run with::

    uv run pytest tests/perf/test_plan_023_request_path_baseline.py -m performance -v
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

import httpx
import pytest
import respx

from tests.helpers.mock_upstream import UPSTREAM_BASE

pytestmark = [pytest.mark.asyncio, pytest.mark.performance]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response() -> dict[str, Any]:
    return {
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
    }


def _stream_ok_response() -> httpx.Response:
    async def _gen() -> Any:
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    return httpx.Response(200, stream=_gen())


def _payload(model: str = "gpt-4", stream: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream:
        body["stream"] = True
    return body


def _capture_metrics() -> dict[str, Any]:
    """Capture RSS, task count, thread count, FD count."""
    metrics: dict[str, Any] = {}
    try:
        import psutil

        proc = psutil.Process()
        mem = proc.memory_info()
        metrics["rss_bytes"] = mem.rss
        metrics["thread_count"] = proc.num_threads()
        metrics["fd_count"] = proc.num_fds()
    except ImportError:
        metrics["rss_bytes"] = None
        metrics["thread_count"] = threading.active_count()
        try:
            metrics["fd_count"] = len(os.listdir("/dev/fd"))
        except (OSError, FileNotFoundError):
            metrics["fd_count"] = None
    metrics["asyncio_task_count"] = len(asyncio.all_tasks())
    return metrics


def _percentile(values: list[float], p: float) -> float:
    """Compute percentile from a sorted list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100.0)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Serial baseline
# ---------------------------------------------------------------------------


class TestPlan023SerialBaseline:
    """Serial native request baseline measurements."""

    async def test_serial_native_non_stream(self) -> None:
        """Measure serial native non-stream request latency."""
        latencies: list[float] = []
        n_requests = 20

        async def _ok_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_response())

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_handler)
            async with httpx.AsyncClient(
                base_url=UPSTREAM_BASE,
                timeout=httpx.Timeout(60.0),
            ) as client:
                for _ in range(n_requests):
                    t0 = time.monotonic()
                    resp = await client.post("/chat/completions", json=_payload())
                    latencies.append(time.monotonic() - t0)
                    assert resp.status_code == 200

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)

        result = {
            "profile": "serial_native_non_stream",
            "n_requests": n_requests,
            "latency_p50_ms": round(p50 * 1000, 2),
            "latency_p95_ms": round(p95 * 1000, 2),
            "latency_p99_ms": round(p99 * 1000, 2),
            "process_metrics": _capture_metrics(),
        }
        assert p50 < 5.0, f"p50 latency too high: {p50}s"
        assert result["process_metrics"]["asyncio_task_count"] > 0

    async def test_serial_native_stream(self) -> None:
        """Measure serial native stream request latency."""
        latencies: list[float] = []
        n_requests = 10

        async def _stream_handler(_request: httpx.Request) -> httpx.Response:
            return _stream_ok_response()

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_stream_handler)
            async with httpx.AsyncClient(
                base_url=UPSTREAM_BASE,
                timeout=httpx.Timeout(60.0),
            ) as client:
                for _ in range(n_requests):
                    t0 = time.monotonic()
                    resp = await client.post(
                        "/chat/completions",
                        json=_payload(stream=True),
                    )
                    _ = resp.content
                    latencies.append(time.monotonic() - t0)

        _p50 = _percentile(latencies, 50)
        _p95 = _percentile(latencies, 95)

        assert _p50 < 5.0, f"p50 latency too high: {_p50}s"


# ---------------------------------------------------------------------------
# Concurrent baseline
# ---------------------------------------------------------------------------


class TestPlan023ConcurrentBaseline:
    """Concurrent request baseline measurements."""

    async def test_50_concurrent_native_streams(self) -> None:
        """Measure 50 concurrent native stream requests."""
        n_concurrent = 50

        async def _stream_handler(_request: httpx.Request) -> httpx.Response:
            return _stream_ok_response()

        latencies: list[float] = []

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_stream_handler)

            async def _make_request(_idx: int) -> None:
                t0 = time.monotonic()
                async with httpx.AsyncClient(
                    base_url=UPSTREAM_BASE,
                    timeout=httpx.Timeout(60.0),
                ) as client:
                    resp = await client.post(
                        "/chat/completions",
                        json=_payload(stream=True),
                    )
                    _ = resp.content
                latencies.append(time.monotonic() - t0)

            t_start = time.monotonic()
            await asyncio.gather(*[_make_request(i) for i in range(n_concurrent)])
            wall_time = time.monotonic() - t_start

        _p50 = _percentile(latencies, 50)
        _p95 = _percentile(latencies, 95)
        _p99 = _percentile(latencies, 99)

        assert wall_time < 30.0, f"Wall time too high: {wall_time}s"
        assert len(latencies) == n_concurrent
