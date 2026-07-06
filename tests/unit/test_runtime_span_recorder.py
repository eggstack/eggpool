"""Tests for the fine-grained :class:`DispatchSpanRecorder`."""

from __future__ import annotations

import time

import pytest


class TestDispatchSpanRecorder:
    """Pure-unit tests for the per-span dispatch recorder."""

    def test_empty_snapshot_is_zero(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=4)
        snap = recorder.snapshot()
        assert snap["window_size"] == 4
        assert snap["spans"] == []

    def test_rejects_non_positive_window_size(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        with pytest.raises(ValueError, match="window_size must be at least 1"):
            DispatchSpanRecorder(window_size=0)

    def test_record_ns_ignores_zero_and_negative(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=4)
        recorder.record_ns("a", 0)
        recorder.record_ns("a", -5)
        snap = recorder.snapshot()
        assert snap["spans"] == []

    def test_record_ns_accumulates_per_span(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=10)
        recorder.record_ns("a", 1_500_000)
        recorder.record_ns("a", 2_500_000)
        recorder.record_ns("b", 5_000_000)
        snap = recorder.snapshot()
        spans = {row["span"]: row for row in snap["spans"]}
        assert "a" in spans and "b" in spans
        assert spans["a"]["sample_count"] == 2
        assert spans["a"]["avg_ms"] == 2.0
        assert spans["a"]["max_ms"] == 2.5
        assert spans["a"]["min_ms"] == 1.5
        assert spans["b"]["sample_count"] == 1
        assert spans["b"]["avg_ms"] == 5.0

    def test_bounded_window_drops_oldest(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=3)
        recorder.record_ns("a", 1_000_000)
        recorder.record_ns("a", 2_000_000)
        recorder.record_ns("a", 3_000_000)
        recorder.record_ns("a", 4_000_000)
        snap = recorder.snapshot()
        assert snap["spans"][0]["sample_count"] == 3
        assert snap["spans"][0]["min_ms"] == 2.0
        assert snap["spans"][0]["max_ms"] == 4.0

    def test_measure_context_manager_records(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=4)
        with recorder.measure("work"):
            time.sleep(0.001)
        snap = recorder.snapshot()
        assert snap["spans"][0]["span"] == "work"
        assert snap["spans"][0]["sample_count"] == 1
        assert snap["spans"][0]["min_ms"] is not None
        assert snap["spans"][0]["min_ms"] > 0

    def test_snapshot_for_spans_returns_missing_keys_with_none(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=4)
        recorder.record_ns("a", 1_500_000)
        snap = recorder.snapshot_for_spans(["a", "missing"])
        spans = {row["span"]: row for row in snap["spans"]}
        assert spans["a"]["sample_count"] == 1
        assert spans["missing"]["sample_count"] == 0
        assert spans["missing"]["avg_ms"] is None
        assert spans["missing"]["p95_ms"] is None

    def test_spans_lists_active_span_keys(self) -> None:
        from eggpool.runtime_dispatch import DispatchSpanRecorder

        recorder = DispatchSpanRecorder(window_size=4)
        recorder.record_ns("a", 1_000_000)
        recorder.record_ns("b", 2_000_000)
        keys = set(recorder.spans())
        assert keys == {"a", "b"}

    def test_all_span_keys_constant_lists_known_spans(self) -> None:
        from eggpool.runtime_dispatch import ALL_SPAN_KEYS

        for key in (
            "json_parse",
            "segmentation",
            "compression_apply",
            "selection_lock_wait",
            "routing_plan",
        ):
            assert key in ALL_SPAN_KEYS
        # Should be a finite, deterministic tuple.
        assert isinstance(ALL_SPAN_KEYS, tuple)
        assert all(isinstance(k, str) for k in ALL_SPAN_KEYS)
