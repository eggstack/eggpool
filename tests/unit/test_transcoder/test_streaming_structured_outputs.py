"""Tests for Phase 6.4 — Structured outputs streaming (regression tests).

Structured outputs (response_format) affect request-level body
translation only.  The upstream's JSON response arrives as normal text
deltas in the stream.  These tests verify that streaming is unaffected
by the structured-outputs body translation.
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


class TestStructuredOutputsStreamingUnaffected:
    """Structured-outputs body translation does not affect streaming."""

    @pytest.mark.asyncio
    async def test_json_text_stream(self) -> None:
        """JSON response text arrives as normal content deltas."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        json_chunks = ['{"name":', ' "Alice"}']
        all_content = []
        for chunk in json_chunks:
            raw = await transcoder.feed(
                _anthropic_sse("content_block_delta", content=chunk, index=0)
            )
            frames = _parse_sse_frames(b"".join(raw))
            for f in frames:
                data = json.loads(f["data"])
                all_content.append(data["choices"][0]["delta"]["content"])

        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="end_turn"))
        await transcoder.flush()

        assert "".join(all_content) == '{"name": "Alice"}'

    @pytest.mark.asyncio
    async def test_json_stream_finish_reason(self) -> None:
        """JSON response stream terminates with stop finish_reason."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        await transcoder.feed(
            _anthropic_sse("content_block_delta", content='{"ok": true}', index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="end_turn")
        )
        await transcoder.flush()

        frames = _parse_sse_frames(b"".join(raw))
        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "stop"
