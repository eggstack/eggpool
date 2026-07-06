"""Phase 3 tests: lazy / path-level copy for safe compression.

Pins the contract that safe-mode compression avoids deep-copying the
full payload when no transform applies, and uses path-level copy-on-write
when transforms do apply.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from eggpool.transcoder.compression.apply import (
    _copy_with_replacements,
    _PlannedReplacement,
    apply_safe_compression,
)
from eggpool.transcoder.compression.policy import CompressionConfig
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
    stable_prefix_content_hash,
)


def _seg(
    *,
    kind: SegmentKind,
    source: SegmentSource,
    protected: bool = False,
    byte_length: int = 0,
    estimated_tokens: int | None = None,
    compressible_candidate: bool | None = None,
    content_path: tuple[Any, ...] | None = None,
) -> RequestSegment:
    return RequestSegment(
        kind=kind,
        source=source,
        message_index=None,
        content_path=content_path or ("messages", 0, source.value),
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


def _enabled_safe_policy(**overrides: object) -> CompressionConfig:
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


def _repeated_lines_payload(n: int = 20) -> str:
    return "\n".join(["line repeated many times here for folding"] * n)


# ---------------------------------------------------------------------------
# Identity / no-op test: deep-copy is NOT called when no transform applies
# ---------------------------------------------------------------------------


def test_noop_does_not_call_deepcopy() -> None:
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    segmentation = _segmentation(
        [_seg(kind=SegmentKind.SEMI_STABLE_CONTEXT, source=SegmentSource.PRIOR_MESSAGE)]
    )
    policy = _enabled_safe_policy()
    with patch(
        "eggpool.transcoder.compression.apply._copy_with_replacements"
    ) as mock_cwr:
        result = apply_safe_compression(payload, segmentation, policy=policy)
    mock_cwr.assert_not_called()
    assert result.applied is False
    assert result.transformed_payload is payload


def test_noop_short_text_returns_original() -> None:
    payload = {"messages": [{"role": "user", "content": "short"}]}
    segmentation = _segmentation(
        [_seg(kind=SegmentKind.SEMI_STABLE_CONTEXT, source=SegmentSource.PRIOR_MESSAGE)]
    )
    policy = _enabled_safe_policy()
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is False
    assert result.transformed_payload is payload
    assert result.transform_count == 0
    assert result.savings_tokens == 0


# ---------------------------------------------------------------------------
# Applied-transform test: path-level copy-on-write preserves unchanged subtrees
# ---------------------------------------------------------------------------


def test_applied_transform_uses_path_level_copy() -> None:
    repeated = _repeated_lines_payload(20)
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": repeated},
        ]
    }
    stable_seg = _seg(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        content_path=("messages", 0, "content"),
        protected=True,
    )
    volatile_seg = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        content_path=("messages", 2, "content"),
        byte_length=len(repeated),
        estimated_tokens=200,
    )
    segmentation = _segmentation([stable_seg, volatile_seg])
    policy = _enabled_safe_policy()
    with patch(
        "eggpool.transcoder.compression.apply._copy_with_replacements"
    ) as mock_cwr:
        mock_cwr.return_value = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "folded"},
            ]
        }
        result = apply_safe_compression(payload, segmentation, policy=policy)
    mock_cwr.assert_called_once()
    assert result.applied is True
    assert result.transform_count >= 1
    assert result.transformed_payload["messages"][2]["content"] == "folded"


def test_stable_prefix_content_hash_preserved_after_apply() -> None:
    repeated = _repeated_lines_payload(20)
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "System instructions."},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": repeated},
        ]
    }
    stable_seg = _seg(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        content_path=("messages", 0, "content"),
        protected=True,
    )
    volatile_seg = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        content_path=("messages", 2, "content"),
        byte_length=len(repeated),
        estimated_tokens=200,
    )
    segmentation = _segmentation([stable_seg, volatile_seg])
    policy = _enabled_safe_policy()
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is True
    assert result.stable_prefix_preserved is True
    pre_hash = stable_prefix_content_hash(payload, segmentation)
    post_hash = stable_prefix_content_hash(result.transformed_payload, segmentation)
    assert pre_hash == post_hash


# ---------------------------------------------------------------------------
# Multi-transform ordering test on the same segment
# ---------------------------------------------------------------------------


def test_multi_transform_chaining_on_same_segment() -> None:
    lines = (
        ["line repeated many times here for folding"] * 10
        + ["ERROR: something broke"] * 3
        + ["normal line"] * 10
    )
    repeated = "\n".join(lines)
    payload: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": repeated},
        ]
    }
    volatile_seg = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        content_path=("messages", 1, "content"),
        byte_length=len(repeated),
        estimated_tokens=500,
    )
    segmentation = _segmentation([volatile_seg])
    policy = _enabled_safe_policy()
    result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is True
    assert result.transform_count >= 1
    transformed = result.transformed_payload
    assert isinstance(transformed, dict)
    assert transformed["messages"][1]["content"] != repeated


# ---------------------------------------------------------------------------
# Fallback test: post-hash mismatch returns original payload
# ---------------------------------------------------------------------------


def test_prefix_hash_mismatch_returns_original() -> None:
    repeated = _repeated_lines_payload(20)
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "tool", "content": repeated},
        ]
    }
    stable_seg = _seg(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        content_path=("messages", 0, "content"),
        protected=True,
    )
    volatile_seg = _seg(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=SegmentSource.TOOL_RESULT,
        content_path=("messages", 1, "content"),
        byte_length=len(repeated),
        estimated_tokens=200,
    )
    segmentation = _segmentation([stable_seg, volatile_seg])
    policy = _enabled_safe_policy()

    original_pre_hash = stable_prefix_content_hash(payload, segmentation)
    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        side_effect=[original_pre_hash, "different_hash"],
    ):
        result = apply_safe_compression(payload, segmentation, policy=policy)
    assert result.applied is False
    assert result.failed_fallback is True
    assert result.transformed_payload is payload


# ---------------------------------------------------------------------------
# _copy_with_replacements unit tests
# ---------------------------------------------------------------------------


def test_copy_with_replacements_single_path() -> None:
    payload = {"a": {"b": "old"}}
    repl = _PlannedReplacement(
        content_path=("a", "b"),
        new_text="new",
        orig_tokens=10,
        comp_tokens=5,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["a"]["b"] == "new"
    assert payload["a"]["b"] == "old"
    assert result is not payload
    assert result["a"] is not payload["a"]


def test_copy_with_replacements_preserves_unchanged_subtrees() -> None:
    shared_list = [1, 2, 3]
    payload = {
        "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": shared_list},
        ]
    }
    repl = _PlannedReplacement(
        content_path=("messages", 0, "content"),
        new_text="new",
        orig_tokens=10,
        comp_tokens=5,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["messages"][0]["content"] == "new"
    assert result["messages"][1]["content"] is shared_list


def test_copy_with_replacements_empty_list() -> None:
    payload = {"items": ["existing"]}
    repl = _PlannedReplacement(
        content_path=("items", 0),
        new_text="new",
        orig_tokens=1,
        comp_tokens=1,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["items"][0] == "new"
    assert payload["items"][0] == "existing"


def test_copy_with_replacements_no_replacements() -> None:
    payload = {"a": "b"}
    result = _copy_with_replacements(payload, [])
    assert result is payload


def test_copy_with_replacements_multiple_paths_sharing_prefix() -> None:
    payload = {"messages": [{"content": "a"}, {"content": "b"}]}
    repls = [
        _PlannedReplacement(
            content_path=("messages", 0, "content"),
            new_text="new_a",
            orig_tokens=1,
            comp_tokens=1,
            reason_code="fold",
            segment_id="s0",
        ),
        _PlannedReplacement(
            content_path=("messages", 1, "content"),
            new_text="new_b",
            orig_tokens=1,
            comp_tokens=1,
            reason_code="fold",
            segment_id="s1",
        ),
    ]
    result = _copy_with_replacements(payload, repls)
    assert result["messages"][0]["content"] == "new_a"
    assert result["messages"][1]["content"] == "new_b"
    assert payload["messages"][0]["content"] == "a"
    assert payload["messages"][1]["content"] == "b"


def test_copy_with_replacements_list_index() -> None:
    payload = {"items": ["old0", "old1", "old2"]}
    repl = _PlannedReplacement(
        content_path=("items", 1),
        new_text="new1",
        orig_tokens=1,
        comp_tokens=1,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["items"] == ["old0", "new1", "old2"]
    assert payload["items"] == ["old0", "old1", "old2"]


# ---------------------------------------------------------------------------
# Phase 5 corrective polish: pins for path-level copy-on-write invariants.
# ---------------------------------------------------------------------------


def test_copy_with_replacements_disjoint_branches_preserve_identity() -> None:
    """Unchanged subtrees on separate branches must remain equal, and where
    structurally safe, identical by object identity to the input.

    Note: only the dict/list nodes *on* the mutated path are shallow-copied.
    Sibling branches whose dict objects are not visited by the path walk
    are preserved by reference, even when the parent list is rebuilt.
    """
    inner = {"keep": "this-branch-untouched"}
    untouched_branch = {"role": "assistant", "content": inner}
    sibling_branch = {"role": "user", "content": "old_c"}
    payload = {
        "messages": [
            {"role": "user", "content": "old_a"},
            untouched_branch,
            sibling_branch,
        ]
    }
    repl = _PlannedReplacement(
        content_path=("messages", 0, "content"),
        new_text="new_a",
        orig_tokens=4,
        comp_tokens=2,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["messages"][0]["content"] == "new_a"
    # Sibling branches survive by reference (Phase 3 invariant).
    assert result["messages"][1] is untouched_branch
    assert result["messages"][1]["content"] is inner
    assert result["messages"][2] is sibling_branch
    # The list itself was rebuilt because one child mutated.
    assert result["messages"] is not payload["messages"]


def test_copy_with_replacements_duplicate_path_last_wins() -> None:
    """Duplicate replacement paths coalesce by insertion order; the
    helper's ``path_to_replacement`` dict keeps the LAST entry, which is
    the correct semantics for the safe-mode discovery loop because
    chained transforms update ``current_text = new_text`` so the final
    planned replacement reflects the fully chained final text.
    """
    payload = {"a": {"b": "original"}}
    repl_old = _PlannedReplacement(
        content_path=("a", "b"),
        new_text="intermediate",
        orig_tokens=10,
        comp_tokens=7,
        reason_code="fold",
        segment_id="s0",
    )
    repl_final = _PlannedReplacement(
        content_path=("a", "b"),
        new_text="final",
        orig_tokens=7,
        comp_tokens=3,
        reason_code="compact",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl_old, repl_final])
    assert result["a"]["b"] == "final"
    assert payload["a"]["b"] == "original"


def test_copy_with_replacements_invalid_path_does_not_mutate_input() -> None:
    """An invalid path (numeric index out of range) must NOT mutate the
    original payload. ``_copy_with_replacements`` is documented as
    fail-loud at the helper level; the public ``apply_safe_compression``
    boundary catches and degrades to a no-op. This test pins the helper
    contract: index errors propagate, identity of the input is preserved.
    """
    payload = {"items": ["only_one"]}
    repl = _PlannedReplacement(
        content_path=("items", 5),
        new_text="nope",
        orig_tokens=1,
        comp_tokens=1,
        reason_code="fold",
        segment_id="s0",
    )
    with pytest.raises((IndexError, KeyError, TypeError)):
        _copy_with_replacements(payload, [repl])
    assert payload == {"items": ["only_one"]}


def test_copy_with_replacements_unchanged_subtree_strict_identity() -> None:
    """Inner dicts on a path that is not mutated must remain identical
    by ``is`` to the input.  This is the structural-sharing contract.
    """
    inner = {"value": 1}
    payload = {
        "messages": [
            {"role": "user", "content": inner},
            {"role": "user", "content": {"value": 2}},
        ]
    }
    repl = _PlannedReplacement(
        content_path=("messages", 1, "content", "value"),
        new_text=99,
        orig_tokens=1,
        comp_tokens=1,
        reason_code="fold",
        segment_id="s0",
    )
    result = _copy_with_replacements(payload, [repl])
    assert result["messages"][0]["content"] is inner
    assert result["messages"][1]["content"]["value"] == 99


def test_copy_with_replacements_chained_text_last_wins() -> None:
    """When two transforms chain on the same segment the discovery loop
    updates ``current_text = new_text`` after each pass and records only
    one ``_PlannedReplacement`` for that ``content_path``. The helper
    must reflect the chained final text, not an intermediate value.
    """
    payload = {"a": {"b": "original"}}
    repl_intermediate = _PlannedReplacement(
        content_path=("a", "b"),
        new_text="intermediate_text",
        orig_tokens=10,
        comp_tokens=7,
        reason_code="fold_repeated_lines",
        segment_id="s0",
    )
    repl_final = _PlannedReplacement(
        content_path=("a", "b"),
        new_text="final_chained_text",
        orig_tokens=7,
        comp_tokens=3,
        reason_code="compact_logs",
        segment_id="s0",
    )
    direct = _copy_with_replacements(payload, [repl_intermediate, repl_final])
    assert direct["a"]["b"] == "final_chained_text"
    assert payload["a"]["b"] == "original"
