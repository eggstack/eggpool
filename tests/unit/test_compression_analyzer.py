"""Tests for observe-mode compression analyzer (Phase 4).

The analyzer is observational: it must never mutate the request
body, never change routing, and never synthesise provider cache
controls.  These tests cover the planned acceptance criteria:

- Disabled compression runs no analyzers.
- Observe mode records candidates / latency / reason codes.
- Cache-protected stable-prefix candidates are suppressed.
- Semi-stable candidates are suppressed under ``suffix_only``.
- Savings below threshold are suppressed.
- Latency budget warnings stop analysis cleanly.
- The observation never mutates the segmentation result.
"""

from __future__ import annotations

import json

import pytest

from eggpool.transcoder.compression import (
    REASON_BASE64_ELISION,
    REASON_BELOW_MIN_CANDIDATE_TOKENS,
    REASON_BELOW_MIN_SAVINGS_TOKENS,
    REASON_EMPTY_SEGMENT,
    REASON_JSON_MINIFY,
    REASON_LATENCY_BUDGET,
    REASON_LOG_COMPACTION,
    REASON_PLACEMENT,
    REASON_PROTECTED_CACHE_BOUNDARY,
    REASON_REPEATED_LINE_RUN,
    REASON_SEARCH_COMPACTION,
    REASON_STACK_TRACE_COMPACTION,
    REASON_STATIC_PREFIX,
    REASON_TRANSFORM_DISABLED,
    CompressionConfig,
    analyze_compression,
)
from eggpool.transcoder.compression.analyzer import (
    _detect_base64_blob,
    _detect_json_minify,
    _detect_log_compaction,
    _detect_repeated_lines,
    _detect_search_compaction,
    _detect_stack_trace,
)
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
    segment_request,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _seg(
    *,
    kind: SegmentKind,
    source: SegmentSource,
    protected: bool = False,
    byte_length: int = 0,
    estimated_tokens: int | None = None,
    compressible_candidate: bool | None = None,
) -> RequestSegment:
    """Build a :class:`RequestSegment` for analyzer tests.

    ``compressible_candidate`` mirrors the segmenter's flag; the
    analyzer honours the policy regardless of this hint, so the
    tests can exercise both branches.
    """
    return RequestSegment(
        kind=kind,
        source=source,
        message_index=None,
        content_path=("messages", 0, source.value),
        byte_length=byte_length,
        estimated_tokens=estimated_tokens,
        protected=protected,
        compressible_candidate=(
            compressible_candidate
            if compressible_candidate is not None
            else (kind is SegmentKind.VOLATILE_SUFFIX and not protected)
        ),
        reason="test",
    )


def _segmentation(segments: list[RequestSegment]) -> SegmentationResult:
    """Build a synthetic :class:`SegmentationResult` for tests."""
    stable_bytes = sum(
        s.byte_length for s in segments if s.kind is SegmentKind.STABLE_PREFIX
    )
    semi_bytes = sum(
        s.byte_length for s in segments if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
    )
    volatile_bytes = sum(
        s.byte_length for s in segments if s.kind is SegmentKind.VOLATILE_SUFFIX
    )
    stable_tokens = sum(
        s.estimated_tokens or 0 for s in segments if s.kind is SegmentKind.STABLE_PREFIX
    )
    semi_tokens = sum(
        s.estimated_tokens or 0
        for s in segments
        if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
    )
    volatile_tokens = sum(
        s.estimated_tokens or 0
        for s in segments
        if s.kind is SegmentKind.VOLATILE_SUFFIX
    )
    counts: dict[SegmentKind, int] = {k: 0 for k in SegmentKind}
    for s in segments:
        counts[s.kind] += 1
    return SegmentationResult(
        status=SegmentationStatus.SEGMENTED,
        segments=tuple(segments),
        segment_count_by_kind=counts,
        stable_prefix_bytes=stable_bytes,
        semi_stable_bytes=semi_bytes,
        volatile_bytes=volatile_bytes,
        stable_prefix_estimated_tokens=stable_tokens or None,
        semi_stable_estimated_tokens=semi_tokens or None,
        volatile_estimated_tokens=volatile_tokens or None,
        stable_prefix_hash="h",
        request_shape_hash="r",
        cache_control_present=False,
    )


