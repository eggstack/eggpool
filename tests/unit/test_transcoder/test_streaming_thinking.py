"""Tests for Phase 6.3 — Thinking delta streaming translation."""

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
    index: int = 0,
    thinking_text: str | None = None,
    text: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
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
        block_type = "thinking" if thinking_text is not None else "text"
        payload = {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": block_type, "thinking": ""},
        }
    elif event == "content_block_delta":
        if thinking_text is not None:
            delta_obj: dict[str, Any] = {
                "type": "thinking_delta",
                "thinking": thinking_text,
            }
        else:
            delta_obj = {"type": "text_delta", "text": text or ""}
        payload = {
            "type": "content_block_delta",
            "index": index,
            "delta": delta_obj,
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


class TestThinkingDeltaStreaming:
    @pytest.mark.asyncio
    async def test_thinking_delta_emits_reasoning(self) -> None:
        """thinking_delta events translate to OpenAI reasoning deltas."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse(
                "content_block_delta", thinking_text="Let me think...", index=0
            )
        )

        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["reasoning"] == "Let me think..."
        assert data["choices"][0]["delta"].get("content") is None

    @pytest.mark.asyncio
    async def test_multiple_thinking_deltas_concatenate(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        all_reasoning = []
        for text in ["First", " ", "thought"]:
            raw = await transcoder.feed(
                _anthropic_sse("content_block_delta", thinking_text=text, index=0)
            )
            frames = _parse_sse_frames(b"".join(raw))
            for f in frames:
                data = json.loads(f["data"])
                all_reasoning.append(data["choices"][0]["delta"]["reasoning"])

        assert "".join(all_reasoning) == "First thought"

    @pytest.mark.asyncio
    async def test_thinking_then_text_stream(self) -> None:
        """Thinking block followed by text block produces reasoning then content."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))

        # Thinking block at index 0
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw1 = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Reasoning...", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        # Text block at index 1
        await transcoder.feed(_anthropic_sse("content_block_start", index=1))
        raw2 = await transcoder.feed(
            _anthropic_sse("content_block_delta", text="The answer.", index=1)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="end_turn"))
        await transcoder.flush()

        all_reasoning = []
        all_content = []
        for raw in [raw1, raw2]:
            for f in _parse_sse_frames(b"".join(raw)):
                if f["data"] == "[DONE]" or not f["data"]:
                    continue
                data = json.loads(f["data"])
                delta = data["choices"][0]["delta"]
                if "reasoning" in delta:
                    all_reasoning.append(delta["reasoning"])
                if delta.get("content"):
                    all_content.append(delta["content"])

        assert "".join(all_reasoning) == "Reasoning..."
        assert "".join(all_content) == "The answer."

    @pytest.mark.asyncio
    async def test_empty_thinking_delta_ignored(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="", index=0)
        )

        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 0

    @pytest.mark.asyncio
    async def test_thinking_delta_with_tool_use_stream(self) -> None:
        """Thinking block followed by tool_use block."""
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))

        # Thinking block at index 0
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw1 = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Analyzing...", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        # Tool use at index 1 — build the frame directly
        tool_block = {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_xyz",
                "name": "get_weather",
                "input": {},
            },
        }
        await transcoder.feed(
            b"event: content_block_start\n"
            b"data: " + json.dumps(tool_block).encode() + b"\n\n"
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        raw2 = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="tool_use")
        )
        await transcoder.flush()

        all_reasoning = []
        for raw in [raw1]:
            for f in _parse_sse_frames(b"".join(raw)):
                if f["data"] == "[DONE]" or not f["data"]:
                    continue
                data = json.loads(f["data"])
                delta = data["choices"][0]["delta"]
                if "reasoning" in delta:
                    all_reasoning.append(delta["reasoning"])

        assert "".join(all_reasoning) == "Analyzing..."

        # Verify tool_calls finish_reason is present
        tool_frames = []
        for f in _parse_sse_frames(b"".join(raw2)):
            if f["data"] and f["data"] != "[DONE]":
                data = json.loads(f["data"])
                if data["choices"]:
                    tool_frames.append(data)
        assert tool_frames[0]["choices"][0]["finish_reason"] == "tool_calls"
