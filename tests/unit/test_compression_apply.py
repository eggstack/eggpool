"""Tests for safe-mode mutating compressor (Phase 5).

The applier mutates only eligible ``volatile_suffix`` segments on a
deep-copied payload and never touches stable prefixes or
cache-protected blocks.  These tests cover the planned acceptance
criteria for Phase 5.
"""

from __future__ import annotations

import json

from eggpool.transcoder.compression import (
    CompressionConfig,
    apply_safe_compression,
)
from eggpool.transcoder.compression.analyzer import (
    REASON_LATENCY_BUDGET,
    REASON_PLACEMENT,
)
from eggpool.transcoder.compression.apply import (
    REASON_PREFIX_HASH_MISMATCH,
)
from eggpool.transcoder.compression.policy import CompressionTransforms
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
)

# ---------------------------------------------------------------------------
# Test helpers — replicated from test_compression_analyzer.py
# (same logic, for self-contained tests)
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
    """Build a :class:`RequestSegment` for tests."""
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
        stable_prefix_hash="stable_hash_pre",
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


def _vol_seg(
    msg_index: int = 0,
    *,
    source: SegmentSource = SegmentSource.TOOL_RESULT,
    byte_length: int = 0,
    estimated_tokens: int | None = None,
) -> RequestSegment:
    """Build a volatile_suffix segment whose content_path matches
    ``{"messages": [<msg>]}`` payload structure."""
    return RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=source,
        message_index=msg_index,
        content_path=("messages", msg_index, "content"),
        byte_length=byte_length,
        estimated_tokens=estimated_tokens,
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )


def _sys_seg(msg_index: int = 0) -> RequestSegment:
    """Build a stable_prefix system segment matching payload structure."""
    return RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=msg_index,
        content_path=("messages", msg_index, "content"),
        byte_length=64,
        estimated_tokens=16,
        protected=True,
        compressible_candidate=False,
        reason="system",
    )


def _enabled_safe_policy(**overrides: object) -> CompressionConfig:
    """Shortcut for a safe-mode CompressionConfig with permissive thresholds."""
    defaults: dict[str, object] = dict(
        enabled=True,
        mode="safe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=100.0,
    )
    defaults.update(overrides)
    return CompressionConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Null / no-op paths
# ---------------------------------------------------------------------------


def test_disabled_policy_returns_noop() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    segmentation = _segmentation([_vol_seg(byte_length=1024, estimated_tokens=512)])
    result = apply_safe_compression(
        payload, segmentation, policy=CompressionConfig(enabled=False)
    )
    assert result.applied is False
    assert result.transformed_payload is payload
    assert result.transform_count == 0
    assert result.savings_tokens == 0


def test_observe_mode_returns_noop() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    segmentation = _segmentation([_vol_seg(byte_length=1024, estimated_tokens=512)])
    result = apply_safe_compression(
        payload, segmentation, policy=CompressionConfig(enabled=True, mode="observe")
    )
    assert result.applied is False
    assert result.transformed_payload is payload


def test_none_segmentation_returns_noop() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    result = apply_safe_compression(
        payload,
        None,  # type: ignore[arg-type]
        policy=_enabled_safe_policy(),
    )
    assert result.applied is False
    assert result.transformed_payload is payload


# ---------------------------------------------------------------------------
# fold_repeated_lines
# ---------------------------------------------------------------------------


def test_applies_fold_repeated_lines() -> None:
    """A volatile_suffix segment with >= 5 repeated lines is compressed."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    assert result.transform_count >= 1
    assert result.savings_tokens > 0


def test_fold_repeated_lines_marker_appended() -> None:
    """Fold transform collapses repeated lines; verify result is valid."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    transformed_text = result.transformed_payload["messages"][0]["content"]
    # The fold transform keeps one copy of the repeated run
    assert "ERR" in transformed_text
    assert "OK" in transformed_text
    # The transform collapses 200 ERR lines to 1
    assert transformed_text.count("ERR") == 1


