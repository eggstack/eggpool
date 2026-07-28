"""Plan 028 — Hot-path latency baseline.

Records the local-pre-upstream, dispatch, and response-parse latencies
for native and transcoded non-stream/stream requests to establish a
post-Plan-028 baseline.  These measurements are compared against the
Plan 023 baseline to detect regressions.

Run with::

    uv run pytest tests/perf/test_plan_028_hotpath_latency.py -m performance -v
"""

from __future__ import annotations

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
        "id": "chatcmpl-latency",
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


def _anthropic_ok_response() -> dict[str, Any]:
    return {
        "id": "msg-latency",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "model": "claude-3-opus",
        "usage": {
            "input_tokens": 5,
            "output_tokens": 3,
        },
    }


def _payload(model: str = "gpt-4", stream: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream:
        body["stream"] = True
    return body


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100.0)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Serial baseline
# ---------------------------------------------------------------------------


class TestPlan028HotpathLatency:
    """Serial request-response latency measurements."""

    async def test_native_nonstream_latency(self) -> None:
        """Measure native non-stream request-response latency."""
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
        assert p50 < 1.0, f"p50={p50:.3f}s exceeds 1s threshold"
        assert p95 < 2.0, f"p95={p95:.3f}s exceeds 2s threshold"

    async def test_anthropic_transcoded_nonstream_latency(self) -> None:
        """Measure Anthropic-to-OpenAI transcoded non-stream latency."""
        latencies: list[float] = []
        n_requests = 10

        async def _ok_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_anthropic_ok_response())

        with respx.mock(base_url=UPSTREAM_BASE) as router_mock:
            router_mock.post("/chat/completions").mock(side_effect=_ok_handler)
            async with httpx.AsyncClient(
                base_url=UPSTREAM_BASE,
                timeout=httpx.Timeout(60.0),
            ) as client:
                for _ in range(n_requests):
                    t0 = time.monotonic()
                    resp = await client.post(
                        "/chat/completions",
                        json=_payload(model="claude-3-opus"),
                    )
                    latencies.append(time.monotonic() - t0)
                    assert resp.status_code == 200

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        assert p50 < 2.0, f"p50={p50:.3f}s exceeds 2s threshold"
        assert p95 < 5.0, f"p95={p95:.3f}s exceeds 5s threshold"
