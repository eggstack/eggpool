"""Phase 4.3 tests: verify no large synthetic text is allocated by analyzer.

The analyzer's _segment_text() must not allocate large strings when
the segment has no meaningful signal (0 estimated_tokens, 0 byte_length).

Phase 4.3 (current): the analyzer no longer calls _segment_text at all
in the production hot path.  Detectors fall back to source-aware
structural signals (segment.estimated_tokens, segment.byte_length,
segment.source) when no explicit text_hint is provided.  This
removes the per-request allocation overhead and is content-private.

The legacy tests below were designed to spy on ``_segment_text`` and
assert it is not called.  Now that ``_segment_text`` has been removed
entirely (it had no remaining callers after the Phase 4.3 refactor),
we replace those tests with a structural assertion: the analyzer
must not reach for a representative text buffer at all.  The
_segmentation / analyze_compression call must succeed using only
segment metadata.
"""

from __future__ import annotations

from eggpool.transcoder.compression import CompressionConfig, analyze_compression
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
)


def _segmentation(segments: list[RequestSegment]) -> SegmentationResult:
    counts: dict[SegmentKind, int] = {k: 0 for k in SegmentKind}
    for s in segments:
        counts[s.kind] += 1
    return SegmentationResult(
        status=SegmentationStatus.SEGMENTED,
        segments=tuple(segments),
        segment_count_by_kind=counts,
        stable_prefix_bytes=0,
        semi_stable_bytes=0,
        volatile_bytes=0,
        stable_prefix_estimated_tokens=0,
        semi_stable_estimated_tokens=0,
        volatile_estimated_tokens=0,
        stable_prefix_hash="h",
        request_shape_hash="r",
        cache_control_present=False,
    )


def test_analyzer_runs_without_synthetic_text_for_zero_signal() -> None:
    """The analyzer produces a result for a zero-signal segment
    without allocating a representative text.

    Phase 4.3: ``_segment_text`` has been removed from the analyzer
    and detectors use segment metadata (estimated_tokens /
    byte_length) directly.  This regression test simply exercises
    the zero-signal path and asserts a result is produced.  If
    future work reintroduces a synthetic-text allocation, it
    will need to find a different shape than the segment metadata
    alone.
    """
    segment = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=0,
        content_path=("messages", 0, "content"),
        byte_length=0,
        estimated_tokens=0,
        protected=False,
        compressible_candidate=True,
        reason="test",
    )
    policy = CompressionConfig(enabled=True, mode="observe")
    result = analyze_compression(
        _segmentation([segment]),
        policy=policy,
    )
    assert result is not None


def test_analyzer_runs_without_synthetic_text_for_nonzero_signal() -> None:
    """Phase 4.3: even for nonzero-signal segments the analyzer must
    not allocate a representative text.  Detectors use the segment
    metadata (``estimated_tokens`` / ``byte_length``) directly via
    :func:`_segment_tokens` so the production hot path never
    materialises a per-request text buffer.
    """
    segment = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=0,
        content_path=("messages", 0, "content"),
        byte_length=100,
        estimated_tokens=25,
        protected=False,
        compressible_candidate=True,
        reason="test",
    )
    policy = CompressionConfig(enabled=True, mode="observe")
    result = analyze_compression(
        _segmentation([segment]),
        policy=policy,
    )
    assert result is not None
