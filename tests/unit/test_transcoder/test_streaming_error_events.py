"""Tests for mid-stream error events in both streaming directions."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.transcoder.streaming import (
    AnthropicToOpenAIStreaming,
    OpenAIToAnthropicStreaming,
)


def _parse_sse_frames(raw: bytes) -> list[dict[str, Any]]:
    """Parse raw SSE output into a list of {event, data} dicts."""
    frames: list[dict[str, Any]] = []
    for block in raw.split(b"\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.split(b"\n"):
            if line.startswith(b"event: "):
                event = line[7:].decode()
            elif line.startswith(b"data: "):
                data = line[6:].decode()
        frames.append({"event": event, "data": data})
    return frames


def _openai_chunk(
    *,
    chunk_id: str = "chatcmpl-1",
    model: str = "gpt-4",
    content: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
) -> bytes:
    """Build an OpenAI SSE data frame."""
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _anthropic_sse(
    event: str,
    *,
    event_type: str | None = None,
    message_id: str = "msg-1",
    model: str = "claude-3",
    content: str | None = None,
    stop_reason: str | None = None,
    index: int = 0,
) -> bytes:
    """Build an Anthropic SSE frame."""
    if event_type is None:
        event_type = event

    if event == "message_start":
        payload: dict[str, Any] = {
            "type": event_type,
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                },
            },
        }
    elif event == "content_block_start":
        payload = {
            "type": event_type,
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
    elif event == "content_block_delta":
        payload = {
            "type": event_type,
            "index": index,
            "delta": {
                "type": "text_delta",
                "text": content or "",
            },
        }
    elif event == "content_block_stop":
        payload = {"type": event_type, "index": index}
    elif event == "message_delta":
        payload = {
            "type": event_type,
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": event_type}
    elif event == "error":
        payload = {
            "type": "error",
            "error": {
                "type": event_type or "api_error",
                "message": content or "Unknown error",
            },
        }
    else:
        payload = {"type": event_type}

    return (
        b"event: " + event.encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
    )


class TestAnthropicErrorToOpenAI:
    """Anthropic upstream error → OpenAI client error format."""

    @pytest.mark.asyncio
    async def test_anthropic_error_to_openai(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="authentication_error",
                content="Invalid API key",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert "error" in error_data
        assert error_data["error"]["message"] == "Invalid API key"
        assert error_data["error"]["type"] == "api_error"

        has_done = any(f["data"] == "[DONE]" for f in frames)
        assert has_done

    @pytest.mark.asyncio
    async def test_anthropic_rate_limit_to_openai(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="rate_limit_error",
                content="Rate limited",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert error_data["error"]["type"] == "api_error"

    @pytest.mark.asyncio
    async def test_anthropic_api_error_to_openai(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="api_error",
                content="Server overloaded",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert error_data["error"]["type"] == "api_error"


class TestOpenAIErrorToAnthropic:
    """OpenAI upstream error → Anthropic client error format.

    Uses event: error format since OpenAIToAnthropicStreaming
    translates upstream Anthropic-style event: error frames
    into Anthropic client format.
    """

    @pytest.mark.asyncio
    async def test_openai_error_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="authentication_error",
                content="Invalid API key",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert error_data["type"] == "error"
        assert error_data["error"]["message"] == "Invalid API key"
        assert error_data["error"]["type"] == "api_error"

        event_types = [f["event"] for f in frames]
        assert "message_stop" in event_types

    @pytest.mark.asyncio
    async def test_rate_limit_error_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="rate_limit_error",
                content="Rate limited",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert error_data["error"]["type"] == "api_error"

    @pytest.mark.asyncio
    async def test_api_error_to_anthropic(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()

        raw = await transcoder.feed(
            _anthropic_sse(
                "error",
                event_type="api_error",
                content="Server error",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert error_data["error"]["type"] == "api_error"


class TestErrorAfterPartialStream:
    @pytest.mark.asyncio
    async def test_error_after_partial_stream_openai_to_anthropic(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()

        chunk1 = _openai_chunk(role="assistant", content="Hello")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(content=" world")
        await transcoder.feed(chunk2)

        error_chunk = _anthropic_sse(
            "error",
            event_type="api_error",
            content="Stream interrupted",
        )
        raw = await transcoder.feed(error_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "error" in event_types
        assert "message_stop" in event_types

    @pytest.mark.asyncio
    async def test_error_after_partial_stream_anthropic_to_openai(
        self,
    ) -> None:
        transcoder = AnthropicToOpenAIStreaming()

        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hello", index=0)
        )

        error_chunk = _anthropic_sse(
            "error",
            event_type="api_error",
            content="Stream interrupted",
        )
        raw = await transcoder.feed(error_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        has_done = any(f["data"] == "[DONE]" for f in frames)
        assert has_done

    @pytest.mark.asyncio
    async def test_error_before_any_content(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()

        error_chunk = _anthropic_sse(
            "error",
            event_type="invalid_request_error",
            content="Bad model",
        )
        raw = await transcoder.feed(error_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        error_frame = frames[0]
        error_data = json.loads(error_frame["data"])
        assert "error" in error_data

        event_types = [f["event"] for f in frames]
        assert "message_stop" in event_types
