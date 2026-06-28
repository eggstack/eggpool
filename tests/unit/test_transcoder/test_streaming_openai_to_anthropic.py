"""Tests for OpenAI → Anthropic streaming SSE translation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.transcoder.streaming import OpenAIToAnthropicStreaming


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

    choice: dict[str, Any] = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason,
    }
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [choice],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _openai_usage_chunk(
    *,
    chunk_id: str = "chatcmpl-1",
    model: str = "gpt-4",
    usage: dict[str, Any] | None = None,
) -> bytes:
    """Build an OpenAI SSE usage-only frame (no choices)."""
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [],
    }
    if usage is not None:
        payload["usage"] = usage
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class TestFirstContentChunk:
    @pytest.mark.asyncio
    async def test_first_content_chunk_emits_message_start_and_block(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(role="assistant", content="Hello")
        raw = await transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "message_start" in event_types
        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types

    @pytest.mark.asyncio
    async def test_message_start_contains_model_and_id(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(
            chunk_id="chatcmpl-42",
            model="gpt-4o",
            role="assistant",
            content="Hi",
        )
        raw = await transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_start = next(f for f in frames if f["event"] == "message_start")
        msg_data = json.loads(msg_start["data"])
        assert msg_data["type"] == "message_start"
        assert msg_data["message"]["id"] == "chatcmpl-42"
        assert msg_data["message"]["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_empty_content_produces_no_output(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(role="assistant", content="")
        raw = await transcoder.feed(chunk)

        assert raw == []


class TestSubsequentChunks:
    @pytest.mark.asyncio
    async def test_subsequent_chunks_emit_only_delta(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(content=" there")
        raw = await transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert event_types == ["content_block_delta"]

    @pytest.mark.asyncio
    async def test_delta_contains_text(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Start")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(content=" continuation")
        raw = await transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        delta_data = json.loads(frames[0]["data"])
        assert delta_data["type"] == "content_block_delta"
        assert delta_data["delta"]["type"] == "text_delta"
        assert delta_data["delta"]["text"] == " continuation"


class TestFinishReason:
    @pytest.mark.asyncio
    async def test_finish_reason_emits_stop_sequence(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="stop")
        raw = await transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        event_types = [f["event"] for f in frames]
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert "message_stop" in event_types

    @pytest.mark.asyncio
    async def test_finish_reason_maps_stop_to_end_turn(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="stop")
        raw = await transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["delta"]["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_finish_reason_maps_length_to_max_tokens(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        await transcoder.feed(chunk1)

        chunk2 = _openai_chunk(finish_reason="length")
        raw = await transcoder.feed(chunk2)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["delta"]["stop_reason"] == "max_tokens"


class TestUsageChunk:
    @pytest.mark.asyncio
    async def test_usage_chunk_translates(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hi")
        await transcoder.feed(chunk1)

        usage_chunk = _openai_usage_chunk(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        )
        raw = await transcoder.feed(usage_chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_delta = next(f for f in frames if f["event"] == "message_delta")
        delta_data = json.loads(msg_delta["data"])
        assert delta_data["type"] == "message_delta"
        assert "usage" in delta_data
        assert delta_data["usage"]["output_tokens"] == 5


class TestEmptyStream:
    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        raw = await transcoder.flush()

        assert raw == []


class TestMultiChunkConcatenation:
    @pytest.mark.asyncio
    async def test_multi_chunk_concatenation(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk1 = _openai_chunk(role="assistant", content="Hello")
        raw1 = await transcoder.feed(chunk1)

        all_delta_text = []
        for raw in [raw1]:
            frames = _parse_sse_frames(b"".join(raw))
            for f in frames:
                if f["event"] == "content_block_delta":
                    d = json.loads(f["data"])
                    all_delta_text.append(d["delta"]["text"])

        parts = [" ", "world", "!"]
        for part in parts:
            raw = await transcoder.feed(_openai_chunk(content=part))
            combined = b"".join(raw)
            frames = _parse_sse_frames(combined)
            for f in frames:
                if f["event"] == "content_block_delta":
                    d = json.loads(f["data"])
                    all_delta_text.append(d["delta"]["text"])

        assert "".join(all_delta_text) == "Hello world!"


class TestIdAndModelPreserved:
    @pytest.mark.asyncio
    async def test_id_and_model_preserved(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        chunk = _openai_chunk(
            chunk_id="chatcmpl-99",
            model="gpt-4o-mini",
            role="assistant",
            content="Hi",
        )
        raw = await transcoder.feed(chunk)

        combined = b"".join(raw)
        frames = _parse_sse_frames(combined)

        msg_start = next(f for f in frames if f["event"] == "message_start")
        msg_data = json.loads(msg_start["data"])
        assert msg_data["message"]["id"] == "chatcmpl-99"
        assert msg_data["message"]["model"] == "gpt-4o-mini"

        content_block_start = next(
            f for f in frames if f["event"] == "content_block_start"
        )
        cb_data = json.loads(content_block_start["data"])
        assert cb_data["index"] == 0


class TestArbitraryChunkBoundaries:
    @pytest.mark.asyncio
    async def test_arbitrary_chunk_boundaries(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        full_sse = (
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{'
            b'"role":"assistant","content":"Hello"},"index":0,'
            b'"finish_reason":null}],"model":"gpt-4",'
            b'"object":"chat.completion.chunk",'
            b'"created":1234567890}\n\n'
            b'data: {"id":"chatcmpl-1","choices":[{"delta":{'
            b'"content":" world"},"index":0,'
            b'"finish_reason":null}],"model":"gpt-4",'
            b'"object":"chat.completion.chunk",'
            b'"created":1234567890}\n\n'
        )

        split_at = 50
        chunk1 = full_sse[:split_at]
        chunk2 = full_sse[split_at:]

        raw1 = await transcoder.feed(chunk1)
        raw2 = await transcoder.feed(chunk2)

        all_frames = []
        for raw in [raw1, raw2]:
            combined = b"".join(raw)
            all_frames.extend(_parse_sse_frames(combined))

        event_types = [f["event"] for f in all_frames]
        assert "message_start" in event_types
        assert "content_block_delta" in event_types

        content_frames = [f for f in all_frames if f["event"] == "content_block_delta"]
        assert len(content_frames) >= 1
        d = json.loads(content_frames[0]["data"])
        assert "Hello" in d["delta"]["text"]
