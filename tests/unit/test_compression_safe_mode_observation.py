"""Phase 2 tests: safe-mode observation builder.

These tests pin the contract that :func:`build_safe_mode_observation`
fills the same shape the analyzer / finalizer / dashboard consume, so
a single safe-mode pass can stand in for the separate observe-mode
analyzer call without removing any operator-visible fields.
"""

from __future__ import annotations

import pytest

from eggpool.transcoder.compression.apply import (
    CompressionResult,
    SafeModeObservation,
    build_safe_mode_observation,
)


def _noop_result(payload: object) -> CompressionResult:
    """Build a no-op :class:`CompressionResult` directly."""
    return CompressionResult(
        applied=False,
        mode="safe",
        transformed_payload=payload,
        transform_count=0,
        transforms_by_reason={},
        original_tokens=0,
        compressed_tokens=0,
        savings_tokens=0,
        pre_stable_prefix_hash="",
        post_stable_prefix_hash="",
        stable_prefix_preserved=True,
        stable_prefix_shape_hash="",
        stable_prefix_content_hash="",
        warnings=(),
        latency_ms=0.0,
        reason_code_counts={},
        failed_fallback=False,
    )


def _applied_result(payload: object) -> CompressionResult:
    """Build a single-transform applied :class:`CompressionResult`."""
    return CompressionResult(
        applied=True,
        mode="safe",
        transformed_payload=payload,
        transform_count=1,
        transforms_by_reason={"log_compaction": 1},
        original_tokens=100,
        compressed_tokens=10,
        savings_tokens=90,
        pre_stable_prefix_hash="abc",
        post_stable_prefix_hash="abc",
        stable_prefix_preserved=True,
        stable_prefix_shape_hash="abc",
        stable_prefix_content_hash="abc",
        warnings=(),
        latency_ms=4.2,
        reason_code_counts={"log_compaction": 1},
        failed_fallback=False,
    )


class TestBuildSafeModeObservation:
    def test_noop_observation_carries_dashboard_fields(self) -> None:
        result = _noop_result({"hello": "world"})
        obs = build_safe_mode_observation(result)
        assert isinstance(obs, SafeModeObservation)
        # Dashboard consumes these via duck-typing in the finalizer.
        assert obs.mode == "safe"
        assert obs.candidate_count == 0
        assert obs.eligible_candidate_count == 0
        assert obs.suppressed_candidate_count == 0
        assert obs.estimated_original_tokens is None
        assert obs.estimated_compressed_tokens is None
        assert obs.estimated_savings_tokens is None
        assert obs.analyzer_latency_ms == 0.0
        assert obs.warnings == ()
        assert obs.reason_code_counts == {}

    def test_applied_observation_captures_token_savings(self) -> None:
        result = _applied_result({"hello": "mutated"})
        obs = build_safe_mode_observation(result)
        assert obs.mode == "safe"
        assert obs.candidate_count == 1
        assert obs.eligible_candidate_count == 1
        assert obs.suppressed_candidate_count == 0
        assert obs.estimated_original_tokens == 100
        assert obs.estimated_compressed_tokens == 10
        assert obs.estimated_savings_tokens == 90
        assert obs.transform_counts == {"log_compaction": 1}
        assert obs.reason_code_counts == {"log_compaction": 1}
        assert obs.analyzer_latency_ms == pytest.approx(4.2)

    def test_summary_json_marks_source_safe_apply(self) -> None:
        import json

        result = _applied_result({"hello": "mutated"})
        obs = build_safe_mode_observation(result)
        payload = json.loads(obs.to_summary_json())
        assert payload["mode"] == "safe"
        assert payload["source"] == "safe_apply"
        assert payload["estimated_savings_tokens"] == 90
        assert payload["candidates"] == []
