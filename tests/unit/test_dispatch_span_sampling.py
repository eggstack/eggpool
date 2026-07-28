"""Request-coherent dispatch-span sampling tests.

Verifies that:

- ``should_sample_request`` is deterministic and stable per request ID.
- Sampling rate produces the expected proportion of sampled requests.
- ``sampled_count`` and ``unsampled_count`` are incremented correctly.
- ``record_ns`` is a no-op when the ContextVar is set to ``False``.
- ``record_ns`` records unconditionally when the ContextVar is unset.
- Full sampling (rate=1.0) samples every request.
- Zero sampling (rate=0.0) samples no requests.
- No raw request IDs or exception messages are retained as metric keys.
"""

from __future__ import annotations

import hashlib

from eggpool.runtime_dispatch import DispatchSpanRecorder


class TestRequestCoherentSampling:
    """Deterministic, request-coherent sampling decision."""

    def test_full_sampling_samples_every_request(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        for i in range(100):
            assert recorder.should_sample_request(f"req-{i}") is True
        sampled, unsampled = recorder.sampled_unsampled_counts()
        assert sampled == 100
        assert unsampled == 0

    def test_zero_sampling_samples_no_requests(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.0)
        for i in range(100):
            assert recorder.should_sample_request(f"req-{i}") is False
        sampled, unsampled = recorder.sampled_unsampled_counts()
        assert sampled == 0
        assert unsampled == 100

    def test_sampling_is_deterministic_for_same_request_id(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.5)
        # Same request ID always produces the same decision
        decision1 = recorder.should_sample_request("req-deterministic")
        # Reset the ContextVar by calling again — the decision is
        # based on the hash, not on state, so it's the same.
        # We need a fresh recorder to avoid double-counting.
        recorder2 = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.5)
        decision2 = recorder2.should_sample_request("req-deterministic")
        assert decision1 == decision2

    def test_sampling_proportion_is_approximately_correct(self) -> None:
        """At 50% rate, roughly half of requests should be sampled."""
        recorder = DispatchSpanRecorder(
            window_size=10000, detailed_span_sample_rate=0.5
        )
        sampled = 0
        total = 1000
        for i in range(total):
            if recorder.should_sample_request(f"req-{i}"):
                sampled += 1
        # Allow some variance — should be within 10% of 50%
        assert 0.40 <= sampled / total <= 0.60

    def test_sampling_does_not_use_random_numbers(self) -> None:
        """The decision is based on a hash, not RNG."""
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.5)
        # Compute the expected decision manually
        request_id = "req-hash-test"
        digest = hashlib.sha256(request_id.encode("utf-8")).digest()
        val = int.from_bytes(digest[:8], "big") / (2**64)
        expected = val < 0.5
        actual = recorder.should_sample_request(request_id)
        assert actual == expected

    def test_sampled_and_unsampled_counts_incremented(self) -> None:
        recorder = DispatchSpanRecorder(
            window_size=10000, detailed_span_sample_rate=0.5
        )
        for i in range(100):
            recorder.should_sample_request(f"req-{i}")
        sampled, unsampled = recorder.sampled_unsampled_counts()
        assert sampled + unsampled == 100
        assert sampled > 0
        assert unsampled > 0


class TestRecordNsRespectsSampling:
    """``record_ns`` is gated by the ContextVar set by ``should_sample_request``."""

    def test_record_ns_skipped_when_not_sampled(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.0)
        recorder.should_sample_request("req-1")
        # ContextVar is set to False — record_ns should be a no-op
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        assert snap["spans"] == []

    def test_record_ns_records_when_sampled(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("req-1")
        # ContextVar is set to True — record_ns should record
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        assert len(snap["spans"]) == 1
        assert snap["spans"][0]["span"] == "test_span"
        assert snap["spans"][0]["sample_count"] == 1

    def test_record_ns_records_when_contextvar_unset(self) -> None:
        """When the ContextVar is unset (e.g. unit tests), record_ns
        records unconditionally for backward compatibility."""
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.5)
        # Don't call should_sample_request — ContextVar is unset (None)
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        assert len(snap["spans"]) == 1
        assert snap["spans"][0]["sample_count"] == 1

    def test_sampled_unsampled_counts_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=0.5)
        for i in range(10):
            recorder.should_sample_request(f"req-{i}")
        snap = recorder.snapshot()
        assert "sampled_count" in snap
        assert "unsampled_count" in snap
        assert snap["sampled_count"] + snap["unsampled_count"] == 10


class TestNoSensitiveDataRetained:
    """No raw request IDs, exception messages, or API keys as metric keys."""

    def test_span_keys_are_finite_constants(self) -> None:
        from eggpool.runtime_dispatch import ALL_SPAN_KEYS

        # Span keys must be a finite, deterministic tuple
        assert isinstance(ALL_SPAN_KEYS, tuple)
        assert len(ALL_SPAN_KEYS) > 0
        assert all(isinstance(k, str) for k in ALL_SPAN_KEYS)
        # No duplicates
        assert len(set(ALL_SPAN_KEYS)) == len(ALL_SPAN_KEYS)

    def test_no_raw_request_ids_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("secret-request-id-12345")
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        snap_str = str(snap)
        assert "secret-request-id-12345" not in snap_str

    def test_no_exception_messages_in_snapshot(self) -> None:
        recorder = DispatchSpanRecorder(window_size=10, detailed_span_sample_rate=1.0)
        recorder.should_sample_request("req-1")
        recorder.record_ns("test_span", 1_000_000)
        snap = recorder.snapshot()
        snap_str = str(snap)
        assert "Traceback" not in snap_str
        assert "Exception" not in snap_str
