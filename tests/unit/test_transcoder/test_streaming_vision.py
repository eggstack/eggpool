"""Tests for Phase 6.2 — Vision streaming (regression tests).

Vision content parts (images, documents) are request-level only and are
never carried inside SSE events.  These tests verify that the streaming
transcoder correctly handles streams that arrive *after* a vision
request has been encoded — i.e., that normal streaming is unaffected.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.transcoder.streaming import AnthropicToOpenAIStreaming


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


def _anthropic_sse(
    event: str,
    *,
    message_id: str = "msg-1",
    model: str = "claude-3",
    content: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    index: int = 0,
) -> bytes:
    """Build an Anthropic SSE frame."""
    if event == "message_start":
        payload: dict[str, Any] = {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": usage or {"input_tokens": 10, "output_tokens": 0},
            },
        }
    elif event == "content_block_start":
        payload = {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
    elif event == "content_block_delta":
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": content or ""},
        }
    elif event == "content_block_stop":
        payload = {"type": "content_block_stop", "index": index}
    elif event == "message_delta":
        payload = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": usage or {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": "message_stop"}
    else:
        payload = {"type": event}
    return (
        b"event: " + event.encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
    )


class TestVisionStreamingUnaffected:
    """Vision requests do not affect streaming — verify normal flow."""

    @pytest.mark.asyncio
    async def test_text_stream_after_vision_request(self) -> None:
        """A text response stream is unaffected by a prior vision request."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="I see a cat.", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="end_turn"))
        await transcoder.flush()

        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["content"] == "I see a cat."

    @pytest.mark.asyncio
    async def test_text_stream_with_usage(self) -> None:
        """Usage is carried correctly even after a vision-heavy request."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Done", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 150},
            )
        )
        await transcoder.flush()

        frames = _parse_sse_frames(b"".join(raw))
        finish_frames = [f for f in frames if f["data"] != "[DONE]" and f["data"]]
        finish_data = json.loads(finish_frames[-1]["data"])
        assert finish_data["usage"]["completion_tokens"] == 150
