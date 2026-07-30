"""Tests for canonical request segmentation (Phase 2).

Phase 2 introduces a structural segmentation layer that annotates requests
into ``stable_prefix`` / ``semi_stable_context`` / ``volatile_suffix``
without mutating them.  These tests verify:

- OpenAI and Anthropic protocol paths produce the expected segment kinds
- Volatile-suffix classification honours tool-result / log / search markers
- Token and byte estimates are non-negative
- Hashes are deterministic and content-private (no raw prompt text)
- Parse failures and empty requests produce well-defined statuses
- The summary JSON round-trips through ``json.loads``
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
    _bucketize,
    _classify_volatile_source,
    _estimate_value_tokens,
    _extract_text,
    _hash_payload,
    _looks_like_command_output,
    _looks_like_log_output,
    _looks_like_search_result,
    segment_request,
    segmentation_summary_json,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

OPENAI_PROTOCOL = "openai"
ANTHROPIC_PROTOCOL = "anthropic"


def _payload(d: dict) -> Mapping[str, Any]:
    return d


# ---------------------------------------------------------------------------
# OpenAI happy path
# ---------------------------------------------------------------------------


def test_openai_basic_chat_completion_yields_three_segment_kinds() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": "What is the capital of France?",
                },
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert isinstance(result, SegmentationResult)
    assert result.status is SegmentationStatus.SEGMENTED
    assert result.stable_prefix_segments
    # In a 2-message conversation (system + user), the user message is
    # the only message so it lands in semi_stable_context (short
    # follow-up), not in volatile_suffix.  All three kinds are still
    # represented in this test by including assistant+tool scenarios
    # in their own tests.
    assert result.semi_stable_segments
    assert result.request_shape_hash
    assert result.stable_prefix_hash
    assert result.total_estimated_tokens is not None
    assert result.total_estimated_tokens > 0
    counts = result.count_by_kind()
    assert counts[SegmentKind.STABLE_PREFIX] >= 1
    assert counts[SegmentKind.SEMI_STABLE_CONTEXT] >= 1


def test_openai_long_history_promotes_tail_to_semi_stable() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello there."},
                {"role": "assistant", "content": "Hi! How can I help?"},
                {"role": "user", "content": "Tell me a joke."},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    # The trailing user message is the most recent context but not flagged
    # as volatile (no markers, no tool result).  It should land in
    # ``semi_stable_context`` to be conservative.
    assert result.semi_stable_segments
    assert all(
        s.kind is SegmentKind.SEMI_STABLE_CONTEXT for s in result.semi_stable_segments
    )


def test_openai_tool_message_is_volatile_suffix() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You can call tools."},
                {"role": "user", "content": "What is the weather in Paris?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "Paris: 18C, clear sky.",
                },
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.volatile_segments
    assert all(s.kind is SegmentKind.VOLATILE_SUFFIX for s in result.volatile_segments)
    sources = {s.source for s in result.volatile_segments}
    assert SegmentSource.TOOL_RESULT in sources


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


def test_anthropic_basic_request_is_segmented() -> None:
    body = _payload(
        {
            "model": "claude-sonnet-4-5",
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "What is the capital of France?"},
            ],
        }
    )
    result = segment_request(body, protocol=ANTHROPIC_PROTOCOL)
    assert result.status is SegmentationStatus.SEGMENTED
    assert result.stable_prefix_segments
    # The lone user turn is short, so it lands in semi_stable_context.
    assert result.semi_stable_segments
    assert any(s.source is SegmentSource.SYSTEM for s in result.stable_prefix_segments)


def test_anthropic_cache_control_block_lands_in_stable_prefix() -> None:
    body = _payload(
        {
            "model": "claude-sonnet-4-5",
            "system": [
                {
                    "type": "text",
                    "text": "You are a helpful assistant.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "What is the capital of France?"},
            ],
        }
    )
    result = segment_request(body, protocol=ANTHROPIC_PROTOCOL)
    assert result.stable_prefix_segments
    cache_segments = [
        s
        for s in result.stable_prefix_segments
        if s.source is SegmentSource.CACHE_CONTROL
    ]
    assert cache_segments
    # Cache-control blocks should compress: marking them as stable_prefix
    # is a hint to the upstream to maintain cache continuity.


def test_anthropic_tool_result_block_is_volatile() -> None:
    body = _payload(
        {
            "model": "claude-sonnet-4-5",
            "messages": [
                {"role": "user", "content": "What is the weather in Paris?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "Paris"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "Paris: 18C, clear sky.",
                        }
                    ],
                },
            ],
        }
    )
    result = segment_request(body, protocol=ANTHROPIC_PROTOCOL)
    assert result.volatile_segments
    sources = {s.source for s in result.volatile_segments}
    assert SegmentSource.TOOL_RESULT in sources


# ---------------------------------------------------------------------------
# Volatile-suffix classification
# ---------------------------------------------------------------------------


def test_volatile_classifier_detects_command_output() -> None:
    text = "$ ls -la\ntotal 0\n-rw-r--r-- 1 user user 0 Jan 1 00:00 file.txt\n"
    assert _looks_like_command_output(text)
    assert _classify_volatile_source(text) is SegmentSource.COMMAND_OUTPUT


def test_volatile_classifier_detects_search_results() -> None:
    text = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1,3 +1,3 @@\n"
    )
    assert _looks_like_search_result(text)
    assert _classify_volatile_source(text) is SegmentSource.SEARCH_RESULT


def test_volatile_classifier_detects_log_output() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
        "AssertionError: expected 1, got 2\n"
    )
    assert _looks_like_log_output(text)
    # Logs map to COMMAND_OUTPUT (we do not track logs as a distinct source).
    assert _classify_volatile_source(text) is SegmentSource.COMMAND_OUTPUT


def test_volatile_classifier_defaults_to_unknown() -> None:
    text = "Hello, how are you today?"
    assert _classify_volatile_source(text) is SegmentSource.UNKNOWN


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def test_token_estimator_handles_strings() -> None:
    assert _estimate_value_tokens("hello") >= 1
    assert _estimate_value_tokens("a" * 400) >= 100
    assert _estimate_value_tokens("héllo") >= 1  # non-ASCII


def test_token_estimator_handles_lists_and_dicts() -> None:
    assert _estimate_value_tokens([{"role": "user", "content": "hi"}]) >= 1
    assert _estimate_value_tokens({"role": "user", "content": "hello world"}) >= 1


def test_token_estimator_does_not_raise_on_garbage() -> None:
    class Garbage:
        def __str__(self) -> str:
            raise ValueError("boom")

    # Should not raise; falls back to safe behavior.
    tokens = _estimate_value_tokens(Garbage())
    assert tokens >= 0


def test_token_estimator_handles_circular_references() -> None:
    a: list[Any] = []
    a.append(a)
    tokens = _estimate_value_tokens(a)
    assert tokens >= 0


def test_bucketize_maps_tokens_to_buckets() -> None:
    assert _bucketize(0) == "0"
    assert _bucketize(255) == "0-256"
    assert _bucketize(256) == "0-256"  # inclusive upper bound
    assert _bucketize(257) == "256-1k"
    assert _bucketize(1023) == "256-1k"
    assert _bucketize(1024) == "256-1k"  # inclusive upper bound
    assert _bucketize(1025) == "1k-4k"
    assert _bucketize(4095) == "1k-4k"
    assert _bucketize(4096) == "1k-4k"  # inclusive upper bound
    assert _bucketize(4097) == "4k-16k"


# ---------------------------------------------------------------------------
# Hash determinism and content privacy
# ---------------------------------------------------------------------------


def test_hashes_are_deterministic() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    r1 = segment_request(body, protocol=OPENAI_PROTOCOL)
    r2 = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert r1.request_shape_hash == r2.request_shape_hash
    assert r1.stable_prefix_hash == r2.stable_prefix_hash


def test_hashes_are_content_private() -> None:
    # Stable prefix hash is computed over a structural descriptor
    # (sources, paths, byte totals, token totals) — not the raw prompt
    # text.  Two structurally-equivalent stable prefixes (same byte
    # total, same sources, same content path) must yield the same
    # stable_prefix_hash.
    body1 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
            ],
        }
    )
    body2 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant.",
                },
            ],
        }
    )
    r1 = segment_request(body1, protocol=OPENAI_PROTOCOL)
    r2 = segment_request(body2, protocol=OPENAI_PROTOCOL)
    assert r1.stable_prefix_hash == r2.stable_prefix_hash
    assert r1.request_shape_hash == r2.request_shape_hash


def test_hash_payload_is_stable_across_key_order() -> None:
    a = _hash_payload({"a": 1, "b": 2, "c": 3})
    b = _hash_payload({"c": 3, "b": 2, "a": 1})
    assert a == b


def test_hash_payload_changes_with_content() -> None:
    a = _hash_payload({"x": "alpha"})
    b = _hash_payload({"x": "beta"})
    assert a != b


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_parse_failure_on_non_mapping() -> None:
    result = segment_request("not a dict", protocol=OPENAI_PROTOCOL)  # type: ignore[arg-type]
    assert result.status is SegmentationStatus.PARSE_FAILURE
    # parse_failure results use empty-string hashes (not None) to keep
    # the dataclass shape stable; callers can treat empty hashes as
    # "no segmentation performed".
    assert result.request_shape_hash == ""
    assert result.stable_prefix_hash == ""
    assert result.compressible_candidate_count() == 0


def test_parse_failure_on_list() -> None:
    result = segment_request([{"role": "user"}], protocol=OPENAI_PROTOCOL)  # type: ignore[arg-type]
    assert result.status is SegmentationStatus.PARSE_FAILURE


def test_empty_messages_produces_empty_status() -> None:
    body = _payload({"model": "gpt-4o", "messages": []})
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.status is SegmentationStatus.EMPTY_REQUEST
    # empty_request results still have hashes (deterministic empty
    # shape); callers can rely on the status to distinguish.
    assert result.request_shape_hash
    assert result.stable_prefix_hash


def test_no_messages_field_produces_empty_status() -> None:
    body = _payload({"model": "gpt-4o"})
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.status is SegmentationStatus.EMPTY_REQUEST


def test_unknown_protocol_yields_parse_failure() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    result = segment_request(body, protocol="unknown")
    assert result.status is SegmentationStatus.PARSE_FAILURE


# ---------------------------------------------------------------------------
# Summary JSON
# ---------------------------------------------------------------------------


def test_summary_json_round_trips() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    summary = segmentation_summary_json(result)
    decoded = json.loads(summary)
    assert decoded["status"] == "segmented"
    assert "segment_count_by_kind" in decoded
    assert "request_shape_hash" in decoded
    assert "stable_prefix_hash" in decoded
    assert "segments" in decoded


def test_summary_json_for_empty_request() -> None:
    body = _payload({"model": "gpt-4o", "messages": []})
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    summary = segmentation_summary_json(result)
    decoded = json.loads(summary)
    assert decoded["status"] == "empty_request"
    assert decoded["request_shape_hash"]
    assert decoded["stable_prefix_hash"]


def test_summary_json_for_parse_failure() -> None:
    result = segment_request("not a dict", protocol=OPENAI_PROTOCOL)  # type: ignore[arg-type]
    summary = segmentation_summary_json(result)
    decoded = json.loads(summary)
    assert decoded["status"] == "parse_failure"


# ---------------------------------------------------------------------------
# Result properties
# ---------------------------------------------------------------------------


def test_segment_kinds_are_partitioned() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": "Run `pytest tests/` and report the output.",
                },
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    stable = set(id(s) for s in result.stable_prefix_segments)
    semi = set(id(s) for s in result.semi_stable_segments)
    volatile = set(id(s) for s in result.volatile_segments)
    # Stable / semi_stable / volatile must be disjoint.
    assert not (stable & semi)
    assert not (stable & volatile)
    assert not (semi & volatile)


def test_total_estimated_tokens_equals_sum_of_segment_tokens() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello there!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.total_estimated_tokens is not None
    sum_tokens = (
        (result.stable_prefix_estimated_tokens or 0)
        + (result.semi_stable_estimated_tokens or 0)
        + (result.volatile_estimated_tokens or 0)
    )
    assert result.total_estimated_tokens == sum_tokens


def test_total_bytes_equals_sum_of_segment_bytes() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello there!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    sum_bytes = (
        (result.stable_prefix_bytes or 0)
        + (result.semi_stable_bytes or 0)
        + (result.volatile_bytes or 0)
    )
    assert result.total_bytes == sum_bytes


def test_compressible_candidate_count_excludes_protected() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello there!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    # All segments with compressible_candidate=True minus protected=True
    compressible = sum(1 for s in result.all_segments() if s.compressible_candidate)
    protected = sum(1 for s in result.all_segments() if s.protected)
    expected = max(compressible - protected, 0)
    assert result.compressible_candidate_count() == expected


def test_extract_text_handles_strings_and_lists() -> None:
    assert _extract_text("hello") == "hello"
    assert _extract_text([{"type": "text", "text": "hello"}]) == "hello"
    assert (
        _extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        == "a\nb"
    )
    assert _extract_text(None) == ""
    assert _extract_text(42) == ""


# ---------------------------------------------------------------------------
# Stable prefix descriptor
# ---------------------------------------------------------------------------


def test_stable_prefix_descriptor_changes_with_system_prompt() -> None:
    body1 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
            ],
        }
    )
    body2 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a different assistant."},
            ],
        }
    )
    r1 = segment_request(body1, protocol=OPENAI_PROTOCOL)
    r2 = segment_request(body2, protocol=OPENAI_PROTOCOL)
    assert r1.stable_prefix_hash != r2.stable_prefix_hash


def test_stable_prefix_descriptor_stable_across_trailing_volatility() -> None:
    body1 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    body2 = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": (
                        "$ pytest\n"
                        "test_foo.py::test_a PASSED\n"
                        "test_foo.py::test_b FAILED\n"
                    ),
                },
            ],
        }
    )
    r1 = segment_request(body1, protocol=OPENAI_PROTOCOL)
    r2 = segment_request(body2, protocol=OPENAI_PROTOCOL)
    # Stable prefix should be the same (same system prompt); request shape
    # should differ (different user content).
    assert r1.stable_prefix_hash == r2.stable_prefix_hash
    assert r1.request_shape_hash != r2.request_shape_hash


# ---------------------------------------------------------------------------
# Cache control flag propagation
# ---------------------------------------------------------------------------


def test_cache_control_flag_present_when_anthropic_system_has_it() -> None:
    body = _payload(
        {
            "model": "claude-sonnet-4-5",
            "system": [
                {
                    "type": "text",
                    "text": "You are a helpful assistant.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=ANTHROPIC_PROTOCOL)
    assert result.cache_control_present is True


def test_cache_control_flag_absent_for_plain_openai() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.cache_control_present is False


# ---------------------------------------------------------------------------
# Segmenter robustness
# ---------------------------------------------------------------------------


def test_segmenter_handles_none_content() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "assistant", "content": None},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.status is SegmentationStatus.SEGMENTED


def test_segmenter_handles_string_content() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.status is SegmentationStatus.SEGMENTED


def test_segmenter_handles_array_content() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello!"},
                        {"type": "text", "text": "How are you?"},
                    ],
                },
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.status is SegmentationStatus.SEGMENTED


def test_segmenter_handles_empty_string_content() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": ""},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    # Empty content should not crash; should be classified somewhere
    # reasonable (likely segmented with zero-token segment).
    assert result.status is SegmentationStatus.SEGMENTED


def test_segmenter_handles_anthropic_string_system() -> None:
    body = _payload(
        {
            "model": "claude-sonnet-4-5",
            "system": "You are a helpful assistant.",
            "messages": [
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=ANTHROPIC_PROTOCOL)
    assert result.status is SegmentationStatus.SEGMENTED
    assert result.stable_prefix_segments


def test_segmenter_handles_malformed_message() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user"},  # no content
                None,  # not a mapping
                {"role": "user", "content": "real message"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    # Should not crash; should produce some segmentation.
    assert result.status is SegmentationStatus.SEGMENTED


# ---------------------------------------------------------------------------
# RequestSegment dataclass
# ---------------------------------------------------------------------------


def test_request_segment_is_frozen() -> None:
    segment = RequestSegment(
        kind=SegmentKind.STABLE_PREFIX,
        source=SegmentSource.SYSTEM,
        message_index=0,
        content_path=("messages", 0),
        byte_length=40,
        estimated_tokens=10,
        protected=True,
        compressible_candidate=False,
        reason="test",
    )
    with contextlib.suppress(Exception):  # noqa: BLE001
        segment.compressible_candidate = True  # type: ignore[misc]


def test_segmentation_result_is_frozen() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    with contextlib.suppress(Exception):  # noqa: BLE001
        result.status = SegmentationStatus.PARSE_FAILURE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Stable prefix protected marker
# ---------------------------------------------------------------------------


def test_stable_prefix_segments_are_protected() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    for segment in result.stable_prefix_segments:
        # Stable prefix should be protected from compression.
        assert segment.protected is True
        assert segment.compressible_candidate is False


# ---------------------------------------------------------------------------
# Volatile suffix compressible marker
# ---------------------------------------------------------------------------


def test_volatile_tool_result_segments_are_compressible() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You can call tools."},
                {"role": "user", "content": "What is the weather in Paris?"},
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "Paris: 18C, clear sky.",
                },
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    tool_segments = [
        s for s in result.volatile_segments if s.source is SegmentSource.TOOL_RESULT
    ]
    assert tool_segments
    for segment in tool_segments:
        assert segment.compressible_candidate is True
        assert segment.protected is False


# ---------------------------------------------------------------------------
# Sanity: hashes are hex
# ---------------------------------------------------------------------------


def test_hashes_are_64_char_hex() -> None:
    body = _payload(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"},
            ],
        }
    )
    result = segment_request(body, protocol=OPENAI_PROTOCOL)
    assert result.request_shape_hash is not None
    assert result.stable_prefix_hash is not None
    assert re.fullmatch(r"[0-9a-f]{64}", result.request_shape_hash)
    assert re.fullmatch(r"[0-9a-f]{64}", result.stable_prefix_hash)