def _segment_id(segment: RequestSegment, index: int) -> str:
    """Mirror the analyzer's :func:`_segment_id` for test hint maps."""
    path = ".".join(str(p) for p in segment.content_path) or f"seg{index}"
    return f"s{index}:{segment.kind.value}:{path}"


def _hints_for(
    segments: list[RequestSegment],
    text_by_index: dict[int, str],
) -> dict[str, str]:
    """Build a text-hints mapping keyed by analyzer segment id."""
    return {
        _segment_id(segment, index): text_by_index[index]
        for index, segment in enumerate(segments)
        if index in text_by_index
    }


@pytest.fixture
def enabled_policy() -> CompressionConfig:
    return CompressionConfig(
        enabled=True,
        mode="observe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=100.0,
    )


# ---------------------------------------------------------------------------
# Disabled / null paths
# ---------------------------------------------------------------------------


def test_disabled_policy_returns_none() -> None:
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.VOLATILE_SUFFIX,
                source=SegmentSource.TOOL_RESULT,
                byte_length=1024,
                estimated_tokens=512,
            )
        ]
    )
    result = analyze_compression(segmentation, policy=CompressionConfig(enabled=False))
    assert result is None


def test_none_segmentation_returns_none() -> None:
    result = analyze_compression(None, policy=CompressionConfig(enabled=True))
    assert result is None


def test_analyzer_never_mutates_segmentation(enabled_policy: CompressionConfig) -> None:
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.TOOL_RESULT,
            byte_length=1024,
            estimated_tokens=512,
        )
    ]
    segmentation = _segmentation(segments)
    snapshot = (segmentation.segments, segmentation.stable_prefix_hash)
    analyze_compression(segmentation, policy=enabled_policy)
    # The segment list and hashes are frozen; identity check
    # confirms the analyzer did not attempt to replace them.
    assert segmentation.segments is snapshot[0]
    assert segmentation.stable_prefix_hash == snapshot[1]


# ---------------------------------------------------------------------------
# Cache-boundary suppression
# ---------------------------------------------------------------------------


def test_protected_stable_prefix_candidates_are_suppressed(
    enabled_policy: CompressionConfig,
) -> None:
    """Stable-prefix system / tool-schema segments are protected.

    The analyzer must suppress every candidate and record the
    suppression under ``protected_cache_boundary``.
    """
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.STABLE_PREFIX,
                source=SegmentSource.SYSTEM,
                protected=True,
                byte_length=8192,
                estimated_tokens=2048,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=enabled_policy)
    assert observation is not None
    assert observation.candidate_count == 0
    assert observation.reason_code_counts.get(REASON_PROTECTED_CACHE_BOUNDARY, 0) > 0


def test_semi_stable_candidates_suppressed_under_suffix_only(
    enabled_policy: CompressionConfig,
) -> None:
    """``placement = "suffix_only"`` rejects non-volatile segments.

    The analyzer records a suppression under ``placement`` for
    every transform applied to a semi-stable segment.
    """
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.SEMI_STABLE_CONTEXT,
                source=SegmentSource.PRIOR_MESSAGE,
                protected=False,
                byte_length=4096,
                estimated_tokens=1024,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=enabled_policy)
    assert observation is not None
    assert observation.candidate_count == 0
    assert observation.reason_code_counts.get(REASON_PLACEMENT, 0) > 0


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_savings_below_threshold_suppressed() -> None:
    """Candidates whose estimated savings are below the threshold
    are recorded as suppressed under
    ``below_min_savings_tokens``."""
    policy = CompressionConfig(
        enabled=True,
        min_candidate_tokens=0,
        min_savings_tokens=10_000_000,
    )
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.VOLATILE_SUFFIX,
                source=SegmentSource.TOOL_RESULT,
                byte_length=128,
                estimated_tokens=64,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=policy)
    assert observation is not None
    assert observation.eligible_candidate_count == 0
    assert observation.suppressed_candidate_count > 0
    assert observation.reason_code_counts.get(REASON_BELOW_MIN_SAVINGS_TOKENS, 0) > 0


