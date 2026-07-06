"""Phase 4.3 tests: verify no large synthetic text is allocated by analyzer.

The analyzer's _segment_text() should not allocate large strings when
the segment has no meaningful signal (0 estimated_tokens, 0 byte_length).
"""

from __future__ import annotations

from unittest.mock import patch

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


def test_no_segment_text_allocation_for_zero_signal() -> None:
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
    call_args: list[int] = []

    original_segment_text = __import__(
        "eggpool.transcoder.compression.analyzer", fromlist=["_segment_text"]
    )._segment_text

    def spy_segment_text(seg: RequestSegment) -> str:
        call_args.append(seg.estimated_tokens or 0)
        return original_segment_text(seg)

    with patch(
        "eggpool.transcoder.compression.analyzer._segment_text",
        spy_segment_text,
    ):
        result = analyze_compression(
            _segmentation([segment]),
            policy=policy,
        )
    assert result is not None
    assert not call_args


def test_segment_text_called_for_nonzero_signal() -> None:
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
    call_count = 0

    original_segment_text = __import__(
        "eggpool.transcoder.compression.analyzer", fromlist=["_segment_text"]
    )._segment_text

    def spy_segment_text(seg: RequestSegment) -> str:
        nonlocal call_count
        call_count += 1
        return original_segment_text(seg)

    with patch(
        "eggpool.transcoder.compression.analyzer._segment_text",
        spy_segment_text,
    ):
        result = analyze_compression(
            _segmentation([segment]),
            policy=policy,
        )
    assert result is not None
    assert call_count >= 1
