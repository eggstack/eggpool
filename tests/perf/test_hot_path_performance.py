"""Hot-path performance measurements for Milestone F.

Captures before/after measurements for:

- F7: JSON parse count per request (ParsedRequestPayload caches one parse).
- F8: Allocation-free padding estimation (estimate_padded_size).
- F9: ImmutableRequestState frozenset construction cost.
- F10: Header sanitization single-pass cost.
- F11: DispatchSpanRecorder append/snapshot latency.
- F6: EventLoopLagMonitor record/snapshot latency.

Run with::

    pytest tests/perf/test_hot_path_performance.py -m performance -v
"""

from __future__ import annotations

import json
import time

import pytest

from eggpool.event_loop_lag import EventLoopLagMonitor
from eggpool.proxy.client import HOP_BY_HOP_HEADERS, build_upstream_headers
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.request.payload_utils import estimate_padded_size
from eggpool.runtime_dispatch import DispatchSpanRecorder
from eggpool.runtime_manager import ImmutableRequestState

pytestmark = pytest.mark.performance

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LARGE_BODY = json.dumps(
    {
        "model": "gpt-4",
        "stream": True,
        "messages": [{"role": "user", "content": "x" * 10_000}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "A" * 200,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for i in range(50)
        ],
    }
).encode()

_SMALL_BODY = b'{"model": "gpt-4", "stream": false}'

_SAMPLE_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer sk-test-key",
    "X-Api-Key": "test-key",
    "Accept": "application/json",
    "OpenAI-Organization": "org-test",
    "Connection": "keep-alive",
    "Transfer-Encoding": "chunked",
    "Host": "api.example.com",
    "Content-Length": "1234",
}


# ---------------------------------------------------------------------------
# F7: ParsedRequestPayload parse caching
# ---------------------------------------------------------------------------


class TestParseCachingPerformance:
    def test_single_parse_vs_repeated_parse(self) -> None:
        """Verify ParsedRequestPayload parses JSON once, not per access."""
        payload = ParsedRequestPayload(original_bytes=_LARGE_BODY)

        # First access triggers parse
        t0 = time.perf_counter_ns()
        _ = payload.parsed_dict
        first_parse_ns = time.perf_counter_ns() - t0

        # Subsequent accesses use cache
        t0 = time.perf_counter_ns()
        for _ in range(1000):
            _ = payload.parsed_dict
        cached_1000_ns = time.perf_counter_ns() - t0

        # Cached access should be dramatically faster than parse
        # Even on slow hardware, 1000 cached reads < 1 original parse
        assert cached_1000_ns < first_parse_ns * 5, (
            f"Cached reads ({cached_1000_ns}ns) should be much faster "
            f"than parse ({first_parse_ns}ns)"
        )

    def test_derived_state_cached(self) -> None:
        """Verify model_id and streaming are computed once."""
        payload = ParsedRequestPayload(original_bytes=_LARGE_BODY)

        # First access computes
        t0 = time.perf_counter_ns()
        _ = payload.model_id
        _ = payload.streaming
        first_ns = time.perf_counter_ns() - t0

        # Second access uses cache
        t0 = time.perf_counter_ns()
        for _ in range(1000):
            _ = payload.model_id
            _ = payload.streaming
        cached_ns = time.perf_counter_ns() - t0

        # Cached should be faster
        assert cached_ns < first_ns * 5

    def test_small_body_parse_latency(self) -> None:
        """Baseline: small body parse is fast."""
        payload = ParsedRequestPayload(original_bytes=_SMALL_BODY)
        t0 = time.perf_counter_ns()
        _ = payload.parsed_dict
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # Should parse in under 100µs on any hardware
        assert elapsed_us < 100


# ---------------------------------------------------------------------------
# F8: Allocation-free padding estimation
# ---------------------------------------------------------------------------


