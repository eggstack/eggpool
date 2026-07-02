"""Tests for stable_prefix_content_hash (exact content hashing).

Re-extracts stable-prefix values from a payload using the
segmentation's stable-prefix segment paths, then hashes a
canonical representation.
"""

from __future__ import annotations

import re

from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
    segment_request,
    stable_prefix_content_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segmentation(segments: list[RequestSegment]) -> SegmentationResult:
    """Build a synthetic SegmentationResult from a list of segments."""
    counts: dict[SegmentKind, int] = {k: 0 for k in SegmentKind}
    for s in segments:
        counts[s.kind] += 1
    return SegmentationResult(
        status=SegmentationStatus.SEGMENTED,
        segments=tuple(segments),
        segment_count_by_kind=counts,
        stable_prefix_bytes=sum(
            s.byte_length for s in segments if s.kind is SegmentKind.STABLE_PREFIX
        ),
        semi_stable_bytes=sum(
            s.byte_length for s in segments if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
        ),
        volatile_bytes=sum(
            s.byte_length for s in segments if s.kind is SegmentKind.VOLATILE_SUFFIX
        ),
        stable_prefix_estimated_tokens=sum(
            s.estimated_tokens or 0
            for s in segments
            if s.kind is SegmentKind.STABLE_PREFIX
        )
        or None,
        semi_stable_estimated_tokens=sum(
            s.estimated_tokens or 0
            for s in segments
            if s.kind is SegmentKind.SEMI_STABLE_CONTEXT
        )
        or None,
        volatile_estimated_tokens=sum(
            s.estimated_tokens or 0
            for s in segments
            if s.kind is SegmentKind.VOLATILE_SUFFIX
        )
        or None,
        stable_prefix_hash="h",
        request_shape_hash="r",
        cache_control_present=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_segmentation_returns_empty_string() -> None:
    """Segmentation with no stable-prefix segments returns ''."""
    payload = {"messages": [{"role": "tool", "content": "out"}]}
    seg = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=0,
        content_path=("messages", 0, "content"),
        byte_length=5,
        estimated_tokens=2,
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )
    result = _segmentation([seg])
    assert stable_prefix_content_hash(payload, result) == ""


def test_same_payload_same_hash() -> None:
    """Same payload + segmentation produces the same hash."""
    payload = {
        "system": "Be helpful.",
        "messages": [{"role": "tool", "content": "output"}],
    }
    seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=12,
        estimated_tokens=3,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    result = _segmentation([seg])
    h1 = stable_prefix_content_hash(payload, result)
    h2 = stable_prefix_content_hash(payload, result)
    assert h1 == h2
    assert h1 != ""


def test_changed_system_content_changes_hash() -> None:
    """Changing stable-prefix content (system text) changes the hash."""
    base = {
        "system": "Original system.",
        "messages": [{"role": "tool", "content": "output"}],
    }
    changed = {
        "system": "Different system.",
        "messages": [{"role": "tool", "content": "output"}],
    }
    seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=16,
        estimated_tokens=4,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    result = _segmentation([seg])
    assert stable_prefix_content_hash(base, result) != stable_prefix_content_hash(
        changed, result
    )


def test_changed_tool_schema_changes_hash() -> None:
    """Changing a tool definition changes the hash."""
    base = {
        "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        "messages": [],
    }
    changed = {
        "tools": [{"type": "function", "function": {"name": "get_time"}}],
        "messages": [],
    }
    seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.TOOL_SCHEMA,
        message_index=None,
        content_path=("tools", 0),
        byte_length=50,
        estimated_tokens=10,
        protected=True,
        compressible_candidate=False,
        reason="tool_schema",
    )
    result = _segmentation([seg])
    assert stable_prefix_content_hash(base, result) != stable_prefix_content_hash(
        changed, result
    )


def test_changed_volatile_suffix_preserves_hash() -> None:
    """Changing only volatile-suffix content does not change the hash."""
    stable_seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=12,
        estimated_tokens=3,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    volatile_seg = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=1,
        content_path=("messages", 1, "content"),
        byte_length=10,
        estimated_tokens=3,
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )
    result = _segmentation([stable_seg, volatile_seg])

    base = {
        "system": "Be helpful.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "output A"},
        ],
    }
    changed = {
        "system": "Be helpful.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "output B (different)"},
        ],
    }
    assert stable_prefix_content_hash(base, result) == stable_prefix_content_hash(
        changed, result
    )