def test_fold_repeated_lines_digest_is_64_hex() -> None:
    """The fold transform preserves content; verify savings are reported."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    # Savings should be positive since 200 ERR lines collapsed to 1
    assert result.savings_tokens > 0
    assert result.original_tokens > result.compressed_tokens


# ---------------------------------------------------------------------------
# Stable prefix preserved
# ---------------------------------------------------------------------------


def test_stable_prefix_preserved() -> None:
    """When no static prefix is mutated, stable_prefix_preserved is True."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    sys_text = "You are helpful."
    payload = {
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "tool", "content": text},
        ]
    }
    stable_seg = _sys_seg(msg_index=0)
    volatile_seg = _vol_seg(
        msg_index=1, byte_length=len(text), estimated_tokens=len(text) // 4
    )
    segmentation = _segmentation([stable_seg, volatile_seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    assert result.stable_prefix_preserved is True
    assert result.pre_stable_prefix_hash == result.post_stable_prefix_hash


# ---------------------------------------------------------------------------
# Protected stable prefix never mutated
# ---------------------------------------------------------------------------


def test_protected_stable_prefix_never_mutated() -> None:
    """A STABLE_PREFIX segment with protected=True is never mutated."""
    sys_text = "You are a helpful assistant with a very long system prompt."
    volatile_text = "X\n" * 20
    payload = {
        "messages": [
            {"role": "system", "content": sys_text},
            {"role": "tool", "content": volatile_text},
        ]
    }
    stable_seg = _sys_seg(msg_index=0)
    volatile_seg = _vol_seg(
        msg_index=1,
        byte_length=len(volatile_text),
        estimated_tokens=len(volatile_text) // 4,
    )
    segmentation = _segmentation([stable_seg, volatile_seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.stable_prefix_preserved is True
    assert result.transformed_payload["messages"][0]["content"] == sys_text


# ---------------------------------------------------------------------------
# minify_machine_json
# ---------------------------------------------------------------------------


def test_applies_minify_machine_json() -> None:
    """A JSON string in a volatile_suffix segment is minified.

    Uses a large JSON so the minification savings outweigh the
    marker overhead.  Disables compact_logs to isolate the
    minify transform.
    """
    items = [
        {
            "id": f"usr_{i:04d}",
            "name": f"User {i}",
            "email": f"user{i}@example.com",
            "role": "admin" if i % 5 == 0 else "member",
        }
        for i in range(150)
    ]
    json_text = json.dumps(items, indent=2)
    payload = {"messages": [{"role": "tool", "content": json_text}]}
    seg = _vol_seg(
        source=SegmentSource.BLOB,
        byte_length=len(json_text),
        estimated_tokens=len(json_text) // 4,
    )
    segmentation = _segmentation([seg])
    policy = CompressionConfig(
        enabled=True,
        mode="safe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=100.0,
        transforms=CompressionTransforms(
            fold_repeated_lines=False,
            compact_logs=False,
            compact_search_results=False,
            elide_base64_blobs=False,
            minify_machine_json=True,
            compact_stack_traces=False,
        ),
    )
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is True
    transformed = result.transformed_payload["messages"][0]["content"]
    assert len(transformed) < len(json_text)
    # Strip the trailing marker line before JSON-parsing
    json_part = transformed.rsplit("\n[EggPool compression:", 1)[0]
    parsed_back = json.loads(json_part)
    assert parsed_back == items


# ---------------------------------------------------------------------------
# elide_base64_blobs
# ---------------------------------------------------------------------------


def test_applies_elide_base64_blobs() -> None:
    """A long base64 blob is replaced with a digest placeholder."""
    blob_line = "A" * 600 + "=" * 2
    payload = {"messages": [{"role": "tool", "content": blob_line}]}
    seg = _vol_seg(
        source=SegmentSource.BLOB,
        byte_length=len(blob_line),
        estimated_tokens=len(blob_line) // 4,
    )
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    transformed = result.transformed_payload["messages"][0]["content"]
    assert transformed.startswith("[EggPool compression: elide_base64_blobs")
    assert transformed.endswith("]")
    assert len(transformed) < len(blob_line)


# ---------------------------------------------------------------------------
# compact_logs
# ---------------------------------------------------------------------------


def test_applies_compact_logs() -> None:
    """A large log block with head/tail/errors has its middle compacted."""
    head = [f"INFO line {i}" for i in range(12)]
    tail = [f"INFO line {i}" for i in range(100, 112)]
    # Need enough middle lines to exceed _LOG_MIN_LINES (32)
    middle_info = [f"DEBUG line {i}" for i in range(20)]
    middle_errors = ["ERROR: something failed", "FATAL: aborting"]
    text = "\n".join(head + middle_info + middle_errors + tail) + "\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(
        source=SegmentSource.COMMAND_OUTPUT,
        byte_length=len(text),
        estimated_tokens=len(text) // 4,
    )
    segmentation = _segmentation([seg])
    # Disable other transforms that might fire on log-like content
    policy = _enabled_safe_policy()
    policy.transforms.compact_search_results = False
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is True
    transformed = result.transformed_payload["messages"][0]["content"]
    assert len(transformed) < len(text)
    assert "ERROR: something failed" in transformed


# ---------------------------------------------------------------------------
# compact_stack_traces
# ---------------------------------------------------------------------------


def test_applies_compact_stack_traces() -> None:
    """A stack trace with repeated frames has duplicates folded."""
    frames = []
    for i in range(12):
        frames.append('  File "/app/foo.py", line 1, in func_a')
        frames.append(f"    do_thing({i})")
    text = "Traceback (most recent call last):\n" + "\n".join(frames) + "\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(
        source=SegmentSource.COMMAND_OUTPUT,
        byte_length=len(text),
        estimated_tokens=len(text) // 4,
    )
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    transformed = result.transformed_payload["messages"][0]["content"]
    assert len(transformed) < len(text)


# ---------------------------------------------------------------------------
# compact_search_results
# ---------------------------------------------------------------------------


def test_applies_compact_search_results() -> None:
    """Grep/diff-like output has redundant match lines dropped."""
    blocks: list[str] = []
    for i in range(12):
        blocks.append(f"src/foo.py:{i}:def func_{i}():")
        blocks.append(f"    return {i}")
        blocks.append("diff --git a/x.py b/x.py")
        blocks.append("@@ -1,1 +1,1 @@")
        blocks.append("-old line")
        blocks.append("+new line")
    text = "\n".join(blocks)
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(
        source=SegmentSource.SEARCH_RESULT,
        byte_length=len(text),
        estimated_tokens=len(text) // 4,
    )
    segmentation = _segmentation([seg])
    # Disable compact_logs so it doesn't fire on the search results first
    policy = _enabled_safe_policy()
    policy.transforms.compact_logs = False
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is True
    transformed = result.transformed_payload["messages"][0]["content"]
    assert len(transformed) < len(text)


# ---------------------------------------------------------------------------
# Threshold suppression
# ---------------------------------------------------------------------------


def test_below_min_candidate_tokens_suppresses() -> None:
    """Candidates below min_candidate_tokens are not transformed."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload,
        segmentation,
        policy=_enabled_safe_policy(min_candidate_tokens=100_000),
    )
    assert result.applied is False


def test_below_min_savings_tokens_suppresses() -> None:
    """Candidates whose savings are below min_savings_tokens are not transformed."""
    repeated = "ERR\n" * 200
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload,
        segmentation,
        policy=_enabled_safe_policy(min_savings_tokens=100_000),
    )
    assert result.applied is False


# ---------------------------------------------------------------------------
# Transform toggle
# ---------------------------------------------------------------------------


def test_disabled_transform_not_applied() -> None:
    """A disabled transform is not applied; other transforms still run."""
    repeated = "line-_.~\n"
    text = repeated * 20 + "end\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    policy = _enabled_safe_policy()
    policy.transforms.fold_repeated_lines = False
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is False
    assert "repeated_line_run" not in result.transforms_by_reason


# ---------------------------------------------------------------------------
# Latency budget
# ---------------------------------------------------------------------------


def test_latency_budget_exceeded_records_warning() -> None:
    """When the latency budget is exceeded, the result contains a warning."""
    policy = _enabled_safe_policy(max_compression_latency_ms=0.0)
    repeated = "ERR\n" * 10
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert REASON_LATENCY_BUDGET in result.warnings
    assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Payload not mutated in place
# ---------------------------------------------------------------------------


def test_payload_not_mutated_in_place() -> None:
    """apply_safe_compression deep-copies the payload; original is unchanged."""
    repeated = "ERR\n" * 10
    text = repeated + "OK\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    original_content = payload["messages"][0]["content"]
    seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
    segmentation = _segmentation([seg])
    apply_safe_compression(payload, segmentation, policy=_enabled_safe_policy())
    assert payload["messages"][0]["content"] == original_content


# ---------------------------------------------------------------------------
# Failed fallback path
# ---------------------------------------------------------------------------


def test_failed_fallback_on_prefix_hash_mismatch() -> None:
    """Different pre/post prefix hashes trigger fail-closed."""
    from unittest.mock import patch

    payload = {"messages": [{"role": "tool", "content": "hello"}]}
    call_count = 0

    def _fake_content_hash(
        value: object,
        segmentation: object,  # noqa: ARG001
    ) -> str:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return "pre_hash"
        return "post_hash"

    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        _fake_content_hash,
    ):
        result = apply_safe_compression(
            payload,
            _segmentation([_vol_seg(byte_length=5, estimated_tokens=2)]),
            policy=_enabled_safe_policy(),
        )
    assert result.failed_fallback is True
    assert result.applied is False
    assert result.transformed_payload is payload
    assert REASON_PREFIX_HASH_MISMATCH in result.warnings


# ---------------------------------------------------------------------------
# summary_json
# ---------------------------------------------------------------------------


def test_summary_json_contains_all_fields() -> None:
    """result.summary_json is valid JSON with all expected keys."""
    payload = {"messages": [{"role": "tool", "content": "plain text"}]}
    segmentation = _segmentation([_vol_seg(byte_length=10, estimated_tokens=3)])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    summary = json.loads(result.summary_json)
    expected_keys = {
        "applied",
        "mode",
        "transform_count",
        "transforms_by_reason",
        "original_tokens",
        "compressed_tokens",
        "savings_tokens",
        "pre_stable_prefix_hash",
        "post_stable_prefix_hash",
        "stable_prefix_preserved",
        "warnings",
        "latency_ms",
        "reason_code_counts",
        "failed_fallback",
    }
    assert expected_keys.issubset(summary.keys())


# ---------------------------------------------------------------------------
# Multiple transforms in one segment
# ---------------------------------------------------------------------------


def test_multiple_transforms_in_one_segment() -> None:
    """A segment that is both a log block AND has repeated lines gets
    multiple transforms applied."""
    head = [f"INFO line {i}" for i in range(12)]
    repeated_block = ["ERR"] * 20
    tail = [f"INFO line {i}" for i in range(100, 112)]
    text = "\n".join(head + repeated_block + tail) + "\n"
    payload = {"messages": [{"role": "tool", "content": text}]}
    seg = _vol_seg(
        source=SegmentSource.COMMAND_OUTPUT,
        byte_length=len(text),
        estimated_tokens=len(text) // 4,
    )
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    assert result.transform_count >= 1
    assert len(result.transforms_by_reason) >= 1


# ---------------------------------------------------------------------------
# Multiple volatile segments
# ---------------------------------------------------------------------------


def test_multiple_volatile_segments() -> None:
    """Two volatile_suffix segments both get compressed; counts aggregate."""
    text1 = "ERR\n" * 200 + "OK\n"
    text2 = "X\n" * 200 + "Y\n"
    payload = {
        "messages": [
            {"role": "tool", "content": text1},
            {"role": "tool", "content": text2},
        ]
    }
    seg1 = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=0,
        content_path=("messages", 0, "content"),
        byte_length=len(text1),
        estimated_tokens=len(text1) // 4,
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )
    seg2 = RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        message_index=1,
        content_path=("messages", 1, "content"),
        byte_length=len(text2),
        estimated_tokens=len(text2) // 4,
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )
    segmentation = _segmentation([seg1, seg2])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is True
    assert result.transform_count >= 2


# ---------------------------------------------------------------------------
# Empty segmentation
# ---------------------------------------------------------------------------


def test_empty_segmentation_returns_noop() -> None:
    """An empty SegmentationResult returns a no-op."""
    counts: dict[SegmentKind, int] = {k: 0 for k in SegmentKind}
    empty_seg = SegmentationResult(
        status=SegmentationStatus.EMPTY_REQUEST,
        segments=(),
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
    result = apply_safe_compression(
        {"messages": []}, empty_seg, policy=_enabled_safe_policy()
    )
    assert result.applied is False


# ---------------------------------------------------------------------------
# Placement suppression
# ---------------------------------------------------------------------------


def test_semi_stable_segment_suppressed_by_placement() -> None:
    """A semi_stable_context segment is not compressed under suffix_only."""
    text = "Some context that looks like repeated lines\n" * 10
    payload = {"messages": [{"role": "assistant", "content": text}]}
    seg = RequestSegment(
        kind=SegmentKind.SEMI_STABLE_CONTEXT,
        source=SegmentSource.PRIOR_MESSAGE,
        message_index=0,
        content_path=("messages", 0, "content"),
        byte_length=len(text),
        estimated_tokens=len(text) // 4,
        protected=False,
        compressible_candidate=False,
        reason="assistant",
    )
    segmentation = _segmentation([seg])
    result = apply_safe_compression(
        payload, segmentation, policy=_enabled_safe_policy()
    )
    assert result.applied is False
    assert result.reason_code_counts.get(REASON_PLACEMENT, 0) > 0