class TestPaddingEstimationPerformance:
    def test_estimate_vs_synthetic_allocation(self) -> None:
        """Verify estimate_padded_size is faster than b'\\x00'*padding."""
        base_size = 10_000
        padding = 500_000

        # New approach: arithmetic only
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            estimate_padded_size(base_size, padding)
        new_ns = time.perf_counter_ns() - t0

        # Old approach: allocation
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            len(b"\x00" * padding) + base_size
        old_ns = time.perf_counter_ns() - t0

        # New approach should be at least as fast (likely much faster)
        # Allow some tolerance for micro-benchmark noise
        assert new_ns <= old_ns * 2, (
            f"estimate_padded_size ({new_ns}ns) should not be "
            f"significantly slower than allocation ({old_ns}ns)"
        )

    def test_large_expansion_bounded(self) -> None:
        """Very large expansions complete quickly."""
        t0 = time.perf_counter_ns()
        for _ in range(1000):
            estimate_padded_size(100, 10_000_000)
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 1000 calls with 10MB expansion should complete in < 1ms
        assert elapsed_us < 1000


# ---------------------------------------------------------------------------
# F9: ImmutableRequestState construction
# ---------------------------------------------------------------------------


class TestImmutableStatePerformance:
    def test_frozenset_construction(self) -> None:
        """Frozenset construction from small sets is fast."""
        providers = {"openai", "anthropic", "google", "minimax", "groq"}
        accounts = {"default", "team-a", "team-b", "shared"}

        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            ImmutableRequestState(
                provider_ids=frozenset(providers),
                account_names=frozenset(accounts),
                hop_by_hop_headers=HOP_BY_HOP_HEADERS,
                local_credential_headers=frozenset(
                    {"authorization", "x-api-key", "proxy-authorization"}
                ),
            )
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 10000 constructions should complete in < 50ms
        assert elapsed_us < 50_000


# ---------------------------------------------------------------------------
# F10: Header sanitization single-pass
# ---------------------------------------------------------------------------


class TestHeaderSanitizationPerformance:
    def test_single_pass_vs_two_pass(self) -> None:
        """build_upstream_headers is comparable to sanitize + update."""
        from eggpool.proxy.client import sanitize_request_headers

        # Single-pass approach
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            build_upstream_headers(_SAMPLE_HEADERS, "sk-test")
        single_pass_ns = time.perf_counter_ns() - t0

        # Two-pass approach (sanitize + auth inject)
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            sanitized = sanitize_request_headers(dict(_SAMPLE_HEADERS))
            sanitized["Authorization"] = "Bearer sk-test"
        two_pass_ns = time.perf_counter_ns() - t0

        # Single pass should be no slower (allow 3x tolerance for
        # connection-header parsing overhead)
        assert single_pass_ns <= two_pass_ns * 3, (
            f"Single pass ({single_pass_ns}ns) should be comparable "
            f"to two-pass ({two_pass_ns}ns)"
        )


# ---------------------------------------------------------------------------
# F11: DispatchSpanRecorder performance
# ---------------------------------------------------------------------------


class TestSpanRecorderPerformance:
    def test_record_latency(self) -> None:
        """Span record is fast under the lock."""
        recorder = DispatchSpanRecorder(window_size=200)
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            recorder.record_ns("test_span", 1_000_000)
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 10000 appends should complete in < 20ms
        assert elapsed_us < 20_000

    def test_snapshot_latency(self) -> None:
        """Snapshot is fast for bounded span count."""
        recorder = DispatchSpanRecorder(window_size=200)
        for i in range(30):
            for _ in range(200):
                recorder.record_ns(f"span_{i}", 1_000_000)

        t0 = time.perf_counter_ns()
        for _ in range(1000):
            recorder.snapshot()
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 1000 snapshots of 30 spans should complete in < 500ms
        assert elapsed_us < 500_000


# ---------------------------------------------------------------------------
# F6: EventLoopLagMonitor performance
# ---------------------------------------------------------------------------


class TestLagMonitorPerformance:
    def test_record_sample_latency(self) -> None:
        """Recording a lag sample is fast."""
        monitor = EventLoopLagMonitor(cadence_s=1.0, window_size=200)
        t0 = time.perf_counter_ns()
        for _ in range(10_000):
            monitor._record_sample(1.5)
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 10000 samples should complete in < 20ms
        assert elapsed_us < 20_000

    def test_snapshot_latency(self) -> None:
        """Snapshot of lag monitor is fast."""
        monitor = EventLoopLagMonitor(cadence_s=1.0, window_size=200)
        for _ in range(200):
            monitor._record_sample(1.5)

        t0 = time.perf_counter_ns()
        for _ in range(1000):
            monitor.snapshot()
        elapsed_us = (time.perf_counter_ns() - t0) / 1000
        # 1000 snapshots should complete in < 20ms
        assert elapsed_us < 20_000
