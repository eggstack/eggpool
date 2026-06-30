"""Integration tests for thinking / reasoning transcoding.

Tests end-to-end encode→decode and encode→stream pipelines for the
extended thinking feature in both directions.
"""

from __future__ import annotations

import json

import pytest

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures
from eggpool.transcoder.streaming import AnthropicToOpenAIStreaming


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"thinking": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic thinking (non-streaming round-trip)
# ---------------------------------------------------------------------------


class TestThinkingOpenAIToAnthropicRoundTrip:
    def test_request_encode_then_response_decode(self) -> None:
        """OpenAI request with reasoning_effort encodes, then Anthropic
        response with thinking blocks decodes correctly."""
        ctx = TranscodeContext(
            request_id="integ-thinking-1",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        # 1. Encode request
        request = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think hard"}],
            "reasoning_effort": "high",
        }
        upstream, warnings = transcoder.encode_request(
            request, ctx, features=_features()
        )
        assert upstream["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        assert "reasoning_effort" not in upstream

        # 2. Decode response with thinking block
        response = {
            "id": "msg_123",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Step 1: analyze...",
                    "signature": "sig",
                },
                {"type": "text", "text": "The answer is 42."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 30},
        }
        client_response, decode_warnings = transcoder.decode_response(
            response, ctx, features=_features()
        )
        assert (
            client_response["choices"][0]["message"]["reasoning_content"]
            == "Step 1: analyze..."
        )
        assert (
            client_response["choices"][0]["message"]["content"] == "The answer is 42."
        )
        assert any(
            w.get("kind") == "thinking_signature_dropped" for w in decode_warnings
        )

    def test_disabled_thinking_preserves_v1_behaviour(self) -> None:
        """When thinking feature is off, reasoning_effort is dropped and
        thinking blocks in response are dropped."""
        ctx = TranscodeContext(
            request_id="integ-thinking-2",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        transcoder = OpenAIToAnthropic()

        request = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think"}],
            "reasoning_effort": "high",
        }
        _, warnings = transcoder.encode_request(request, ctx, features=None)
        assert any(w.get("kind") == "dropped_field" for w in warnings)

        response = {
            "id": "msg_456",
            "content": [
                {"type": "thinking", "thinking": "My thoughts"},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }
        _, decode_warnings = transcoder.decode_response(response, ctx, features=None)
        assert any(
            w.get("kind") == "reasoning_content_dropped" for w in decode_warnings
        )


# ---------------------------------------------------------------------------
# Anthropic → OpenAI thinking (non-streaming round-trip)
# ---------------------------------------------------------------------------


class TestThinkingAnthropicToOpenAIRoundTrip:
    def test_request_encode_then_response_decode(self) -> None:
        """Anthropic request with thinking blocks encodes, then OpenAI
        response decodes correctly."""
        ctx = TranscodeContext(
            request_id="integ-thinking-3",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        # 1. Encode request with thinking history
        request = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me reason...",
                            "signature": "sig",
                        },
                        {"type": "text", "text": "Here is my answer."},
                    ],
                },
            ],
        }
        upstream, warnings = transcoder.encode_request(
            request, ctx, features=_features()
        )
        assistant_msg = upstream["messages"][1]
        assert assistant_msg["reasoning_content"] == "Let me reason..."
        assert assistant_msg["content"] == "Here is my answer."
        assert any(w.get("kind") == "thinking_signature_dropped" for w in warnings)

    def test_disabled_thinking_drops_thinking_history(self) -> None:
        ctx = TranscodeContext(
            request_id="integ-thinking-4",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        transcoder = AnthropicToOpenAI()

        request = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "My thoughts"},
                        {"type": "text", "text": "Answer"},
                    ],
                },
            ],
        }
        _, warnings = transcoder.encode_request(request, ctx, features=None)
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)


# ---------------------------------------------------------------------------
# Thinking streaming (Anthropic → OpenAI)
# ---------------------------------------------------------------------------


class TestThinkingStreamingE2E:
    @pytest.mark.asyncio
    async def test_thinking_streaming_round_trip(self) -> None:
        """Full stream: thinking delta → text delta → finish."""
        transcoder = AnthropicToOpenAIStreaming()

        # message_start
        await transcoder.feed(
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"id":"msg-1","type":"message","role":"assistant","content":[],'
            b'"model":"claude-3","stop_reason":null,'
            b'"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        )

        # thinking block start
        await transcoder.feed(
            b'event: content_block_start\ndata: {"type":"content_block_start",'
            b'"index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
        )

        # thinking delta
        raw = await transcoder.feed(
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"thinking_delta","thinking":"Let me analyze"}}\n\n'
        )
        frames = []
        for block in b"".join(raw).split(b"\n\n"):
            if not block.strip():
                continue
            for line in block.split(b"\n"):
                if line.startswith(b"data: ") and line[6:] != b"[DONE]":
                    frames.append(json.loads(line[6:]))

        assert len(frames) == 1
        assert frames[0]["choices"][0]["delta"]["reasoning"] == "Let me analyze"
        assert frames[0]["choices"][0]["delta"].get("content") is None

        # thinking block stop
        _cbs0 = b'{"type":"content_block_stop","index":0}'
        await transcoder.feed(b"event: content_block_stop\ndata: " + _cbs0 + b"\n\n")

        # text block
        await transcoder.feed(
            b'event: content_block_start\ndata: {"type":"content_block_start",'
            b'"index":1,"content_block":{"type":"text","text":""}}\n\n'
        )
        raw2 = await transcoder.feed(
            b'event: content_block_delta\ndata: {"type":"content_block_delta",'
            b'"index":1,"delta":{"type":"text_delta","text":"Answer"}}\n\n'
        )
        text_frames = []
        for block in b"".join(raw2).split(b"\n\n"):
            if not block.strip():
                continue
            for line in block.split(b"\n"):
                if line.startswith(b"data: ") and line[6:] != b"[DONE]":
                    text_frames.append(json.loads(line[6:]))

        assert text_frames[0]["choices"][0]["delta"]["content"] == "Answer"

        # finish
        _cbs1 = b'{"type":"content_block_stop","index":1}'
        await transcoder.feed(b"event: content_block_stop\ndata: " + _cbs1 + b"\n\n")
        raw3 = await transcoder.feed(
            b"event: message_delta\n"
            b'data: {"type":"message_delta",'
            b'"delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":20}}\n\n'
        )
        await transcoder.flush()

        finish_frames = []
        for block in b"".join(raw3).split(b"\n\n"):
            if not block.strip():
                continue
            for line in block.split(b"\n"):
                if line.startswith(b"data: ") and line[6:] != b"[DONE]":
                    finish_frames.append(json.loads(line[6:]))

        assert finish_frames[0]["choices"][0]["finish_reason"] == "stop"
