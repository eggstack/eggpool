"""End-to-end production-style tests for safe-mode compression.

Feeds real production-style payloads through segment_request() then
apply_safe_compression() to verify the full pipeline.
"""

from __future__ import annotations

from eggpool.transcoder.compression import (
    CompressionConfig,
    apply_safe_compression,
)
from eggpool.transcoder.compression.policy import CompressionTransforms
from eggpool.transcoder.segmentation import (
    SegmentKind,
    resolve_text_path,
    segment_request,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_policy(**overrides: object) -> CompressionConfig:
    """Safe-mode config with permissive thresholds."""
    transform_overrides: dict[str, bool] = {}
    top_overrides: dict[str, object] = dict(
        enabled=True,
        mode="safe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=100.0,
    )
    transform_keys = {
        "fold_repeated_lines",
        "compact_logs",
        "compact_search_results",
        "elide_base64_blobs",
        "minify_machine_json",
        "compact_stack_traces",
    }
    for key, value in overrides.items():
        if key in transform_keys:
            transform_overrides[key] = value  # type: ignore[assignment]
        else:
            top_overrides[key] = value
    defaults = {
        "fold_repeated_lines": True,
        "compact_logs": True,
        "compact_search_results": True,
        "elide_base64_blobs": True,
        "minify_machine_json": True,
        "compact_stack_traces": True,
    }
    defaults.update(transform_overrides)
    return CompressionConfig(
        **top_overrides,  # type: ignore[arg-type]
        transforms=CompressionTransforms(**defaults),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_openai_tool_message_with_large_repeated_log() -> None:
    """OpenAI tool message with repeated ERR lines is compressed."""
    repeated_content = "ERR: connection timeout\n" * 200 + "OK\n"
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "tool", "content": repeated_content},
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(
        payload, segmentation, policy=_safe_policy(compact_logs=False)
    )
    assert result.applied is True
    assert result.failed_fallback is False
    # System message must be preserved
    assert result.transformed_payload["messages"][0]["content"] == (
        "You are a helpful assistant."
    )
    # Transformed content should be shorter
    transformed_tool = result.transformed_payload["messages"][1]["content"]
    assert len(transformed_tool) < len(repeated_content)


def test_openai_latest_user_message_with_log() -> None:
    """OpenAI latest user message with log output is compressed."""
    log_content = (
        "INFO: starting build\n" * 10
        + "ERROR: build failed\n"
        + "DEBUG: retrying\n" * 20
        + "INFO: done\n" * 10
    )
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Build agent."},
            {"role": "user", "content": log_content},
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    assert result.failed_fallback is False
    assert result.applied is True


def test_openai_list_content_text_part() -> None:
    """OpenAI list-content message is compressed via the nested path."""
    big_text = "x" * 5000
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Sys."},
            {
                "role": "tool",
                "content": [{"type": "text", "text": big_text}],
            },
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(
        payload,
        segmentation,
        policy=_safe_policy(
            fold_repeated_lines=False,
            compact_logs=False,
        ),
    )
    # Verify no crash and prefix preserved
    assert result.failed_fallback is False
    assert result.stable_prefix_preserved is True


def test_anthropic_tool_result_string() -> None:
    """Anthropic tool_result with string content is compressed."""
    tool_output = "ERR\n" * 200 + "DONE\n"
    payload = {
        "model": "claude-sonnet-4",
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Run it."},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "run", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": tool_output}
                ],
            },
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    # Confirm the segmentation emits a concrete path to the string leaf
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert ("messages", 2, "content", 0, "content") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None

    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    assert result.applied is True
    assert result.failed_fallback is False
    assert result.stable_prefix_preserved is True
    # System must be preserved
    assert result.transformed_payload["system"] == "You are helpful."
    # tool_use_id must be preserved (block-level field, not the string leaf)
    tool_block = result.transformed_payload["messages"][2]["content"][0]
    assert tool_block["tool_use_id"] == "t1"
    # The string leaf itself was compressed: shorter and contains the marker
    transformed_content = tool_block["content"]
    assert isinstance(transformed_content, str)
    assert len(transformed_content) < len(tool_output)
    assert "[EggPool compression:" in transformed_content