def test_min_candidate_tokens_threshold() -> None:
    """Candidates smaller than ``min_candidate_tokens`` are recorded
    as suppressed under ``below_min_candidate_tokens``."""
    policy = CompressionConfig(
        enabled=True,
        min_candidate_tokens=10_000_000,
        min_savings_tokens=0,
    )
    repeated_line = "the same line over and over"
    text = "\n".join([repeated_line] * 16)
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.VOLATILE_SUFFIX,
                source=SegmentSource.TOOL_RESULT,
                byte_length=len(text),
                estimated_tokens=len(text) // 4,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=policy)
    assert observation is not None
    assert observation.eligible_candidate_count == 0
    assert observation.reason_code_counts.get(REASON_BELOW_MIN_CANDIDATE_TOKENS, 0) > 0


# ---------------------------------------------------------------------------
# Latency budget
# ---------------------------------------------------------------------------


def test_latency_budget_records_warning_and_stops() -> None:
    """When the latency budget is exhausted, the analyzer records
    a warning and stops cleanly.  The observation must still be
    returned with the partial work it performed."""
    policy = CompressionConfig(
        enabled=True,
        max_compression_latency_ms=0.0,
        min_candidate_tokens=0,
        min_savings_tokens=0,
    )
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.TOOL_RESULT,
            byte_length=4096,
            estimated_tokens=1024,
        )
        for _ in range(8)
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(segmentation, policy=policy)
    assert observation is not None
    # The deadline is reached before any candidate is recorded
    # because the per-segment loop checks the budget before
    # analysis.  Note: with ``max_compression_latency_ms = 0``
    # the deadline is in the past, so the first segment loop
    # iteration trips the warning and we never reach the
    # per-transform analyser.  Suppressed candidates from
    # transforms are not recorded in this path.
    assert observation.candidate_count == 0
    assert REASON_LATENCY_BUDGET in observation.reason_code_counts
    assert any(REASON_LATENCY_BUDGET in w for w in observation.warnings)


# ---------------------------------------------------------------------------
# Transform detection
# ---------------------------------------------------------------------------


def test_repeated_lines_detected_in_volatile_suffix(
    enabled_policy: CompressionConfig,
) -> None:
    repeated_line = "the same line over and over"
    text = "\n".join([repeated_line] * 16)
    estimated = len(text) // 4
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.COMMAND_OUTPUT,
            protected=False,
            byte_length=len(text),
            estimated_tokens=estimated,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    transforms = {c.transform for c in observation.candidates}
    assert "fold_repeated_lines" in transforms
    assert observation.reason_code_counts.get(REASON_REPEATED_LINE_RUN, 0) > 0


def test_log_compaction_detected_in_volatile_suffix(
    enabled_policy: CompressionConfig,
) -> None:
    text = (
        "\n".join([f"INFO line {i}" for i in range(40)])
        + "\nERROR: something went wrong\n"
        + "\n".join([f"INFO line {i}" for i in range(40, 80)])
    )
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.COMMAND_OUTPUT,
            protected=False,
            byte_length=len(text),
            estimated_tokens=len(text) // 4,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    assert observation.transform_counts.get("compact_logs", 0) > 0
    assert observation.reason_code_counts.get(REASON_LOG_COMPACTION, 0) > 0


def test_search_result_compaction_detected(
    enabled_policy: CompressionConfig,
) -> None:
    line_blocks: list[str] = []
    for i in range(8):
        line_blocks.append(f"src/foo.py:{i}:def func_{i}():")
        line_blocks.append(f"    return {i}")
        line_blocks.append("diff --git a/x.py b/x.py")
        line_blocks.append("@@ -1,1 +1,1 @@")
        line_blocks.append("-old line")
        line_blocks.append("+new line")
    text = "\n".join(line_blocks)
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.SEARCH_RESULT,
            protected=False,
            byte_length=len(text),
            estimated_tokens=len(text) // 4,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    assert observation.transform_counts.get("compact_search_results", 0) > 0
    assert observation.reason_code_counts.get(REASON_SEARCH_COMPACTION, 0) > 0


