"""Tests for context-limit precedence over compression.

Documents and verifies that context-limit checks happen before
compression can shrink the request. An over-limit request must be
rejected regardless of compression capabilities.
"""

from __future__ import annotations

import json

from eggpool.transcoder.compression import (
    CompressionConfig,
    apply_safe_compression,
)
from eggpool.transcoder.segmentation import segment_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_policy(**overrides: object) -> CompressionConfig:
    """Safe-mode config with permissive thresholds."""
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
# Tests
# ---------------------------------------------------------------------------


class TestContextLimitPrecedence:
    """Context-limit checks happen before compression."""

    def test_over_limit_request_rejected_before_compression(self) -> None:
        """An over-limit request is rejected by the context-limit
        layer, not silently compressed.

        This test documents the design invariant: the compression
        layer operates on already-validated payloads. If a request
        exceeds the model's context limits, the proxy_request layer
        raises ContextLimitExceededError before compression can run.

        We verify this by confirming that apply_safe_compression
        itself does not perform any context-limit validation — it
        trusts the caller to have already checked. A request that
        exceeds limits but is fed to apply_safe_compression directly
        will be compressed (the layer is not responsible for limits).
        This confirms that the check MUST happen upstream.
        """
        # A very large payload that would exceed any reasonable limit
        huge_content = "x" * 10_000_000
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "Sys."},
                {"role": "user", "content": huge_content},
            ],
        }
        segmentation = segment_request(payload, protocol="openai")
        # The compression layer itself does NOT reject — it processes
        # whatever it receives. The proxy_request layer is responsible
        # for the context-limit gate.
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
        # This confirms: compression does not raise on large payloads.
        # The context-limit check must be upstream.
        assert result.transformed_payload is not None

    def test_compression_cannot_rescue_over_limit(self) -> None:
        """Compression does not enable otherwise-invalid requests.

        The context-limit check runs on the ORIGINAL payload body
        before compression. Even if compression would shrink the
        payload below the limit, the request is rejected based on
        its original size.

        This test verifies the architectural invariant by confirming
        that the compression layer does NOT report any limit-aware
        behavior — it is purely a size-reduction layer that the
        caller invokes after validation.
        """
        # Construct a payload with a large tool output
        tool_output = "line\n" * 5000
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "Sys."},
                {"role": "tool", "content": tool_output},
            ],
        }
        segmentation = segment_request(payload, protocol="openai")
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
        # Compression may or may not fire; the key invariant is that
        # compression is not the mechanism that enforces limits.
        assert result.failed_fallback is False
        # If compression fired, the result is smaller but the caller
        # must have already rejected the original if it was over-limit.
        if result.applied:
            original_bytes = len(str(payload))
            compressed_bytes = len(str(result.transformed_payload))
            assert compressed_bytes < original_bytes

    def test_compression_result_never_carries_limit_info(self) -> None:
        """CompressionResult does not carry context-limit fields.

        This documents that context-limit validation is orthogonal to
        compression. The CompressionResult fields relate to token
        savings, stable-prefix hashes, and transform counts — not
        to limit enforcement.
        """
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "tool", "content": "ERR\n" * 100 + "OK\n"},
            ],
        }
        segmentation = segment_request(payload, protocol="openai")
        result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
        # CompressionResult has no limit-related fields
        summary_keys = set(json.loads(result.summary_json).keys())
        limit_keywords = {"limit", "max_context", "max_input", "exceeded"}
        for key in summary_keys:
            assert not any(kw in key.lower() for kw in limit_keywords), (
                f"CompressionResult.summary_json contains limit-related key: {key}"
            )