def test_anthropic_tool_result_nested_list() -> None:
    """Anthropic tool_result with nested content list is compressed."""
    inner_text = "ERR\n" * 200 + "OK\n"
    payload = {
        "model": "claude-sonnet-4",
        "system": "Sys.",
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "run", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": inner_text}],
                    }
                ],
            },
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    candidate_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert ("messages", 2, "content", 0, "content", 0, "text") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None

    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    assert result.applied is True
    assert result.failed_fallback is False
    assert result.stable_prefix_preserved is True
    # The nested text field was compressed
    tool_block = result.transformed_payload["messages"][2]["content"][0]
    transformed_text = tool_block["content"][0]["text"]
    assert isinstance(transformed_text, str)
    assert len(transformed_text) < len(inner_text)
    assert "[EggPool compression:" in transformed_text
    # tool_use_id and tool type are preserved
    assert tool_block["type"] == "tool_result"
    assert tool_block["tool_use_id"] == "t1"


def test_anthropic_tool_result_non_text_nested_blocks_not_marked_compressible() -> None:
    """Nested blocks without string text are NOT marked compressible."""
    payload = {
        "model": "claude-sonnet-4",
        "system": "Sys.",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "data": "x"},
                            },
                        ],
                    }
                ],
            }
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    # No compressible segment should be emitted for the image nested block.
    compressible_tool_result_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert compressible_tool_result_paths == []
    # No path emitted for the non-text block either.
    for path in compressible_tool_result_paths:
        assert resolve_text_path(payload, path) is not None


def test_anthropic_tool_result_no_content_falls_back_to_observational() -> None:
    """A tool_result block with no ``content`` field falls back to a
    non-compressible segment at the block level so observability is
    preserved without a bogus compressible path."""
    payload = {
        "model": "claude-sonnet-4",
        "system": "Sys.",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1"},
                ],
            }
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    compressible_paths = [
        s.content_path
        for s in segmentation.segments
        if s.compressible_candidate and s.kind is SegmentKind.VOLATILE_SUFFIX
    ]
    assert compressible_paths == []


def test_anthropic_system_with_cache_control_preserved() -> None:
    """System with cache_control annotation is not compressed."""
    payload = {
        "model": "claude-sonnet-4",
        "system": [
            {
                "type": "text",
                "text": "System instructions.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {"role": "user", "content": "hi"},
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    assert result.failed_fallback is False
    # System content should be unchanged
    sys_block = result.transformed_payload["system"][0]
    assert sys_block["text"] == "System instructions."


def test_openai_tools_array_never_mutated() -> None:
    """System message + tools + tool_result: system and tools unchanged."""
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "ERR\n" * 200 + "OK\n"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "search", "parameters": {}},
            }
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    # System must be unchanged
    assert result.transformed_payload["messages"][0]["content"] == ("System prompt.")
    # Tools must be unchanged
    assert result.transformed_payload["tools"] == payload["tools"]


def test_anthropic_thinking_block_preserved() -> None:
    """Thinking block is protected and never mutated."""
    payload = {
        "model": "claude-sonnet-4",
        "system": "Sys.",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me think about this...",
                    },
                    {"type": "text", "text": "Here is my answer."},
                ],
            },
            {"role": "user", "content": "ERR\n" * 200 + "OK\n"},
        ],
    }
    segmentation = segment_request(payload, protocol="anthropic")
    result = apply_safe_compression(payload, segmentation, policy=_safe_policy())
    assert result.failed_fallback is False
    # Thinking block preserved in original form
    thinking_block = result.transformed_payload["messages"][0]["content"][0]
    assert thinking_block["type"] == "thinking"
    assert thinking_block["thinking"] == "Let me think about this..."


def test_compression_disabled_returns_original_payload() -> None:
    """Disabled compression returns the original payload unchanged."""
    content = "ERR\n" * 200 + "OK\n"
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Sys."},
            {"role": "tool", "content": content},
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(
        payload,
        segmentation,
        policy=CompressionConfig(enabled=False),
    )
    assert result.applied is False
    assert result.transformed_payload is payload


def test_observe_mode_returns_original_payload() -> None:
    """Observe mode returns the original payload unchanged."""
    content = "ERR\n" * 200 + "OK\n"
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "Sys."},
            {"role": "tool", "content": content},
        ],
    }
    segmentation = segment_request(payload, protocol="openai")
    result = apply_safe_compression(
        payload,
        segmentation,
        policy=CompressionConfig(enabled=True, mode="observe"),
    )
    assert result.applied is False
    assert result.transformed_payload is payload
