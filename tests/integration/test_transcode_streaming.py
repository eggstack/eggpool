"""Integration tests for streaming SSE translation.

Tests end-to-end streaming translation in both directions using
respx to mock upstream SSE responses.
"""

from __future__ import annotations

import json

import pytest

from eggpool.transcoder.streaming import (
    AnthropicToOpenAIStreaming,
    OpenAIToAnthropicStreaming,
    select_streaming_transcoder,
)


def _openai_sse(data: dict[str, object]) -> bytes:
    """Build an OpenAI SSE frame from a dict."""
    return f"data: {json.dumps(data)}\n\n".encode()


def _anthropic_sse(event: str, data: dict[str, object]) -> bytes:
    """Build an Anthropic SSE frame from event type and dict."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


_OPENAI_ID = "chatcmpl-123"
_ANTHROPIC_ID = "msg-abc"
_MODEL_O = "gpt-4"
_MODEL_A = "claude-3"


@pytest.mark.asyncio
async def test_select_streaming_transcoder_returns_correct_type() -> None:
    """Factory returns the correct transcoder for each protocol pair."""
    t = select_streaming_transcoder(
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    assert isinstance(t, AnthropicToOpenAIStreaming)

    t = select_streaming_transcoder(
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    assert isinstance(t, OpenAIToAnthropicStreaming)

    assert (
        select_streaming_transcoder(
            client_protocol="openai",
            upstream_protocol="openai",
        )
        is None
    )
    assert (
        select_streaming_transcoder(
            client_protocol="anthropic",
            upstream_protocol="anthropic",
        )
        is None
    )


@pytest.mark.asyncio
async def test_openai_upstream_to_anthropic_client_e2e() -> None:
    """OpenAI upstream SSE -> Anthropic client SSE (end-to-end)."""
    transcoder = OpenAIToAnthropicStreaming()

    chunks = [
        _openai_sse(
            {
                "id": _OPENAI_ID,
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
                "id": _OPENAI_ID,
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": _OPENAI_ID,
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        _openai_sse(
            {
                "id": _OPENAI_ID,
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": _MODEL_O,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
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
    frames = [f for f in text.split("\n\n") if f.strip()]

    # Verify message_start
    assert "event: message_start" in frames[0]
    start_data = json.loads(frames[0].split("data: ")[1])
    assert start_data["type"] == "message_start"
    assert start_data["message"]["id"] == _OPENAI_ID
    assert start_data["message"]["model"] == _MODEL_O

    # Verify content_block_start
    assert "event: content_block_start" in frames[1]

    # Verify content deltas
    assert "event: content_block_delta" in frames[2]
    delta_data = json.loads(frames[2].split("data: ")[1])
    assert delta_data["delta"]["text"] == "Hello"

    assert "event: content_block_delta" in frames[3]
    delta_data = json.loads(frames[3].split("data: ")[1])
    assert delta_data["delta"]["text"] == " world"

    # Verify finish sequence
    assert "event: content_block_stop" in frames[4]
    assert "event: message_delta" in frames[5]
    stop_data = json.loads(frames[5].split("data: ")[1])
    assert stop_data["delta"]["stop_reason"] == "end_turn"
    assert stop_data["usage"]["output_tokens"] == 2
    assert "event: message_stop" in frames[6]


@pytest.mark.asyncio
async def test_anthropic_upstream_to_openai_client_e2e() -> None:
    """Anthropic upstream SSE -> OpenAI client SSE (end-to-end)."""
    transcoder = AnthropicToOpenAIStreaming()

    chunks = [
        _anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": _ANTHROPIC_ID,
                    "type": "message",
                    "role": "assistant",
                    "model": _MODEL_A,
                    "content": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 0,
                    },
                },
            },
        ),
        _anthropic_sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"},
            },
        ),
        _anthropic_sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": " there"},
            },
        ),
        _anthropic_sse(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": 0,
            },
        ),
        _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        _anthropic_sse(
            "message_stop",
            {
                "type": "message_stop",
            },
        ),
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

    # Should contain OpenAI-formatted chunks
    assert "chat.completion.chunk" in text
    assert '"role": "assistant"' in text
    assert '"content": "Hi"' in text
    assert '"content": " there"' in text
    assert '"finish_reason": "stop"' in text
    assert "data: [DONE]" in text

    # Parse and verify usage — the message_delta carries output_tokens;
    # prompt_tokens comes from the same frame (defaults to 0 when
    # upstream message_delta doesn't include input_tokens).
    frames = [f for f in text.split("\n\n") if f.strip()]
    usage_found = False
    for frame in frames:
        if '"usage"' in frame and '"prompt_tokens"' in frame:
            usage_data = json.loads(frame.replace("data: ", ""))
            assert usage_data["usage"]["completion_tokens"] == 2
            usage_found = True
            break
    assert usage_found


@pytest.mark.asyncio
async def test_streaming_usage_finalized_correctly() -> None:
    """Usage extracted from upstream frames is accessible via .usage."""
    o2a = OpenAIToAnthropicStreaming()

    await o2a.feed(
        _openai_sse(
            {
                "id": "c1",
                "choices": [
                    {
                        "delta": {"role": "assistant", "content": "x"},
                        "finish_reason": None,
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            }
        )
    )
    await o2a.flush()

    usage = o2a.usage
    assert usage.input_tokens == 5
    assert usage.output_tokens == 3

    a2o = AnthropicToOpenAIStreaming()
    await a2o.feed(
        _anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "m1",
                    "model": _MODEL_A,
                    "usage": {"input_tokens": 10},
                },
            },
        )
    )
    await a2o.feed(
        _anthropic_sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        )
    )
    await a2o.flush()

    usage = a2o.usage
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5


@pytest.mark.asyncio
async def test_streaming_anthropic_error_to_openai_e2e() -> None:
    """Mid-stream Anthropic error translates to OpenAI error + [DONE]."""
    transcoder = AnthropicToOpenAIStreaming()

    # Start with a content chunk
    await transcoder.feed(
        _anthropic_sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "m1",
                    "model": _MODEL_A,
                    "content": [],
                    "usage": {"input_tokens": 5},
                },
            },
        )
    )

    # Then an error
    result = await transcoder.feed(
        _anthropic_sse(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "Server overloaded",
                },
            },
        )
    )

    all_output = b"".join(result)
    flush_result = await transcoder.flush()
    for frame in flush_result:
        all_output += frame

    text = all_output.decode()
    assert '"error"' in text
    assert "Server overloaded" in text
    assert "data: [DONE]" in text
