"""Tests for Phase 8 — Response-field compatibility for reasoning output."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import (
    OpenAIReasoningFields,
    TranscoderFeatures,
    build_reasoning_fields,
)
from eggpool.transcoder.streaming import AnthropicToOpenAIStreaming


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
) -> TranscodeContext:
    return TranscodeContext(
        request_id="test-reasoning-fields",
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"thinking": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


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
                "usage": {"input_tokens": 10, "output_tokens": 0},
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
            "usage": {"output_tokens": 5},
        }
    elif event == "message_stop":
        payload = {"type": "message_stop"}
    else:
        payload = {"type": event}
    return (
        b"event: " + event.encode() + b"\n"
        b"data: " + json.dumps(payload).encode() + b"\n\n"
    )


# ---------------------------------------------------------------------------
# build_reasoning_fields helper
# ---------------------------------------------------------------------------


class TestBuildReasoningFields:
    def test_single_field(self) -> None:
        result = build_reasoning_fields(["reasoning_content"], "text")
        assert result == {"reasoning_content": "text"}

    def test_multiple_fields_aliases_off(self) -> None:
        result = build_reasoning_fields(["reasoning", "reasoning_content"], "text")
        assert result == {"reasoning": "text"}

    def test_multiple_fields_aliases_on(self) -> None:
        result = build_reasoning_fields(
            ["reasoning", "reasoning_content"], "text", emit_compat_aliases=True
        )
        assert result == {"reasoning": "text", "reasoning_content": "text"}

    def test_empty_field_names(self) -> None:
        result = build_reasoning_fields([], "text")
        assert result == {}

    def test_single_field_aliases_on(self) -> None:
        result = build_reasoning_fields(
            ["reasoning_content"], "text", emit_compat_aliases=True
        )
        assert result == {"reasoning_content": "text"}


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class TestOpenAIReasoningFieldsConfig:
    def test_defaults(self) -> None:
        cfg = OpenAIReasoningFields()
        assert cfg.non_stream == ["reasoning_content"]
        assert cfg.stream_delta == ["reasoning"]
        assert cfg.emit_compat_aliases is False

    def test_custom(self) -> None:
        cfg = OpenAIReasoningFields(
            non_stream=["reasoning", "reasoning_content"],
            stream_delta=["reasoning"],
            emit_compat_aliases=True,
        )
        assert cfg.non_stream == ["reasoning", "reasoning_content"]
        assert cfg.emit_compat_aliases is True


# ---------------------------------------------------------------------------
# Non-streaming: OpenAIToAnthropic.decode_response
# ---------------------------------------------------------------------------


class TestNonStreamingReasoningFields:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_default_field_name(self) -> None:
        payload = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "Thinking..."},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = self.transcoder.decode_response(
            payload, _make_context(), features=_features()
        )
        msg = result["choices"][0]["message"]
        assert msg["reasoning_content"] == "Thinking..."
        assert msg["content"] == "Answer"

    def test_custom_field_name(self) -> None:
        payload = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "Thinking..."},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = self.transcoder.decode_response(
            payload,
            _make_context(),
            features=_features(),
            reasoning_field_names=["reasoning"],
        )
        msg = result["choices"][0]["message"]
        assert msg["reasoning"] == "Thinking..."
        assert "reasoning_content" not in msg

    def test_compat_aliases(self) -> None:
        payload = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "Thinking..."},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = self.transcoder.decode_response(
            payload,
            _make_context(),
            features=_features(),
            reasoning_field_names=["reasoning", "reasoning_content"],
            emit_compat_aliases=True,
        )
        msg = result["choices"][0]["message"]
        assert msg["reasoning"] == "Thinking..."
        assert msg["reasoning_content"] == "Thinking..."

    def test_no_thinking_when_disabled(self) -> None:
        payload = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "Thinking..."},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=None
        )
        msg = result["choices"][0]["message"]
        assert "reasoning_content" not in msg
        assert "reasoning" not in msg
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)

    def test_no_thinking_when_feature_disabled(self) -> None:
        payload = {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "Thinking..."},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, warnings = self.transcoder.decode_response(
            payload,
            _make_context(),
            features=TranscoderFeatures(thinking=False),
        )
        msg = result["choices"][0]["message"]
        assert "reasoning_content" not in msg
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)


# ---------------------------------------------------------------------------
# Streaming: AnthropicToOpenAIStreaming
# ---------------------------------------------------------------------------


class TestStreamingReasoningFields:
    @pytest.mark.asyncio
    async def test_default_field_name(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Thinking...", index=0)
        )
        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["reasoning"] == "Thinking..."
        assert "reasoning_content" not in data["choices"][0]["delta"]

    @pytest.mark.asyncio
    async def test_custom_field_name(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning_content"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Thinking...", index=0)
        )
        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["reasoning_content"] == "Thinking..."
        assert "reasoning" not in data["choices"][0]["delta"]

    @pytest.mark.asyncio
    async def test_compat_aliases(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning", "reasoning_content"],
            emit_compat_aliases=True,
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Thinking...", index=0)
        )
        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        delta = data["choices"][0]["delta"]
        assert delta["reasoning"] == "Thinking..."
        assert delta["reasoning_content"] == "Thinking..."

    @pytest.mark.asyncio
    async def test_feature_gate_drops_thinking(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            features=TranscoderFeatures(thinking=False),
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Thinking...", index=0)
        )
        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 0

    @pytest.mark.asyncio
    async def test_feature_gate_none_allows_thinking(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(features=None)
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Thinking...", index=0)
        )
        frames = _parse_sse_frames(b"".join(raw))
        assert len(frames) == 1
        data = json.loads(frames[0]["data"])
        assert data["choices"][0]["delta"]["reasoning"] == "Thinking..."

    @pytest.mark.asyncio
    async def test_thinking_then_text_with_custom_fields(self) -> None:
        transcoder = AnthropicToOpenAIStreaming(
            reasoning_field_names=["reasoning_content"],
        )
        await transcoder.feed(_anthropic_sse("message_start"))
        await transcoder.feed(_anthropic_sse("content_block_start", index=0))
        raw1 = await transcoder.feed(
            _anthropic_sse("content_block_delta", thinking_text="Think...", index=0)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=0))
        await transcoder.feed(_anthropic_sse("content_block_start", index=1))
        raw2 = await transcoder.feed(
            _anthropic_sse("content_block_delta", text="Answer.", index=1)
        )
        await transcoder.feed(_anthropic_sse("content_block_stop", index=1))
        await transcoder.feed(_anthropic_sse("message_delta", stop_reason="end_turn"))
        await transcoder.flush()

        reasoning_text = ""
        content_text = ""
        for raw in [raw1, raw2]:
            for f in _parse_sse_frames(b"".join(raw)):
                if f["data"] == "[DONE]" or not f["data"]:
                    continue
                data = json.loads(f["data"])
                delta = data["choices"][0]["delta"]
                if "reasoning_content" in delta:
                    reasoning_text += delta["reasoning_content"]
                if delta.get("content"):
                    content_text += delta["content"]
        assert reasoning_text == "Think..."
        assert content_text == "Answer."
