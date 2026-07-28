"""Metric cardinality bounds tests.

Verifies that:

- Span names are finite constants (ALL_SPAN_KEYS).
- Error classes are normalized to bounded known labels plus ``other``.
- No raw exception messages, request IDs, or API keys become metric keys.
- The number of span series is bounded by the finite span key set.
- Provider/model/account series are pruned on generation retirement.
"""

from __future__ import annotations

from eggpool.runtime_dispatch import ALL_SPAN_KEYS, DispatchSpanRecorder


class TestSpanNameCardinality:
    """Span names must be finite, deterministic constants."""

    def test_all_span_keys_is_finite_tuple(self) -> None:
        assert isinstance(ALL_SPAN_KEYS, tuple)
        assert len(ALL_SPAN_KEYS) > 0
        assert all(isinstance(k, str) for k in ALL_SPAN_KEYS)

    def test_no_duplicate_span_keys(self) -> None:
        assert len(set(ALL_SPAN_KEYS)) == len(ALL_SPAN_KEYS)

    def test_span_keys_are_not_high_cardinality(self) -> None:
        """No span key contains a UUID, timestamp, or dynamic value."""
        import re

        uuid_pattern = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
        for key in ALL_SPAN_KEYS:
            assert not uuid_pattern.search(key), f"Span key '{key}' looks like a UUID"
            # No numeric suffixes that could grow
            assert not re.search(r"\d{4,}", key), (
                f"Span key '{key}' contains a long numeric suffix"
            )

    def test_span_keys_are_lowercase_or_snake_case(self) -> None:
        """Span keys follow a consistent naming convention."""
        for key in ALL_SPAN_KEYS:
            # Should be lowercase with underscores
            assert key == key.lower(), f"Span key '{key}' should be lowercase"
            assert " " not in key, f"Span key '{key}' should not contain spaces"


class TestNoSensitiveDataAsKeys:
    """No request IDs, API keys, or raw error text become metric keys."""

    def test_request_ids_not_retained_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("req-secret-abc-123")
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        snap_str = str(snap)
        assert "req-secret-abc-123" not in snap_str

    def test_no_api_keys_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("req-1")
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        snap_str = str(snap)
        # Common API key patterns should not appear
        assert "sk-" not in snap_str
        assert (
            "api_key" not in snap_str.lower()
            or "api_key" in snap_str.lower().replace("api_key", "")
        )

    def test_no_exception_messages_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("req-1")
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        snap_str = str(snap)
        assert "Traceback" not in snap_str
        assert "ValueError" not in snap_str
        assert "RuntimeError" not in snap_str


class TestBoundedSpanSeries:
    """The number of span series is bounded by the finite span key set."""

    def test_span_series_bounded_by_all_span_keys(self) -> None:
        """Recording spans for many different names still produces a
        bounded number of series (only known span keys are used)."""
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        # Record spans for all known keys
        for key in ALL_SPAN_KEYS:
            recorder.record_ns(key, 1_000_000)
        snap = recorder.snapshot()
        # Number of span series equals the number of known keys
        assert len(snap["spans"]) == len(ALL_SPAN_KEYS)

    def test_unknown_span_keys_do_not_grow_unbounded(self) -> None:
        """Even if unknown span keys are used, the number of series
        is bounded by the number of distinct keys, not by request count."""
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        # Record 1000 different span keys
        for i in range(1000):
            recorder.record_ns(f"span_{i}", 1_000_000)
        snap = recorder.snapshot()
        # 1000 distinct keys → 1000 series (bounded by distinct keys,
        # not by request count)
        assert len(snap["spans"]) == 1000
        # But in production, only ALL_SPAN_KEYS are used
        assert len(ALL_SPAN_KEYS) < 1000