def test_base64_blob_detected_in_volatile_suffix(
    enabled_policy: CompressionConfig,
) -> None:
    blob_line = "A" * 600 + "=" * 2
    text = blob_line
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.BLOB,
            protected=False,
            byte_length=len(text),
            estimated_tokens=len(text) // 4,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    assert observation.transform_counts.get("elide_base64_blobs", 0) > 0
    assert observation.reason_code_counts.get(REASON_BASE64_ELISION, 0) > 0


def test_machine_json_minify_detected(
    enabled_policy: CompressionConfig,
) -> None:
    inner = {f"key_{i}": i for i in range(40)}
    payload = {"a": inner, "b": [1, 2, 3, 4, 5]}
    text = json.dumps(payload, indent=2)
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.BLOB,
            protected=False,
            byte_length=len(text),
            estimated_tokens=len(text) // 4,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    assert observation.transform_counts.get("minify_machine_json", 0) > 0
    assert observation.reason_code_counts.get(REASON_JSON_MINIFY, 0) > 0


def test_stack_trace_compaction_detected(
    enabled_policy: CompressionConfig,
) -> None:
    frame_lines: list[str] = []
    for i in range(20):
        frame_lines.append('  File "/app/foo.py", line 1, in func_a')
        frame_lines.append(f"    do_thing({i})")
    text = (
        "Traceback (most recent call last):\n"
        + "\n".join(frame_lines)
        + "\nException: boom\n"
    )
    segments = [
        _seg(
            kind=SegmentKind.VOLATILE_SUFFIX,
            source=SegmentSource.COMMAND_OUTPUT,
            protected=False,
            byte_length=len(text),
            estimated_tokens=len(text) // 4,
        )
    ]
    segmentation = _segmentation(segments)
    observation = analyze_compression(
        segmentation,
        policy=enabled_policy,
        text_hints=_hints_for(segments, {0: text}),
    )
    assert observation is not None
    assert observation.transform_counts.get("compact_stack_traces", 0) > 0
    assert observation.reason_code_counts.get(REASON_STACK_TRACE_COMPACTION, 0) > 0


# ---------------------------------------------------------------------------
# Transform toggles
# ---------------------------------------------------------------------------


def test_disabled_transform_records_transform_disabled() -> None:
    transforms = type(CompressionConfig().transforms)(
        fold_repeated_lines=False,
        compact_logs=False,
        compact_search_results=False,
        elide_base64_blobs=False,
        minify_machine_json=False,
        compact_stack_traces=False,
    )
    policy = CompressionConfig(enabled=True, transforms=transforms)
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.VOLATILE_SUFFIX,
                source=SegmentSource.TOOL_RESULT,
                byte_length=4096,
                estimated_tokens=1024,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=policy)
    assert observation is not None
    assert observation.candidate_count == 0
    assert observation.reason_code_counts.get(REASON_TRANSFORM_DISABLED, 0) > 0


# ---------------------------------------------------------------------------
# Observation serialization
# ---------------------------------------------------------------------------


def test_observation_to_summary_json_round_trips() -> None:
    """``to_summary_json`` produces JSON that parses cleanly and
    preserves the candidate breakdown."""
    observation = analyze_compression(
        _segmentation(
            [
                _seg(
                    kind=SegmentKind.VOLATILE_SUFFIX,
                    source=SegmentSource.COMMAND_OUTPUT,
                    byte_length=4096,
                    estimated_tokens=1024,
                )
            ]
        ),
        policy=CompressionConfig(
            enabled=True, min_candidate_tokens=0, min_savings_tokens=0
        ),
    )
    assert observation is not None
    raw = observation.to_summary_json()
    parsed = json.loads(raw)
    assert parsed["mode"] == "observe"
    assert "candidate_count" in parsed
    assert "reason_code_counts" in parsed
    assert "candidates" in parsed
    assert isinstance(parsed["candidates"], list)


# ---------------------------------------------------------------------------
# Integration with Phase 2 segmenter
# ---------------------------------------------------------------------------


