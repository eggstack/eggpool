"""Tests for telemetry bounded growth (Milestone F11/F12).

Verifies:

- DispatchOverheadRecorder deque never exceeds window_size.
- DispatchSpanRecorder per-span samples never exceed window_size.
- DispatchSpanRecorder span count grows only with new span keys.
- StreamDiagnostics ring histograms are bounded.
- EventLoopLagMonitor sample buffer is bounded.
- Snapshot under concurrent append does not raise.
"""

from __future__ import annotations

import threading

from eggpool.event_loop_lag import EventLoopLagMonitor
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    StreamDiagnostics,
)
from eggpool.runtime_dispatch import (
    DispatchOverheadRecorder,
    DispatchSpanRecorder,
)


class TestOverheadRecorderBounded:
    def test_deque_never_exceeds_window(self) -> None:
        recorder = DispatchOverheadRecorder(window_size=10)
        for i in range(100):
            recorder.record_ns(i * 1_000_000)
        snap = recorder.snapshot()
        assert snap["sample_count"] <= 10

    def test_window_size_respected(self) -> None:
        recorder = DispatchOverheadRecorder(window_size=5)
        for _ in range(50):
            recorder.record_ns(1000)
        snap = recorder.snapshot()
        assert snap["sample_count"] == 5

    def test_empty_snapshot(self) -> None:
        recorder = DispatchOverheadRecorder(window_size=10)
        snap = recorder.snapshot()
        assert snap["sample_count"] == 0

    def test_concurrent_appends(self) -> None:
        recorder = DispatchOverheadRecorder(window_size=50)
        errors: list[Exception] = []

        def record_batch() -> None:
            try:
                for _ in range(200):
                    recorder.record_ns(1000)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_batch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        snap = recorder.snapshot()
        assert snap["sample_count"] <= 50


class TestSpanRecorderBounded:
    def test_per_span_samples_never_exceed_window(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10)
        for i in range(100):
            recorder.record_ns("test_span", i * 1_000)
        snap = recorder.snapshot()
        for row in snap["spans"]:
            assert row["sample_count"] <= 10

    def test_multiple_spans_independent(self) -> None:
        recorder = DispatchSpanRecorder(window_size=5)
        for _ in range(20):
            recorder.record_ns("span_a", 1000)
            recorder.record_ns("span_b", 2000)
        snap = recorder.snapshot()
        spans = {row["span"]: row["sample_count"] for row in snap["spans"]}
        assert spans["span_a"] == 5
        assert spans["span_b"] == 5

    def test_span_count_grows_only_with_new_keys(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10)
        for _ in range(20):
            recorder.record_ns("s1", 1000)
            recorder.record_ns("s2", 2000)
        snap = recorder.snapshot()
        assert len(snap["spans"]) == 2

    def test_empty_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10)
        snap = recorder.snapshot()
        assert snap["spans"] == []

    def test_snapshot_for_spans_empty_key(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10)
        snap = recorder.snapshot_for_spans(["nonexistent"])
        assert len(snap["spans"]) == 1
        assert snap["spans"][0]["sample_count"] == 0

    def test_concurrent_span_appends(self) -> None:
        recorder = DispatchSpanRecorder(window_size=50)
        errors: list[Exception] = []

        def record_batch(span: str) -> None:
            try:
                for _ in range(100):
                    recorder.record_ns(span, 1000)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=record_batch, args=(f"span_{i}",)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        snap = recorder.snapshot()
        for row in snap["spans"]:
            assert row["sample_count"] <= 50


class TestStreamDiagnosticsBounded:
    def test_histogram_capacity_respected(self) -> None:
        diag = StreamDiagnostics(histogram_capacity=10)
        for i in range(100):
            diag.record_outcome(STREAM_OUTCOME_COMPLETED, elapsed_ms=i)
        snap = diag.snapshot()
        assert snap["completed_ms"]["sample_count"] <= 10

    def test_client_cancel_histogram_bounded(self) -> None:
        diag = StreamDiagnostics(histogram_capacity=5)
        for i in range(50):
            diag.record_outcome(STREAM_OUTCOME_CLIENT_CANCELLED, elapsed_ms=i)
        snap = diag.snapshot()
        assert snap["client_cancel_ms"]["sample_count"] <= 5

    def test_outcome_counter_grows_unbounded(self) -> None:
        """Outcome counters are NOT bounded (they're simple ints)."""
        diag = StreamDiagnostics(histogram_capacity=5)
        for _ in range(100):
            diag.record_outcome(STREAM_OUTCOME_COMPLETED)
        snap = diag.snapshot()
        assert snap["outcomes"][STREAM_OUTCOME_COMPLETED] == 100

    def test_concurrent_outcome_recording(self) -> None:
        diag = StreamDiagnostics(histogram_capacity=100)
        errors: list[Exception] = []

        def record_batch() -> None:
            try:
                for _ in range(100):
                    diag.record_outcome(STREAM_OUTCOME_COMPLETED, elapsed_ms=50)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_batch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        snap = diag.snapshot()
        assert snap["outcomes"][STREAM_OUTCOME_COMPLETED] == 400

    def test_snapshot_returns_all_expected_keys(self) -> None:
        diag = StreamDiagnostics()
        snap = diag.snapshot()
        assert "outcomes" in snap
        assert "completed_ms" in snap
        assert "client_cancel_ms" in snap
        assert "finalizer_timeout_ms" in snap
        assert "httpx_exception_counts" in snap
        assert "last_event" in snap
        assert "last_event_age_ms" in snap


class TestLagMonitorBounded:
    def test_snapshot_empty_by_default(self) -> None:
        monitor = EventLoopLagMonitor(cadence_s=1.0, window_size=10)
        snap = monitor.snapshot()
        assert snap.sample_count == 0
        assert snap.window_size == 10

    def test_record_sample_bounded(self) -> None:
        monitor = EventLoopLagMonitor(cadence_s=1.0, window_size=5)
        for _ in range(20):
            monitor._record_sample(1.5)
        snap = monitor.snapshot()
        assert snap.sample_count <= 5

    def test_to_dict_returns_all_keys(self) -> None:
        monitor = EventLoopLagMonitor(cadence_s=1.0)
        d = monitor.to_dict()
        expected_keys = {
            "window_size",
            "sample_count",
            "avg_ms",
            "min_ms",
            "max_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "loop_identity",
            "cadence_s",
            "last_sample_ts",
        }
        assert set(d.keys()) == expected_keys
