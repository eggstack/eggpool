"""Tests for fail-closed verification in safe-mode compression.

Verifies that when a transform accidentally mutates stable-prefix
content, the applier detects the hash mismatch and returns the
original payload unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

from eggpool.transcoder.compression import apply_safe_compression
from eggpool.transcoder.compression.apply import REASON_PREFIX_HASH_MISMATCH
from eggpool.transcoder.segmentation import segment_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_policy(**overrides: object) -> object:
    """Safe-mode config with permissive thresholds."""
    from eggpool.transcoder.compression import CompressionConfig

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


def _make_payload() -> dict[str, object]:
    """Build an OpenAI payload with a system message (stable prefix)
    and a tool message (volatile suffix)."""
    return {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "tool", "content": "ERR\n" * 10 + "OK\n"},
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fail_closed_triggers_on_stable_prefix_mutation() -> None:
    """When pre/post stable-prefix hashes differ, fail-closed fires.

    We simulate a bug where the post-compression stable-prefix hash
    differs from the pre-compression hash by mocking
    ``stable_prefix_content_hash`` to return different values for
    the original vs mutated payload.
    """
    payload = _make_payload()
    segmentation = segment_request(payload, protocol="openai")

    call_count = 0

    def _fake_hash(p: object, s: object) -> str:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "pre_hash_original"
        return "post_hash_different"

    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        _fake_hash,
    ):
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())

    assert result.failed_fallback is True
    assert result.applied is False
    assert result.stable_prefix_preserved is False
    # The transformed_payload must be the ORIGINAL (not the mutated copy)
    assert result.transformed_payload is payload


def test_fail_closed_warning_includes_reason() -> None:
    """The warning tuple contains stable_prefix_hash_mismatch."""
    payload = _make_payload()
    segmentation = segment_request(payload, protocol="openai")

    call_count = 0

    def _fake_hash(p: object, s: object) -> str:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "pre_hash"
        return "post_hash"

    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        _fake_hash,
    ):
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())

    assert REASON_PREFIX_HASH_MISMATCH in result.warnings


def test_fail_closed_reason_code_bumped() -> None:
    """The reason_code_counts dict has stable_prefix_hash_mismatch > 0."""
    payload = _make_payload()
    segmentation = segment_request(payload, protocol="openai")

    call_count = 0

    def _fake_hash(p: object, s: object) -> str:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "pre"
        return "post"

    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        _fake_hash,
    ):
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())

    assert result.reason_code_counts.get(REASON_PREFIX_HASH_MISMATCH, 0) > 0


def test_no_fail_closed_when_only_volatile_changed() -> None:
    """Normal compression does NOT trigger fail-closed."""
    payload = _make_payload()
    segmentation = segment_request(payload, protocol="openai")

    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())

    assert result.failed_fallback is False
    assert result.stable_prefix_preserved is True
    # System message must be byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == ("You are helpful.")


def test_fail_closed_returns_original_not_copy() -> None:
    """The returned payload is the exact original object, not a copy."""
    payload = _make_payload()
    segmentation = segment_request(payload, protocol="openai")

    call_count = 0

    def _fake_hash(p: object, s: object) -> str:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "pre"
        return "post"

    with patch(
        "eggpool.transcoder.compression.apply.stable_prefix_content_hash",
        _fake_hash,
    ):
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())

    assert result.transformed_payload is payload