def test_end_to_end_observation_via_phase2_segmenter(
    enabled_policy: CompressionConfig,
) -> None:
    """The analyzer accepts the output of ``segment_request``."""
    body = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {
                "role": "user",
                "content": "Run the test suite and show the output.",
            },
            {
                "role": "tool",
                "content": "FAILED test_foo\n"
                + "Traceback (most recent call last):\n"
                + '  File "/app/foo.py", line 1, in bar\n'
                + "    raise Exception()\n"
                + "Exception:\n"
                + "\n".join([f"INFO line {i}" for i in range(60)]),
            },
        ],
    }
    segmentation = segment_request(body, protocol="openai")
    assert segmentation is not None
    observation = analyze_compression(segmentation, policy=enabled_policy)
    assert observation is not None
    # The tool output is volatile_suffix, the system is
    # stable_prefix.  Expect at least one transform to fire on
    # the tool output and at least one suppression on the system.
    assert observation.candidate_count > 0
    assert observation.reason_code_counts.get(REASON_PROTECTED_CACHE_BOUNDARY, 0) > 0
    assert observation.analyzer_latency_ms >= 0


# ---------------------------------------------------------------------------
# Static prefix flag (validate at config layer, never reachable
# in observe mode)
# ---------------------------------------------------------------------------


def test_static_prefix_candidates_recorded_when_respect_cache_boundaries_false() -> (
    None
):
    """With ``respect_cache_boundaries = false`` the cache-boundary
    protection does not fire; ``stable_prefix`` is still suppressed
    because ``compress_static_prefix`` defaults to ``false`` in
    observe mode."""
    policy = CompressionConfig(
        enabled=True,
        respect_cache_boundaries=False,
        placement="suffix_only",
    )
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.STABLE_PREFIX,
                source=SegmentSource.SYSTEM,
                protected=True,
                byte_length=4096,
                estimated_tokens=1024,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=policy)
    assert observation is not None
    # Stable prefix is gated by compress_static_prefix (the
    # cache-boundary flag is disabled by the test policy).
    assert observation.reason_code_counts.get(REASON_STATIC_PREFIX, 0) > 0


def test_analyzer_handles_empty_segment_text(enabled_policy: CompressionConfig) -> None:
    """Segments with no estimated text still produce a clean observation."""
    segmentation = _segmentation(
        [
            _seg(
                kind=SegmentKind.VOLATILE_SUFFIX,
                source=SegmentSource.UNKNOWN,
                protected=False,
                byte_length=0,
                estimated_tokens=0,
            )
        ]
    )
    observation = analyze_compression(segmentation, policy=enabled_policy)
    assert observation is not None
    assert observation.candidate_count == 0
    assert observation.reason_code_counts.get(REASON_EMPTY_SEGMENT, 0) > 0


# ---------------------------------------------------------------------------
# Detector unit tests (no policy, no segmentation)
# ---------------------------------------------------------------------------


def test_detect_repeated_lines_under_min_run() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
    )
    assert _detect_repeated_lines(segment, "a\nb\nc\nd") == 0


def test_detect_repeated_lines_returns_zero_for_short_input() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
    )
    assert _detect_repeated_lines(segment, "") == 0
    assert _detect_repeated_lines(segment, "a") == 0


def test_detect_log_compaction_handles_short_input() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.COMMAND_OUTPUT,
    )
    assert _detect_log_compaction(segment, "") == 0
    assert _detect_log_compaction(segment, "line 1\nline 2") == 0


def test_detect_search_compaction_handles_unknown_shape() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
    )
    assert _detect_search_compaction(segment, "hello world") == 0


def test_detect_base64_blob_handles_short_text() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.BLOB,
    )
    assert _detect_base64_blob(segment, "") == 0
    assert _detect_base64_blob(segment, "short text") == 0


def test_detect_json_minify_handles_invalid_json() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.BLOB,
    )
    assert _detect_json_minify(segment, "not json {") == 0
    assert _detect_json_minify(segment, "") == 0


def test_detect_stack_trace_handles_plain_text() -> None:
    segment = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.COMMAND_OUTPUT,
    )
    assert _detect_stack_trace(segment, "nothing to see here") == 0
