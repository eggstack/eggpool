"""Tests for Anthropic → OpenAI streaming SSE translation."""

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
    event_type: str | None = None,
    message_id: str = "msg-1",
    model: str = "claude-3",
    content: str | None = None,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
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
                "usage": usage or {"input_tokens": 10, "output_tokens": 0},
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
            "usage": usage or {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": event_type}
    else:
        payload = {"type": event_type}

    return (
        b"event: " + event.encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
    )


class TestMessageStart:
    @pytest.mark.asyncio
    async def test_message_start_emits_role_delta(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["role"] == "assistant"
        assert data["choices"][0]["delta"]["content"] == ""

    @pytest.mark.asyncio
    async def test_message_start_preserves_id(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.feed(
            _anthropic_sse("message_start", message_id="msg-xyz")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        data = json.loads(frames[0]["data"])
        assert data["id"] == "msg-xyz"


class TestContentBlockStart:
    @pytest.mark.asyncio
    async def test_content_block_start_noop(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )

        raw = await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 0


class TestContentBlockDelta:
    @pytest.mark.asyncio
    async def test_content_block_delta_emits_content(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hello", index=0)
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["content"] == "Hello"
        assert data["choices"][0]["delta"].get("role") is None

    @pytest.mark.asyncio
    async def test_multiple_deltas_concatenate(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-1",
                model="claude-3",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        texts = ["Hello", " ", "world"]
        all_content = []
        for text in texts:
            raw = await transcoder.feed(
                _anthropic_sse("content_block_delta", content=text, index=0)
            )
            combined = b"".join(raw)
            frames = _parse_sse_frames(combined)
            for f in frames:
                data = json.loads(f["data"])
                all_content.append(data["choices"][0]["delta"]["content"])

        assert "".join(all_content) == "Hello world"


class TestMessageDelta:
    @pytest.mark.asyncio
    async def test_message_delta_emits_finish_and_done(self) -> None:
        """message_delta with end_turn emits finish + [DONE]."""
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
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 5},
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        has_done = any(f["data"] == "[DONE]" for f in frames)
        assert has_done

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_message_delta_maps_max_tokens(self) -> None:
        """message_delta with max_tokens emits finish_reason chunk."""
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
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="max_tokens")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "length"

    @pytest.mark.asyncio
    async def test_message_delta_tool_use_maps_to_tool_calls(
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
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("message_delta", stop_reason="tool_use")
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert finish_data["choices"][0]["finish_reason"] == "tool_calls"


class TestUsageInMessageDelta:
    @pytest.mark.asyncio
    async def test_usage_in_message_delta(self) -> None:
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
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))

        raw = await transcoder.feed(
            _anthropic_sse(
                "message_delta",
                stop_reason="end_turn",
                usage={"output_tokens": 7},
            )
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        finish_frames = [
            f
            for f in frames
            if f["data"] != "[DONE]" and f["data"] and json.loads(f["data"])["choices"]
        ]
        assert len(finish_frames) == 1
        finish_data = json.loads(finish_frames[0]["data"])
        assert "usage" in finish_data
        assert finish_data["usage"]["completion_tokens"] == 7


class TestEmptyStream:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        raw = await transcoder.flush()

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        assert len(frames) >= 1
        assert frames[-1]["data"] == "[DONE]"


class TestIdAndModelPreserved:
    @pytest.mark.asyncio
    async def test_id_and_model_preserved(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(
            _anthropic_sse(
                "message_start",
                message_id="msg-abc",
                model="claude-3-opus",
            )
        )
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))

        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", content="Hi", index=0)
        )

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        data = json.loads(frames[0]["data"])
        assert data["id"] == "msg-abc"
        assert data["model"] == "claude-3-opus"


class TestArbitraryChunkBoundaries:
    @pytest.mark.asyncio
    async def test_arbitrary_chunk_boundaries(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        frame1 = _anthropic_sse("message_start", message_id="msg-1", model="claude-3")
        frame2 = _anthropic_sse("content_block_start", index=0)
        frame3 = _anthropic_sse("content_block_delta", content="Hel", index=0)
        frame4 = _anthropic_sse("content_block_delta", content="lo", index=0)

        all_bytes = frame1 + frame2 + frame3 + frame4

        split_at = 60
        chunk1 = all_bytes[:split_at]
        chunk2 = all_bytes[split_at:]

        raw1 = await transcoder.feed(chunk1)
        raw2 = await transcoder.feed(chunk2)

        all_frames = []
        for raw in [raw1, raw2]:
            combined = b"".join(raw)
            all_frames.extend(_parse_sse_frames(combined))

        content_frames = [
            f
            for f in all_frames
            if f["data"] and f["data"] != "[DONE]" and json.loads(f["data"])["choices"]
        ]
        role_frame = content_frames[0]
        role_data = json.loads(role_frame["data"])
        assert role_data["choices"][0]["delta"]["role"] == "assistant"

        text_frames = [
            f
            for f in content_frames
            if json.loads(f["data"])["choices"][0]["delta"].get("content")
        ]
        all_text = "".join(
            json.loads(f["data"])["choices"][0]["delta"]["content"] for f in text_frames
        )
        assert "Hello" in all_text