def test_cache_control_marker_skipped() -> None:
    """cache_control metadata segments (with None leaf) are skipped."""
    payload = {
        "system": "Be helpful.",
        "messages": [{"role": "tool", "content": "out"}],
    }
    seg_text = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=12,
        estimated_tokens=3,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    # cache_control segment has a path that resolves to None in the payload
    seg_cc = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.CACHE_CONTROL,
        message_index=None,
        content_path=("system", "cache_control"),
        byte_length=0,
        estimated_tokens=0,
        protected=True,
        compressible_candidate=False,
        reason="cache_control_present",
    )
    result = _segmentation([seg_text, seg_cc])
    h = stable_prefix_content_hash(payload, result)
    # The cache_control segment should be skipped; only the text segment
    # contributes to the hash.
    assert h != ""


def test_deterministic_across_calls() -> None:
    """Two calls with the same inputs produce identical hashes."""
    payload = {
        "system": "Instructions.",
        "messages": [{"role": "tool", "content": "data"}],
    }
    seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=12,
        estimated_tokens=3,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    result = _segmentation([seg])
    assert stable_prefix_content_hash(payload, result) == stable_prefix_content_hash(
        payload, result
    )


def test_openai_and_anthropic_hashes_differ() -> None:
    """Two structurally equivalent requests under OpenAI vs Anthropic
    produce different hashes because their stable-prefix paths differ."""
    openai_payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ],
    }
    anthropic_payload = {
        "model": "claude-sonnet-4",
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    openai_seg = segment_request(openai_payload, protocol="openai")
    anthropic_seg = segment_request(anthropic_payload, protocol="anthropic")

    openai_hash = stable_prefix_content_hash(openai_payload, openai_seg)
    anthropic_hash = stable_prefix_content_hash(anthropic_payload, anthropic_seg)

    # Both should have stable-prefix hashes (system content)
    assert openai_hash != ""
    assert anthropic_hash != ""
    # Different paths => different canonical entries => different hashes
    assert openai_hash != anthropic_hash


def test_content_hash_format() -> None:
    """Hash is 64-char lowercase hex."""
    payload = {
        "system": "Instructions.",
        "messages": [{"role": "tool", "content": "data"}],
    }
    seg = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=None,
        content_path=("system",),
        byte_length=12,
        estimated_tokens=3,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )
    result = _segmentation([seg])
    h = stable_prefix_content_hash(payload, result)
    assert re.fullmatch(r"[0-9a-f]{64}", h) is not None, f"bad hash format: {h!r}"


def test_hash_with_multiple_stable_segments() -> None:
    """Multiple stable-prefix segments are all included in the hash."""
    payload = {
        "system": "Instructions.",
        "tools": [{"type": "function", "function": {"name": "search"}}],
        "messages": [{"role": "tool", "content": "data"}],
    }
    segs = [
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.SYSTEM,
            message_index=None,
            content_path=("system",),
            byte_length=12,
            estimated_tokens=3,
            protected=True,
            compressible_candidate=False,
            reason="system",
        ),
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.TOOL_SCHEMA,
            message_index=None,
            content_path=("tools", 0),
            byte_length=40,
            estimated_tokens=8,
            protected=True,
            compressible_candidate=False,
            reason="tool_schema",
        ),
    ]
    result = _segmentation(segs)
    h = stable_prefix_content_hash(payload, result)
    assert h != ""
    assert re.fullmatch(r"[0-9a-f]{64}", h) is not None


def test_hash_changes_when_tool_added() -> None:
    """Adding a tool changes the hash even if system text is unchanged."""
    base = {
        "system": "Instructions.",
        "messages": [{"role": "tool", "content": "data"}],
    }
    with_tool = {
        "system": "Instructions.",
        "tools": [{"type": "function", "function": {"name": "search"}}],
        "messages": [{"role": "tool", "content": "data"}],
    }
    base_segs = [
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.SYSTEM,
            message_index=None,
            content_path=("system",),
            byte_length=12,
            estimated_tokens=3,
            protected=True,
            compressible_candidate=False,
            reason="system",
        ),
    ]
    with_tool_segs = [
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.SYSTEM,
            message_index=None,
            content_path=("system",),
            byte_length=12,
            estimated_tokens=3,
            protected=True,
            compressible_candidate=False,
            reason="system",
        ),
        RequestSegment(
            kind=SegmentKind.STABLE_PREFIX,
            source=SegmentSource.TOOL_SCHEMA,
            message_index=None,
            content_path=("tools", 0),
            byte_length=50,
            estimated_tokens=10,
            protected=True,
            compressible_candidate=False,
            reason="tool_schema",
        ),
    ]
    base_result = _segmentation(base_segs)
    with_tool_result = _segmentation(with_tool_segs)
    assert stable_prefix_content_hash(base, base_result) != stable_prefix_content_hash(
        with_tool, with_tool_result
    )
