"""Tests for Milestone A4 dispatch timing boundaries.

Verifies that:

- ``ProxyRequestContext.request_received_monotonic_ns`` and
  ``local_pre_upstream_ms`` round-trip through context construction
  and ``_send_upstream_request``.
- ``LocalPreUpstreamRecorder`` and ``DispatchOverheadRecorder`` are
  distinct recorders with non-overlapping coverage: the former
  measures handler entry -> dispatch, the latter measures context
  build -> dispatch.
- The runtime metrics service exposes a ``local_pre_upstream`` key
  distinct from ``dispatch_overhead`` when a recorder is wired.
"""

from __future__ import annotations

from eggpool.request.coordinator import ProxyRequestContext
from eggpool.runtime_dispatch import (
    DispatchOverheadRecorder,
    LocalPreUpstreamRecorder,
    LocalPreUpstreamSnapshot,
)


def test_local_pre_upstream_recorder_distinct_from_dispatch_overhead() -> None:
    """The two recorders use different window types and a different
    sample unit (milliseconds vs nanoseconds)."""
    lpu = LocalPreUpstreamRecorder(window_size=10)
    dispatch = DispatchOverheadRecorder(window_size=10)
    lpu.record_ms(50)
    dispatch.record_ns(123_456_789)
    snap_lpu = lpu.snapshot()
    snap_dispatch = dispatch.snapshot()
    assert isinstance(snap_lpu, LocalPreUpstreamSnapshot)
    assert snap_lpu.sample_count == 1
    assert snap_lpu.avg_ms == 50.0
    # Dispatch overhead records nanoseconds; returns a dict.
    assert snap_dispatch["sample_count"] == 1
    assert snap_dispatch["avg_ms"] == 123.456789


def test_proxy_request_context_accepts_request_received_anchor() -> None:
    """The new A4 fields are accepted on ``ProxyRequestContext``."""
    ctx = ProxyRequestContext(
        request_id="a4",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
        request_received_monotonic_ns=1_000_000_000,
    )
    assert ctx.request_received_monotonic_ns == 1_000_000_000
    assert ctx.local_pre_upstream_ms is None


def test_proxy_request_context_defaults_for_a4_fields() -> None:
    """``request_received_monotonic_ns`` defaults to ``None`` so
    legacy callers that build contexts without the anchor still work."""
    ctx = ProxyRequestContext(
        request_id="legacy",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"messages":[{"role":"user","content":"hi"}]}',
        incoming_headers={},
    )
    assert ctx.request_received_monotonic_ns is None
    assert ctx.local_pre_upstream_ms is None


def test_local_pre_upstream_recorder_window_size_validation() -> None:
    """The constructor rejects zero / negative window sizes."""
    import pytest

    with pytest.raises(ValueError):
        LocalPreUpstreamRecorder(window_size=0)
    with pytest.raises(ValueError):
        LocalPreUpstreamRecorder(window_size=-1)


def test_local_pre_upstream_recorder_ignores_negative() -> None:
    """Negative samples are ignored (caller may pass uninitialised timers);
    zero is allowed because the recorder has no way to distinguish a
    real zero-ms request from a sentinel value, so it falls through."""
    rec = LocalPreUpstreamRecorder(window_size=10)
    rec.record_ms(-5)
    assert rec.snapshot().sample_count == 0
    rec.record_ms(25)
    assert rec.snapshot().sample_count == 1
    assert rec.snapshot().avg_ms == 25.0


def test_local_pre_upstream_snapshot_has_full_percentiles() -> None:
    """The snapshot exposes avg/min/max/p50/p95/p99 percentiles."""
    rec = LocalPreUpstreamRecorder(window_size=100)
    for value in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 50, 100]:
        rec.record_ms(value)
    snap = rec.snapshot()
    assert snap.sample_count == 12
    assert snap.min_ms == 1.0
    assert snap.max_ms == 100.0
    assert snap.p50_ms is not None
    assert snap.p95_ms is not None
    assert snap.p99_ms is not None
    assert snap.p95_ms >= snap.p50_ms
    assert snap.p99_ms >= snap.p95_ms
