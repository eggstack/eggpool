"""Integration tests for streaming tool-call translation.

Exercises the streaming transcoder feed/flush pipeline for both
directions, verifying that tool_use/tool_call deltas survive the
streaming round trip.
"""

from __future__ import annotations

import json

import pytest

from eggpool.transcoder.streaming import (
    AnthropicToOpenAIStreaming,
    OpenAIToAnthropicStreaming,
)


def _openai_sse(data: dict[str, object]) -> bytes:
    """Build an OpenAI SSE frame from a dict."""
    return f"data: {json.dumps(data)}\n\n".encode()


def _anthropic_sse(event: str, data: dict[str, object]) -> bytes:
    """Build an Anthropic SSE frame from event type and dict."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


_MODEL_O = "gpt-4"
_MODEL_A = "claude-3"


# ── Anthropic upstream → OpenAI client (tool_use blocks) ──────────────


@pytest.mark.asyncio
async def test_anthropic_tool_use_stream_emits_openai_tool_calls() -> None:
    """Full Anthropic tool_use stream translates to OpenAI tool_calls deltas."""
    transcoder = AnthropicToOpenAIStreaming()

    chunks = [
        _anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg-tools-1",
                    "type": "message",
                    "role": "assistant",
                    "model": _MODEL_A,
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        _anthropic_sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_xyz789",
                    "name": "get_weather",
                    "input": {},
                },
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city"'},
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": ': "SF"}'},
            },
        ),
        _anthropic_sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 5},
            },
        ),
        _anthropic_sse("message_stop", {"type": "message_stop"}),
    ]

    all_output = b""
    for chunk in chunks:
        result = await transcoder.feed(chunk)
        for frame in result:
            all_output += frame

    flush_result = await transcoder.flush()
    for frame in flush_result:
        all_output += frame

    text = all_output.decode()
    frames = [f for f in text.split("\n\n") if f.strip()]

    # Should have tool_calls delta with id, function name, and arguments
    tool_call_frames = [f for f in frames if "tool_calls" in f and '"id"' in f]
    assert len(tool_call_frames) >= 1
    first_tool = json.loads(tool_call_frames[0].split("data: ")[1])
    tc = first_tool["choices"][0]["delta"]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert tc["id"].startswith("call_")

    # Should have arguments delta
    arg_frames = [f for f in frames if "tool_calls" in f and '"arguments"' in f]
    assert len(arg_frames) >= 1

    # Should end with finish_reason: tool_calls
    finish_frames = [f for f in frames if '"finish_reason": "tool_calls"' in f]
    assert len(finish_frames) == 1

    # Should end with [DONE]
    assert "data: [DONE]" in text


@pytest.mark.asyncio
async def test_anthropic_multiple_tool_use_blocks_parallel() -> None:
    """Two Anthropic tool_use blocks produce two OpenAI tool_calls."""
    transcoder = AnthropicToOpenAIStreaming()

    chunks = [
        _anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg-tools-2",
                    "model": _MODEL_A,
                    "content": [],
                    "usage": {"input_tokens": 10},
                },
            },
        ),
        _anthropic_sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_aaa",
                    "name": "get_weather",
                    "input": {},
                },
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"city":"NYC"}'},
            },
        ),
        _anthropic_sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        _anthropic_sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_bbb",
                    "name": "get_time",
                    "input": {},
                },
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"tz":"EST"}'},
            },
        ),
        _anthropic_sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 1},
        ),
        _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8},
            },
        ),
        _anthropic_sse("message_stop", {"type": "message_stop"}),
    ]

    all_output = b""
    for chunk in chunks:
        result = await transcoder.feed(chunk)
        for frame in result:
            all_output += frame

    flush_result = await transcoder.flush()
    for frame in flush_result:
        all_output += frame

    text = all_output.decode()

    # Extract all tool_calls entries from the output
    tool_call_ids: list[str] = []
    tool_names: list[str] = []
    for frame in text.split("\n\n"):
        if "tool_calls" not in frame or "data:" not in frame:
            continue
        try:
            parsed = json.loads(frame.split("data: ")[1])
        except (json.JSONDecodeError, IndexError):
            continue
        delta = parsed.get("choices", [{}])[0].get("delta", {})
        for tc in delta.get("tool_calls", []):
            if "id" in tc:
                tool_call_ids.append(tc["id"])
                tool_names.append(tc["function"]["name"])

    assert len(tool_names) == 2
    assert set(tool_names) == {"get_weather", "get_time"}
    assert all(tid.startswith("call_") for tid in tool_call_ids)


# ── OpenAI upstream → Anthropic client (tool_calls deltas) ────────────


@pytest.mark.asyncio
async def test_openai_tool_calls_stream_emits_anthropic_tool_use() -> None:
    """OpenAI tool_calls deltas buffer and emit Anthropic tool_use blocks at finish."""
    transcoder = OpenAIToAnthropicStreaming()

    chunks = [
        _openai_sse(
            {
                "id": "chatcmpl-tools-1",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": "chatcmpl-tools-1",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_aaa",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": "chatcmpl-tools-1",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"city":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": "chatcmpl-tools-1",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"SF"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": "chatcmpl-tools-1",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ),
        b"data: [DONE]\n\n",
    ]

    all_output = b""
    for chunk in chunks:
        result = await transcoder.feed(chunk)
        for frame in result:
            all_output += frame

    flush_result = await transcoder.flush()
    for frame in flush_result:
        all_output += frame

    text = all_output.decode()

    # Should contain content_block_start with tool_use
    assert "content_block_start" in text
    tool_block_start = [f for f in text.split("\n\n") if "content_block_start" in f]
    assert len(tool_block_start) >= 1
    block_data = json.loads(tool_block_start[0].split("data: ")[1])
    assert block_data["content_block"]["type"] == "tool_use"
    assert block_data["content_block"]["name"] == "get_weather"
    assert block_data["content_block"]["id"].startswith("toolu_")

    # Should contain tool_use stop_reason
    assert '"stop_reason": "tool_use"' in text
    stop_frames = [f for f in text.split("\n\n") if '"stop_reason": "tool_use"' in f]
    assert len(stop_frames) >= 1
    stop_data = json.loads(stop_frames[0].split("data: ")[1])
    assert stop_data["delta"]["stop_reason"] == "tool_use"

    # Should end with message_stop
    assert "message_stop" in text


@pytest.mark.asyncio
async def test_openai_parallel_tool_calls_stream() -> None:
    """Multiple OpenAI tool_calls indices produce multiple Anthropic tool_use blocks."""
    transcoder = OpenAIToAnthropicStreaming()

    chunks = [
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        # Tool call 0
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_001",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        # Tool call 1
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_002",
                                    "type": "function",
                                    "function": {"name": "get_time", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        # Arguments for tool 0
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"city":"SF"}'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        # Arguments for tool 1
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": '{"tz":"PST"}'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        # Finish
        _openai_sse(
            {
                "id": "chatcmpl-parallel",
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ),
        b"data: [DONE]\n\n",
    ]

    all_output = b""
    for chunk in chunks:
        result = await transcoder.feed(chunk)
        for frame in result:
            all_output += frame

    flush_result = await transcoder.flush()
    for frame in flush_result:
        all_output += frame

    text = all_output.decode()

    # Count content_block_start events (should be 2 for 2 tool_use blocks)
    block_starts = [f for f in text.split("\n\n") if "content_block_start" in f]
    assert len(block_starts) == 2

    # Both should be tool_use
    for bs in block_starts:
        data = json.loads(bs.split("data: ")[1])
        assert data["content_block"]["type"] == "tool_use"

    # Names should be get_weather and get_time
    names = set()
    for bs in block_starts:
        data = json.loads(bs.split("data: ")[1])
        names.add(data["content_block"]["name"])
    assert names == {"get_weather", "get_time"}

    # IDs should be toolu_ shaped
    for bs in block_starts:
        data = json.loads(bs.split("data: ")[1])
        assert data["content_block"]["id"].startswith("toolu_")
