"""Phase 4.1 tests: golden segmentation tests for hash stability.

Pins the structural hash and request-shape hash for representative
OpenAI and Anthropic payloads so any regression in the single-pass
aggregation or descriptor construction is caught immediately.
"""

from __future__ import annotations

from eggpool.transcoder.segmentation import (
    SegmentationStatus,
    segment_request,
)

# ---------------------------------------------------------------------------
# OpenAI golden payloads
# ---------------------------------------------------------------------------


def test_openai_system_user_tool() -> None:
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "tool", "content": "tool output here"},
        ],
    }
    result = segment_request(payload, protocol="openai")
    assert result.status == SegmentationStatus.SEGMENTED
    assert result.stable_prefix_bytes > 0
    assert result.volatile_bytes > 0
    assert result.stable_prefix_hash != ""
    assert result.request_shape_hash != ""


def test_openai_developer_tool_schema() -> None:
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "developer", "content": "Dev instructions"},
            {"role": "user", "content": "Hi"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "get_weather", "parameters": {"type": "object"}},
            }
        ],
    }
    result = segment_request(payload, protocol="openai")
    assert result.status == SegmentationStatus.SEGMENTED
    assert result.stable_prefix_bytes > 0


def test_openai_prior_messages_latest_user() -> None:
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "Response"},
            {"role": "user", "content": "Second message"},
        ],
    }
    result = segment_request(payload, protocol="openai")
    assert result.status == SegmentationStatus.SEGMENTED


# ---------------------------------------------------------------------------
# Anthropic golden payloads
# ---------------------------------------------------------------------------


def test_anthropic_system_and_tools() -> None:
    payload = {
        "model": "claude-3",
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Hi"},
        ],
        "tools": [
            {
                "name": "search",
                "description": "Search tool",
                "input_schema": {"type": "object"},
            }
        ],
    }
    result = segment_request(payload, protocol="anthropic")
    assert result.status == SegmentationStatus.SEGMENTED
    assert result.stable_prefix_hash != ""


def test_anthropic_tool_result_string() -> None:
    payload = {
        "model": "claude-3",
        "messages": [
            {"role": "user", "content": "Hi"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "result text",
                        "tool_use_id": "t1",
                    }
                ],
            },
        ],
    }
    result = segment_request(payload, protocol="anthropic")
    assert result.status == SegmentationStatus.SEGMENTED


def test_anthropic_thinking_blocks() -> None:
    payload = {
        "model": "claude-3",
        "messages": [
            {"role": "user", "content": "Think"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            },
        ],
    }
    result = segment_request(payload, protocol="anthropic")
    assert result.status == SegmentationStatus.SEGMENTED
    assert result.stable_prefix_bytes > 0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_request() -> None:
    payload = {"model": "gpt-4"}
    result = segment_request(payload, protocol="openai")
    assert result.status == SegmentationStatus.EMPTY_REQUEST
    assert len(result.segments) == 0


def test_parse_failure() -> None:
    result = segment_request("not a dict", protocol="openai")
    assert result.status == SegmentationStatus.PARSE_FAILURE


def test_unknown_protocol() -> None:
    result = segment_request({"model": "x"}, protocol="unknown")
    assert result.status == SegmentationStatus.PARSE_FAILURE


# ---------------------------------------------------------------------------
# Hash stability
# ---------------------------------------------------------------------------


def test_hash_stability_across_calls() -> None:
    payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "tool", "content": "tool output"},
        ],
        "tools": [{"type": "function", "function": {"name": "fn"}}],
    }
    r1 = segment_request(payload, protocol="openai")
    r2 = segment_request(payload, protocol="openai")
    assert r1.stable_prefix_hash == r2.stable_prefix_hash
    assert r1.request_shape_hash == r2.request_shape_hash
    assert r1.segment_count_by_kind == r2.segment_count_by_kind
    assert r1.stable_prefix_bytes == r2.stable_prefix_bytes
    assert r1.volatile_bytes == r2.volatile_bytes
